"""The owner's PWA: a device-authenticated app server and an optional admin server.

Both listen on loopback only. Publish the app through a reverse proxy on a private
network (for example Tailscale Serve, or Caddy reachable only over a VPN); see the README.
The admin server exposes invites and devices over HTTP for an external console; it is
disabled unless ADMIN_TOKEN is set, and the `bankhub invite` / `bankhub devices` commands
do the same from the terminal.
"""

import asyncio
import hashlib
import hmac
import os
import re
import threading
import uuid
from contextlib import closing
from dataclasses import dataclass
from datetime import date
from importlib import resources
from pathlib import Path
from urllib.parse import urlsplit

import uvicorn
from starlette.concurrency import run_in_threadpool
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

from .. import fx, ledger, sync
from ..config import Config, _read_env_file, load_config
from ..db import connect, connect_readonly, transaction
from .. import statements
from ..enablebanking import EnableBanking
from ..rules import load_rules
from . import data, rules_edit
from .alerts import Alerts
from .auth import AuthStore, RedeemLimiter

COOKIE = "bankhub_device"
STATIC = Path(str(resources.files("bankhub") / "web" / "static"))
SHELL_FILES = {"index.html", "app.js", "style.css", "manifest.webmanifest", "pwa-update.js", "sw-update.js",
               "bust.html", "bust.js", "PWA-KIT-LICENSE"}
MONTH = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
                               "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
}


@dataclass
class WebConfig:
    core: Config
    admin_token: str | None
    base_url: str          # public URL of the app, e.g. https://host.ts.net/bank
    cookie_path: str
    port: int
    admin_port: int
    bind: str = "127.0.0.1"   # 0.0.0.0 only inside a container whose port is published to loopback

    @property
    def callback_url(self) -> str:
        return self.base_url.rstrip("/") + "/callback"


def load_web_config(core: Config | None = None) -> WebConfig:
    core = core or load_config()
    env = {**_read_env_file(core.data_dir / ".env"), **os.environ}
    token = env.get("ADMIN_TOKEN") or None
    return WebConfig(core=core, admin_token=token,
                     base_url=env.get("PUBLIC_BASE_URL", "http://127.0.0.1:8041").rstrip("/"),
                     # The cookie is scoped to the app's own path (e.g. /bank) unless set explicitly.
                     cookie_path=env.get("COOKIE_PATH") or (urlsplit(env.get("PUBLIC_BASE_URL", "")).path.rstrip("/") or "/"),
                     port=int(env.get("WEB_PORT", "8041")), admin_port=int(env.get("ADMIN_PORT", "8141")),
                     bind=env.get("BANKHUB_BIND", "127.0.0.1"))


def _build_version() -> str:
    h = hashlib.sha256()
    for p in sorted(STATIC.rglob("*")):
        if p.is_file():
            h.update(p.name.encode())
            h.update(p.read_bytes())
    return h.hexdigest()[:12]


class Redeem(BaseModel):
    code: str


class Rule(BaseModel):
    pattern: str
    category: str
    subcategory: str | None = None
    name: str | None = None


class LinkStart(BaseModel):
    bank: str


class RuleEdit(BaseModel):
    etag: str
    patterns: list[str]
    category: str
    subcategory: str | None = None
    name: str | None = None


class Recategorize(BaseModel):
    scope: str            # "payee": every payment to this payee (a rule); "one": this payment only
    category: str
    subcategory: str | None = None
    pattern: str | None = None   # the match text the owner chose; a narrower one makes a new rule
    name: str | None = None


class PushSubscription(BaseModel):
    endpoint: str
    keys: dict


class AlertSettings(BaseModel):
    large_payment_ron: float | None = None
    balance_below_ron: float | None = None
    income_late: bool | None = None
    price_change: bool | None = None
    consent_expiring: bool | None = None
    health: bool | None = None
    details: bool | None = None


class AccountName(BaseModel):
    name: str | None = None


