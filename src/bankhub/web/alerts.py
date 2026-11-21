"""Phone alerts: conditions worth knowing about, sent as Web Push to the owner's devices.

Each alert has a stable key, so it is sent once. By default a notification says what kind of
thing happened, not the amount or the payee (lock screens are visible); `details` turns those on.
"""

import base64
import json
import os
import sqlite3
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

DEFAULTS = {
    "large_payment_ron": 2000,     # a single payment at or above this; 0 = off
    "balance_below_ron": 0,        # total of all accounts below this; 0 = off
    "income_late": True,
    "price_change": True,
    "consent_expiring": True,
    "health": True,                # a bank that stopped syncing, a backup that stopped running
    "details": False,              # amounts and payees in the notification text
}
SCHEMA = """
CREATE TABLE IF NOT EXISTS push_subscriptions (
    device_id  INTEGER PRIMARY KEY REFERENCES devices(id) ON DELETE CASCADE,
    endpoint   TEXT NOT NULL UNIQUE,
    p256dh     TEXT NOT NULL,
    auth       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_sent  TEXT,
    last_error TEXT
);
CREATE TABLE IF NOT EXISTS alert_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS alert_log (
    key   TEXT PRIMARY KEY,
    at    TEXT NOT NULL,
    title TEXT NOT NULL,
    body  TEXT NOT NULL,
    url   TEXT NOT NULL,
    sent  INTEGER NOT NULL DEFAULT 0
);
"""
PUSH_HOSTS = {"fcm.googleapis.com", "updates.push.services.mozilla.com", "web.push.apple.com"}


@dataclass
class Alert:
    key: str
    title: str
    body: str          # with details
    private_body: str  # without amounts or names
    url: str = "./#month"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def validate_subscription(sub: dict) -> dict:
    endpoint = sub.get("endpoint", "")
    parsed = urlsplit(endpoint if isinstance(endpoint, str) else "")
    host = parsed.hostname or ""
    if parsed.scheme != "https" or len(endpoint) > 4096 or parsed.username or parsed.port not in (None, 443) \
            or not (host in PUSH_HOSTS or host.endswith(".notify.windows.com")):
        raise ValueError("Unsupported push service. Use Chrome, Firefox, Edge, or an installed Safari web app.")
    keys = sub.get("keys") or {}
    try:
        ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), _unb64(keys["p256dh"]))
        if len(_unb64(keys["auth"])) != 16:
            raise ValueError
    except (KeyError, TypeError, ValueError) as e:
        raise ValueError("Invalid subscription keys") from e
    return {"endpoint": endpoint, "p256dh": keys["p256dh"], "auth": keys["auth"]}


class Alerts:
    def __init__(self, web_db: Path, data_dir: Path, subject: str = "mailto:bankhub@localhost"):
        self.web_db, self.data_dir, self.subject = web_db, data_dir, subject
        with self._conn() as c:
            c.executescript(SCHEMA)
        self.key_path = data_dir / "secrets" / "vapid.pem"
        if not self.key_path.exists():
            self.key_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            key = ec.generate_private_key(ec.SECP256R1())
            pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                    serialization.NoEncryption())
            with os.fdopen(os.open(self.key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as f:
                f.write(pem)
        key = serialization.load_pem_private_key(self.key_path.read_bytes(), None)
        raw = key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
        self.public_key = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.web_db, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA busy_timeout = 5000")
        return c

    # --- settings and subscriptions ---------------------------------------------------

    def settings(self) -> dict:
        with self._conn() as c:
            stored = {r["key"]: json.loads(r["value"]) for r in c.execute("SELECT * FROM alert_settings")}
        return {**DEFAULTS, **{k: v for k, v in stored.items() if k in DEFAULTS or k == "since_id"}}

    def save_settings(self, values: dict) -> dict:
        clean = {}
        for k, default in DEFAULTS.items():
            if k in values:
                v = values[k]
                clean[k] = max(0, float(v)) if isinstance(default, (int, float)) and not isinstance(default, bool) \
                    else bool(v)
        with self._conn() as c:
            for k, v in clean.items():
                c.execute("INSERT OR REPLACE INTO alert_settings(key, value) VALUES (?,?)", (k, json.dumps(v)))
        return self.settings()

    def subscribe(self, device_id: int, sub: dict) -> None:
        s = validate_subscription(sub)
        with self._conn() as c:
            c.execute("DELETE FROM push_subscriptions WHERE endpoint=? AND device_id<>?", (s["endpoint"], device_id))
            c.execute("""INSERT INTO push_subscriptions(device_id, endpoint, p256dh, auth, created_at) VALUES (?,?,?,?,?)
                         ON CONFLICT(device_id) DO UPDATE SET endpoint=excluded.endpoint, p256dh=excluded.p256dh,
                         auth=excluded.auth, last_error=NULL""",
                      (device_id, s["endpoint"], s["p256dh"], s["auth"], _now()))
            # Payment alerts start after the newest transaction stored now: older payments are not news.
            if not c.execute("SELECT 1 FROM alert_settings WHERE key='since_id'").fetchone():
                c.execute("INSERT INTO alert_settings(key, value) VALUES ('since_id', ?)",
                          (json.dumps(self._newest_transaction_id()),))

    def _newest_transaction_id(self) -> int:
        db = self.data_dir / "bankhub.db"
        if not db.exists():
            return 0
        c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            return c.execute("SELECT coalesce(max(id), 0) FROM transactions_raw").fetchone()[0]
        except sqlite3.OperationalError:
            return 0
        finally:
            c.close()

    def unsubscribe(self, device_id: int) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM push_subscriptions WHERE device_id=?", (device_id,))

    def status(self, device_id: int) -> dict:
        with self._conn() as c:
            row = c.execute("SELECT last_sent, last_error FROM push_subscriptions WHERE device_id=?",
                            (device_id,)).fetchone()
            recent = [dict(r) for r in c.execute(
                "SELECT at, title, body, url FROM alert_log ORDER BY at DESC LIMIT 10")]
        return {"public_key": self.public_key, "subscribed": row is not None,
                "last_sent": row["last_sent"] if row else None, "last_error": row["last_error"] if row else None,
                "settings": self.settings(), "recent": recent}

    # --- sending -------------------------------------------------------------------------

    def _send_all(self, payload: dict, only_device: int | None = None) -> int:
        from pywebpush import WebPushException, webpush
        sent = 0
        with self._conn() as c:
            subs = c.execute(
                """SELECT p.* FROM push_subscriptions p JOIN devices d ON d.id = p.device_id
                   WHERE d.revoked = 0""" + (" AND p.device_id = ?" if only_device else ""),
                (only_device,) if only_device else ()).fetchall()
            for s in subs:
                try:
                    webpush({"endpoint": s["endpoint"], "keys": {"p256dh": s["p256dh"], "auth": s["auth"]}},
                            json.dumps(payload), vapid_private_key=str(self.key_path),
                            vapid_claims={"sub": self.subject}, ttl=24 * 3600, timeout=15)
                    c.execute("UPDATE push_subscriptions SET last_sent=?, last_error=NULL WHERE device_id=?",
                              (_now(), s["device_id"]))
                    sent += 1
                except WebPushException as e:
                    status = getattr(e.response, "status_code", None)
                    if status in (404, 410):   # the browser dropped this subscription
                        c.execute("DELETE FROM push_subscriptions WHERE device_id=?", (s["device_id"],))
                    else:
                        c.execute("UPDATE push_subscriptions SET last_error=? WHERE device_id=?",
                                  (str(status or e)[:200], s["device_id"]))
        return sent

    def test(self, device_id: int) -> int:
        return self._send_all({"title": "Bank Hub", "body": "Notifications work. You'll hear from me when something "
                               "needs a look.", "url": "./#accounts", "tag": "bankhub-test"}, only_device=device_id)

    def run(self, bank_conn: sqlite3.Connection, today: date | None = None, send: bool = True) -> list[Alert]:
        """Evaluates every condition; sends and logs the ones not sent before."""
        settings = self.settings()
        found = evaluate(bank_conn, settings, self.data_dir, today or date.today())
        with self._conn() as c:
            have_subs = c.execute("SELECT 1 FROM push_subscriptions LIMIT 1").fetchone() is not None
            new = [a for a in found if not c.execute("SELECT 1 FROM alert_log WHERE key=?", (a.key,)).fetchone()]
        if not have_subs or not send:
            return new   # nothing is logged, so these still go out once a phone subscribes
        for a in new:
            body = a.body if settings["details"] else a.private_body
            sent = self._send_all({"title": a.title, "body": body, "url": a.url, "tag": a.key[:60]})
            with self._conn() as c:
                c.execute("INSERT OR IGNORE INTO alert_log(key, at, title, body, url, sent) VALUES (?,?,?,?,?,?)",
                          (a.key, _now(), a.title, body, a.url, sent))
        return new


def _ron(v: float) -> str:
    return f"{v:,.0f} RON"


def evaluate(conn: sqlite3.Connection, s: dict, data_dir: Path, today: date) -> list[Alert]:
    out: list[Alert] = []
    since_id = s.get("since_id")

    if s["large_payment_ron"] and since_id is not None:
        for r in conn.execute(
                """SELECT c.id, c.booking_date, c.amount_ron, c.merchant, c.counterparty_type, c.category,
                          coalesce(t.counterparty_name, '') AS name
                   FROM transactions_clean c JOIN transactions_raw t ON t.id = c.id
                   WHERE c.kind IN ('spending', 'one_off') AND c.amount_ron <= ? AND t.id > ?
                     AND c.booking_date >= ?""",
                (-s["large_payment_ron"], since_id, (today - timedelta(days=7)).isoformat())):
            who = r["merchant"] if r["counterparty_type"] == "merchant" else (r["name"] or "a payee")
            out.append(Alert(f"pay:{r['id']}", "Large payment",
                             f"{_ron(-r['amount_ron'])} to {who} on {r['booking_date']}.",
                             "A payment above your alert amount went out.", "./#tx"))

    if s["balance_below_ron"]:
        total = conn.execute(
            """SELECT sum(b.amount_minor / 100.0) FROM accounts a JOIN balance_snapshots b ON b.account_id = a.id
               WHERE a.active = 1 AND a.role = 'own' AND a.currency = 'RON' AND b.id = (SELECT id FROM balance_snapshots
                     WHERE account_id = a.id ORDER BY fetched_at DESC, id DESC LIMIT 1)""").fetchone()[0]
        if total is not None and total < s["balance_below_ron"]:
            out.append(Alert(f"bal:{today.isoformat()}", "Balance is low",
                             f"Your RON accounts hold {_ron(total)}, below {_ron(s['balance_below_ron'])}.",
                             "Your balance is below your alert amount.", "./#accounts"))

    if s["income_late"]:
        for r in conn.execute("SELECT merchant, next_expected FROM recurring_series "
                              "WHERE direction = 'in' AND status = 'missed'"):
            out.append(Alert(f"inc:{r['merchant']}:{r['next_expected']}", "Income is late",
                             f"Regular income expected around {r['next_expected']} has not arrived.",
                             "A regular income has not arrived on time.", "./#recurring"))

    if s["price_change"]:
        for r in conn.execute(
                "SELECT merchant, last_date, last_amount, previous_amount, currency, price_change_pct "
                "FROM recurring_series WHERE direction = 'out' AND status = 'active' AND price_change_pct IS NOT NULL "
                "AND last_date >= ?", ((today - timedelta(days=10)).isoformat(),)):
            out.append(Alert(f"price:{r['merchant']}:{r['last_date']}", "A regular payment changed",
                             f"{r['merchant']}: {r['previous_amount']:.2f} → {r['last_amount']:.2f} {r['currency']} "
                             f"({r['price_change_pct']:+.0f}%).",
                             "One of your regular payments changed amount.", "./#recurring"))

    if s["consent_expiring"]:
        for r in conn.execute("SELECT institution, valid_until FROM consents WHERE status = 'active'"):
            days = (date.fromisoformat(r["valid_until"][:10]) - today).days
            bucket = next((b for b in (0, 3, 7, 14) if days <= b), None)
            if bucket is not None:
                out.append(Alert(f"consent:{r['institution']}:{r['valid_until'][:10]}:{bucket}",
                                 f"Renew {r['institution']}",
                                 f"Access to {r['institution']} ends in {max(days, 0)} days. "
                                 "Renew it from Accounts.",
                                 f"A bank connection ends in {max(days, 0)} days.", "./#accounts"))

    if s["health"]:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat(timespec="seconds")
        for r in conn.execute(
                """SELECT a.institution, max(coalesce(sr.at, '')) AS last_ok FROM accounts a
                   JOIN consents c ON c.session_id = a.session_id AND c.status = 'active'
                   LEFT JOIN sync_results sr ON sr.account_id = a.id AND sr.skipped IS NULL
                   WHERE a.active = 1 GROUP BY a.institution"""):
            if r["last_ok"] and r["last_ok"] < cutoff:
                out.append(Alert(f"stale:{r['institution']}:{today.isoformat()}", f"{r['institution']} is not syncing",
                                 f"No successful sync from {r['institution']} for over two days.",
                                 "A bank has not synced for over two days.", "./#accounts"))
        backups = data_dir / "backups"
        snaps = sorted(p.name for p in backups.iterdir() if p.is_dir() and len(p.name) == 16) \
            if backups.exists() else []
        if snaps and snaps[-1] < (datetime.now(timezone.utc) - timedelta(hours=48)).strftime("%Y%m%dT%H%M%SZ"):
            out.append(Alert(f"backup:{today.isoformat()}", "Backups stopped",
                             "The last local backup is more than two days old.",
                             "The last local backup is more than two days old.", "./#accounts"))
    return out