class Group(BaseModel):
    name: str
    behaviour: str
    category: str = "family"
    subcategory: str | None = None
    money_back: str = "money_in"
    sent_as: str = "spending"
    accounts: list[str] | None = None
    payees: list[str] = []
    ignored: list[str] = []


class OneOff(BaseModel):
    value: bool | None     # True: always one-off, False: never, None: automatic (by size)


CATEGORY = re.compile(r"^[a-z_]{2,30}$")


def _check_category(category: str, subcategory: str | None) -> None:
    if not CATEGORY.match(category) or (subcategory and not CATEGORY.match(subcategory)):
        raise HTTPException(400, "Categories are lowercase letters and underscores, 2-30 characters.")


def _secure(app: FastAPI) -> None:
    @app.middleware("http")
    async def headers(request: Request, call_next):
        response = await call_next(request)
        for k, v in SECURITY_HEADERS.items():
            response.headers.setdefault(k, v)
        if request.url.path.startswith("/api/") or request.url.path == "/callback":
            response.headers["Cache-Control"] = "no-store"
        return response


def create_app(cfg: WebConfig) -> FastAPI:
    # Apply any pending database migrations before serving: the app's reads use read-only
    # connections, which never migrate, so a new column would otherwise be missing until the next sync.
    connect(cfg.core.db_path).close()
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    _secure(app)
    app.add_middleware(GZipMiddleware, minimum_size=800)
    auth = AuthStore(cfg.core.data_dir / "web.db")
    alerts = Alerts(cfg.core.data_dir / "web.db", cfg.core.data_dir)
    limiter = RedeemLimiter()
    write_lock = threading.Lock()   # rebuilds and syncs from the UI run one at a time
    link_status: dict = {}          # the latest bank import started from the app
    version = _build_version()

    def ro():
        return closing(connect_readonly(cfg.core.db_path))

    def device(request: Request) -> dict:
        dev = auth.device_for_token(request.cookies.get(COOKIE))
        if not dev:
            raise HTTPException(401, "This device is not registered.")
        if request.method != "GET" and request.headers.get("x-bankhub") != "1":
            # A custom header cannot be sent cross-site without a CORS preflight, which we never allow.
            raise HTTPException(403, "Missing request header.")
        return dev

    def rebuild() -> dict:
        with write_lock, closing(connect(cfg.core.db_path)) as conn:
            fx.ensure_rates(conn)
            return ledger.rebuild(conn, load_rules(cfg.core.rules_path), one_off_ron=cfg.core.one_off_ron)

    # --- shell -------------------------------------------------------------------

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/sw.js")
    def service_worker():
        body = (STATIC / "sw.js").read_text().replace("__BUILD_VERSION__", version)
        return Response(body, media_type="text/javascript", headers={"Cache-Control": "no-cache, no-store"})

    @app.get("/bust")
    def bust():
        return FileResponse(STATIC / "bust.html", headers={"Cache-Control": "no-store", "Clear-Site-Data": '"cache"'})

    @app.get("/icons/{name}")
    def icon(name: str):
        path = STATIC / "icons" / name
        if not re.fullmatch(r"[a-z0-9-]+\.(png|svg)", name) or not path.is_file():
            raise HTTPException(404)
        return FileResponse(path, headers={"Cache-Control": "public, max-age=604800"})

    # --- registration ------------------------------------------------------------

    @app.post("/api/invites/redeem")
    @app.post("/api/auth/redeem")
    def redeem(body: Redeem, response: Response):
        if limiter.blocked():
            raise HTTPException(429, "Too many attempts. Try again in a few minutes.")
        result = auth.redeem(body.code)
        if not result:
            limiter.fail()
            raise HTTPException(400, "This code is not valid. It may have been used, revoked or expired.")
        token, dev = result
        response.set_cookie(COOKIE, token, max_age=400 * 86400, path=cfg.cookie_path, httponly=True,
                            secure=cfg.base_url.startswith("https://"), samesite="lax")
        return {"device": dev}

    @app.get("/api/me")
    def me(dev: dict = Depends(device)):
        return {"device": dev}

    # --- data --------------------------------------------------------------------

    @app.get("/api/overview")
    def overview(month: str | None = None, dev: dict = Depends(device)):
        month = month or date.today().strftime("%Y-%m")
        if not MONTH.match(month):
            raise HTTPException(400, "month must be YYYY-MM")
        with ro() as conn:
            return data.overview(conn, month)

    @app.get("/api/transactions")
    def transactions(month: str | None = None, category: str | None = None, q: str | None = None,
                     account: str | None = None, include_internal: bool = True, kind: str | None = None,
                     limit: int = 50, offset: int = 0, dev: dict = Depends(device)):
        if month and not MONTH.match(month):
            raise HTTPException(400, "month must be YYYY-MM")
        with ro() as conn:
            return data.transactions(conn, month=month, category=category, q=q, account=account,
                                     include_internal=include_internal, kind=kind, limit=limit,
                                     offset=max(0, offset))

    @app.get("/api/recurring")
    def recurring(dev: dict = Depends(device)):
        with ro() as conn:
            return data.recurring(conn)

    @app.get("/api/accounts")
    def accounts(dev: dict = Depends(device)):
        with ro() as conn:
            result = data.accounts(conn)
        result["can_link"] = bool(cfg.core.app_id) and cfg.core.redirect_url == cfg.callback_url
        result["link_status"] = dict(link_status) or None
        with ro() as conn:
            result["health"] = data.health(conn, cfg.core.data_dir)
        result["one_off_ron"] = cfg.core.one_off_ron
        result["callback_url"] = cfg.callback_url
        return result

    @app.get("/api/alerts")
    def alerts_status(dev: dict = Depends(device)):
        return alerts.status(dev["id"])

    @app.post("/api/alerts/settings")
    def alerts_settings(body: AlertSettings, dev: dict = Depends(device)):
        return alerts.save_settings({k: v for k, v in body.model_dump().items() if v is not None})

    @app.post("/api/push/subscribe")
    def push_subscribe(body: PushSubscription, dev: dict = Depends(device)):
        try:
            alerts.subscribe(dev["id"], body.model_dump())
        except ValueError as e:
            raise HTTPException(400, str(e))
        return alerts.status(dev["id"])

    @app.post("/api/push/unsubscribe")
    def push_unsubscribe(dev: dict = Depends(device)):
        alerts.unsubscribe(dev["id"])
        return alerts.status(dev["id"])

    @app.post("/api/push/test")
    def push_test(dev: dict = Depends(device)):
        if not alerts.test(dev["id"]):
            raise HTTPException(400, "This device is not subscribed, or the push service refused the message.")
        return {"ok": True}

    @app.post("/api/accounts/{alias}/name")
    def rename(alias: str, body: AccountName, dev: dict = Depends(device)):
        with write_lock, closing(connect(cfg.core.db_path)) as conn:
            if not data.rename_account(conn, alias, body.name):
                raise HTTPException(404)
        return {"ok": True}

    def save_group(group_id: int | None, body: Group):
        _check_category(body.category, body.subcategory)
        payload = body.model_dump(exclude_none=True)
        try:
            with write_lock, closing(connect(cfg.core.db_path)) as conn, transaction(conn):
                gid = data.save_group(conn, group_id, payload)
        except ValueError as e:
            raise HTTPException(400, str(e))
        except LookupError:
            raise HTTPException(404)
        return {"id": gid, "rebuilt": rebuild()}

    @app.post("/api/groups")
    def create_group(body: Group, dev: dict = Depends(device)):
        return save_group(None, body)

    @app.post("/api/groups/{group_id}")
    def update_group(group_id: int, body: Group, dev: dict = Depends(device)):
        return save_group(group_id, body)

    @app.delete("/api/groups/{group_id}")
    def remove_group(group_id: int, dev: dict = Depends(device)):
        with write_lock, closing(connect(cfg.core.db_path)) as conn:
            with transaction(conn):
                found = data.delete_group(conn, group_id)
            if not found:
                raise HTTPException(404)
        return {"rebuilt": rebuild()}

    @app.post("/api/statements")
    async def add_statement(request: Request, rebuild_after: bool = True, dev: dict = Depends(device)):
        """A credit card statement: the PDF itself is the request body. Only what is read from it is kept."""
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > statements.MAX_PDF_BYTES:
            raise HTTPException(413, "That PDF is larger than 5 MB.")
        body = await request.body()
        try:
            st = statements.parse_brd_card(await run_in_threadpool(statements.pdf_text, body))
            with write_lock, closing(connect(cfg.core.db_path)) as conn:
                result = statements.store_statement(conn, st)
        except statements.StatementError as e:
            raise HTTPException(400, str(e))
        if rebuild_after:
            result["rebuilt"] = rebuild()
        return result

    @app.get("/api/inbox")
    def inbox(dev: dict = Depends(device)):
        with ro() as conn:
            result = data.inbox(conn)
            result["categories"] = categories(conn)
        return result

    def categories(conn) -> list[str]:
        rules_text = cfg.core.rules_path.read_text() if cfg.core.rules_path.exists() else ""
        defaults = (resources.files("bankhub") / "data" / "categories.yaml").read_text()
        return sorted(set(data.category_choices(conn, defaults)) | set(data.category_choices(conn, rules_text)))

    @app.post("/api/inbox/rule")
    def add_rule(body: Rule, dev: dict = Depends(device)):
        _check_category(body.category, body.subcategory)
        try:
            entry = rules_edit.add(cfg.core.rules_path, patterns=[body.pattern], category=body.category,
                                   subcategory=body.subcategory, name=body.name or None)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"rule": entry, "rebuilt": rebuild()}

    @app.get("/api/rules")
    def list_rules(dev: dict = Depends(device)):
        with ro() as conn:
            return {"rules": rules_edit.list_rules(conn, cfg.core.rules_path), "categories": categories(conn)}

    @app.put("/api/rules/{rule_id}")
    def edit_rule(rule_id: str, body: RuleEdit, dev: dict = Depends(device)):
        _check_category(body.category, body.subcategory)
        try:
            entry = rules_edit.update(cfg.core.rules_path, rule_id, body.etag, patterns=body.patterns,
                                      category=body.category, subcategory=body.subcategory, name=body.name)
        except rules_edit.RuleConflict as e:
            raise HTTPException(409, str(e))
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"rule": entry, "rebuilt": rebuild()}

    @app.delete("/api/rules/{rule_id}")
    def delete_rule(rule_id: str, etag: str, dev: dict = Depends(device)):
        try:
            rules_edit.delete(cfg.core.rules_path, rule_id, etag)
        except rules_edit.RuleConflict as e:
            raise HTTPException(409, str(e))
        return {"rebuilt": rebuild()}

    def payee_source(row) -> str:
        return (row["counterparty_name"] if row["counterparty_has_account"]
                else (row["remittance"] if row["remittance"] and "*" in row["remittance"] else
                      row["counterparty_name"] or row["remittance"])) or ""

    @app.get("/api/transactions/{tx_id}/payee_rule")
    def payee_rule(tx_id: int, dev: dict = Depends(device)):
        """The rule 'all payments to this payee' would create or change, and what it would catch."""
        with ro() as conn:
            row = conn.execute("""SELECT t.*, c.merchant, c.counterparty_type FROM transactions_raw t
                                  JOIN transactions_clean c ON c.id = t.id WHERE t.id = ?""", (tx_id,)).fetchone()
            if not row:
                raise HTTPException(404)
            user_rules = rules_edit.parse_rules(rules_edit.load(cfg.core.rules_path))
            existing = rules_edit.first_match(user_rules, row)
            pattern = data.suggest_pattern(payee_source(row))
            current = None
            if existing:
                section, i = existing.split(":")
                entry = rules_edit.load(cfg.core.rules_path)[section][int(i)]
                current = {"id": existing, "name": entry.get("name"), "patterns": entry["patterns"],
                           "category": entry["category"]}
            return {"pattern": pattern, "existing": current, "preview": rules_edit.preview(conn, pattern),
                    "named": row["counterparty_type"] == "merchant"}

    @app.get("/api/rules/preview")
    def rules_preview(pattern: str, dev: dict = Depends(device)):
        if len(pattern.strip()) < 3:
            raise HTTPException(400, "At least 3 characters.")
        with ro() as conn:
            return rules_edit.preview(conn, pattern)

    @app.post("/api/transactions/{tx_id}/category")
    def recategorize(tx_id: int, body: Recategorize, dev: dict = Depends(device)):
        _check_category(body.category, body.subcategory)
        with ro() as conn:
            row = conn.execute(
                """SELECT t.*, c.merchant, c.counterparty_type, c.is_internal_transfer
                   FROM transactions_raw t JOIN transactions_clean c ON c.id = t.id WHERE t.id = ?""",
                (tx_id,)).fetchone()
        if not row:
            raise HTTPException(404)
        if row["is_internal_transfer"]:
            raise HTTPException(400, "Transfers between your own accounts are not spending and have no category.")
        if body.scope == "one":
            with write_lock, closing(connect(cfg.core.db_path)) as conn:
                conn.execute(
                    """INSERT INTO transaction_overrides(raw_id, category, subcategory, updated_at)
                       VALUES (?,?,?,datetime('now'))
                       ON CONFLICT(raw_id) DO UPDATE SET category=excluded.category,
                           subcategory=excluded.subcategory, updated_at=excluded.updated_at""",
                    (tx_id, body.category, body.subcategory))
            return {"scope": "one", "rebuilt": rebuild()}
        if body.scope != "payee":
            raise HTTPException(400, "scope must be 'payee' or 'one'")
        if row["amount_minor"] > 0:
            raise HTTPException(400, "Money in can only be recategorized one payment at a time.")
        user_rules = rules_edit.parse_rules(rules_edit.load(cfg.core.rules_path))
        rule_id = rules_edit.first_match(user_rules, row)
        chosen = (body.pattern or "").strip()
        if chosen and not chosen.startswith("re:"):
            chosen = chosen.lower()
        if rule_id and chosen:
            section, i = rule_id.split(":")
            if chosen not in rules_edit.load(cfg.core.rules_path)[section][int(i)]["patterns"]:
                rule_id = None   # a narrower match: a new rule, checked first, instead of changing the broad one
        try:
            if rule_id:
                # The payee already has one of your rules: change that rule rather than stacking another.
                current = rules_edit.load(cfg.core.rules_path)
                section, i = rule_id.split(":")
                entry = current[section][int(i)]
                result = rules_edit.update(cfg.core.rules_path, rule_id, rules_edit.etag(entry),
                                           patterns=entry["patterns"], category=body.category,
                                           subcategory=body.subcategory, name=entry.get("name"))
            else:
                pattern = chosen or data.suggest_pattern(payee_source(row))
                name = None
                if row["counterparty_type"] == "merchant":
                    # A narrower match on a known merchant ("google*cloud") gets its own name ("Google Cloud").
                    name = body.name or (rules_edit.rule_name(pattern) if chosen else row["merchant"])
                result = rules_edit.add(cfg.core.rules_path, patterns=[pattern],
                                        category=body.category, subcategory=body.subcategory, name=name)
        except ValueError as e:
            raise HTTPException(400, f"Could not make a rule for this payee ({e}); change this payment only.")
        with write_lock, closing(connect(cfg.core.db_path)) as conn:
            conn.execute("UPDATE transaction_overrides SET category=NULL, subcategory=NULL WHERE raw_id=?", (tx_id,))
        return {"scope": "payee", "rule": result, "rebuilt": rebuild()}

    @app.post("/api/transactions/{tx_id}/one_off")
    def set_one_off(tx_id: int, body: OneOff, dev: dict = Depends(device)):
        with write_lock, closing(connect(cfg.core.db_path)) as conn:
            if not conn.execute("SELECT 1 FROM transactions_raw WHERE id=?", (tx_id,)).fetchone():
                raise HTTPException(404)
            conn.execute(
                """INSERT INTO transaction_overrides(raw_id, one_off, updated_at) VALUES (?,?,datetime('now'))
                   ON CONFLICT(raw_id) DO UPDATE SET one_off=excluded.one_off, updated_at=excluded.updated_at""",
                (tx_id, None if body.value is None else int(body.value)))
        return {"rebuilt": rebuild()}

    @app.delete("/api/transactions/{tx_id}/category")
    def reset_category(tx_id: int, dev: dict = Depends(device)):
        with write_lock, closing(connect(cfg.core.db_path)) as conn:
            conn.execute("UPDATE transaction_overrides SET category=NULL, subcategory=NULL WHERE raw_id=?", (tx_id,))
        return {"rebuilt": rebuild()}

    @app.post("/api/sync")
    def sync_now(dev: dict = Depends(device)):
        if not cfg.core.app_id:
            raise HTTPException(400, "Enable Banking is not configured.")
        with write_lock, closing(connect(cfg.core.db_path)) as conn, \
                EnableBanking(cfg.core.app_id, cfg.core.key_path) as eb:
            sync.expire_consents(conn)
            result = sync.sync_all(conn, eb)
        rebuilt = rebuild()
        try:
            with ro() as conn:
                alerts.run(conn)
        except Exception:   # an alert problem must never break a sync
            pass
        return {"accounts": [a.__dict__ for a in result.accounts], "rebuilt": rebuilt}

    # --- bank linking ------------------------------------------------------------

    @app.get("/api/banks")
    def banks(dev: dict = Depends(device)):
        with EnableBanking(cfg.core.app_id, cfg.core.key_path) as eb:
            return {"banks": sorted(a["name"] for a in eb.aspsps(cfg.core.country))}

    @app.post("/api/link/start")
    def link_start(body: LinkStart, dev: dict = Depends(device)):
        if cfg.core.redirect_url != cfg.callback_url:
            raise HTTPException(400, f"Set BANKHUB_REDIRECT_URL={cfg.callback_url} and register it in the "
                                     "Enable Banking Control Panel first.")
        with EnableBanking(cfg.core.app_id, cfg.core.key_path) as eb:
            match = [a for a in eb.aspsps(cfg.core.country) if a["name"].lower() == body.bank.lower()]
            if not match:
                raise HTTPException(404, "Unknown bank.")
            state = str(uuid.uuid4())
            auth.add_pending_link(state, match[0]["name"], dev["id"])
            url, _, valid_until = eb.start_auth(match[0], cfg.core.country, cfg.callback_url, state=state)
        return {"url": url, "valid_until": valid_until.date().isoformat()}

    @app.get("/callback")
    def callback(request: Request, state: str = "", code: str = "", error: str = ""):
        back = cfg.base_url + "/#accounts"
        dev = auth.device_for_token(request.cookies.get(COOKIE))
        pending = auth.take_pending_link(state) if state else None
        if not dev or not pending or pending["device_id"] != dev["id"]:
            return RedirectResponse(back + "?link=invalid", status_code=303)
        if error or not code:
            return RedirectResponse(back + "?link=cancelled", status_code=303)
        # Exchange the code now (it expires quickly); fetch the history in the background, so the phone
        # gets an answer at once instead of a blank screen while years of transactions download.
        with EnableBanking(cfg.core.app_id, cfg.core.key_path) as eb:
            session = eb.create_session(code)
        bank = pending["bank"]
        link_status.clear()
        link_status.update({"bank": bank, "state": "importing", "accounts": 0, "transactions": 0})

        def run_import():
            try:
                with write_lock, closing(connect(cfg.core.db_path)) as conn, \
                        EnableBanking(cfg.core.app_id, cfg.core.key_path) as eb2:
                    ids = sync.register_session(conn, session, cfg.core.country)
                    if not ids:
                        link_status.update(state="no-accounts")
                        return
                    for acc in conn.execute(f"SELECT * FROM accounts WHERE id IN ({','.join('?' * len(ids))})", ids):
                        r = sync.sync_account(conn, eb2, acc, backfill=True)
                        link_status["accounts"] += 1
                        link_status["transactions"] += r.fetched
                rebuild()
                link_status.update(state="ok")
            except Exception as e:   # shown to the owner; bank error bodies carry no account data
                link_status.update(state="error", error=str(e)[:200])

        threading.Thread(target=run_import, daemon=True).start()
        return RedirectResponse(back + "?link=importing", status_code=303)

    # Registered last: a catch-all would shadow /callback and anything after it.
    @app.get("/{name}")
    def shell_file(name: str):
        if name not in SHELL_FILES:
            raise HTTPException(404)
        media = "application/manifest+json" if name.endswith(".webmanifest") else None
        return FileResponse(STATIC / name, media_type=media, headers={"Cache-Control": "no-cache"})

    return app


def create_admin_app(cfg: WebConfig) -> FastAPI:
    """Invites and devices over HTTP, for an external admin console. Every request needs the
    X-Admin-Token header; with ADMIN_TOKEN unset, everything is refused."""
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    _secure(app)
    auth = AuthStore(cfg.core.data_dir / "web.db")

    @app.middleware("http")
    async def admin_only(request: Request, call_next):
        given = request.headers.get("x-admin-token", "")
        if not cfg.admin_token or not hmac.compare_digest(given.encode(), cfg.admin_token.encode()):
            return JSONResponse({"detail": "forbidden"}, status_code=403)
        return await call_next(request)

    class Label(BaseModel):
        label: str = ""

    class Revoked(BaseModel):
        revoked: bool = True

    @app.get("/api/admin/invites")
    def invites():
        return {"invites": auth.list_invites(cfg.base_url)}

    @app.post("/api/admin/invites")
    def create_invite(body: Label):
        return auth.create_invite(body.label.strip()[:60], cfg.base_url)

    @app.post("/api/admin/invites/{invite_id}/revoke")
    def revoke_invite(invite_id: int):
        if not auth.revoke_invite(invite_id):
            raise HTTPException(404)
        return {"ok": True}

    @app.get("/api/admin/devices")
    def devices():
        return {"devices": auth.list_devices()}

    @app.post("/api/admin/devices/{device_id}/revoke")
    def revoke_device(device_id: int, body: Revoked | None = None):
        if not auth.set_device_revoked(device_id, True if body is None else body.revoked):
            raise HTTPException(404)
        return {"ok": True}

    @app.post("/api/admin/devices/{device_id}/label")
    def label_device(device_id: int, body: Label):
        if not auth.set_device_label(device_id, body.label.strip()[:60]):
            raise HTTPException(404)
        return {"ok": True}

    @app.delete("/api/admin/devices/{device_id}")
    def delete_device(device_id: int):
        if not auth.delete_device(device_id):
            raise HTTPException(404)
        return {"ok": True}

    return app


def serve() -> None:
    cfg = load_web_config()

    async def main():
        servers = [uvicorn.Server(uvicorn.Config(create_app(cfg), host=cfg.bind, port=cfg.port,
                                                 proxy_headers=False, access_log=False, log_level="warning"))]
        if cfg.admin_token:   # the admin API exists only for an external console that has the token
            servers.append(uvicorn.Server(uvicorn.Config(create_admin_app(cfg), host=cfg.bind, port=cfg.admin_port,
                                                         proxy_headers=False, access_log=False, log_level="warning")))
        await asyncio.gather(*(s.serve() for s in servers))

    asyncio.run(main())
