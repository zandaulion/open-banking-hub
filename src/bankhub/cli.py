"""bankhub command line."""

import json
import sqlite3
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path

import typer

from . import fx, ledger, queries, redaction, statements, sync
from .config import load_config
from .db import connect, connect_readonly, transaction, utcnow
from .enablebanking import EnableBanking
from .rules import load_rules

app = typer.Typer(no_args_is_help=True, help="Personal Bank Hub: read-only banking data, redacted for AI agents.")
auth_app = typer.Typer(no_args_is_help=True, help="Link a bank (PSD2 consent).")
report_app = typer.Typer(no_args_is_help=True, help="Reports for the two core questions.")
transfers_app = typer.Typer(no_args_is_help=True, help="Review detected transfers between own accounts.")
app.add_typer(auth_app, name="auth")
app.add_typer(report_app, name="report")
app.add_typer(transfers_app, name="transfers")


def _cfg():
    return load_config()


def _db() -> sqlite3.Connection:
    return connect(_cfg().db_path)


def _ro() -> sqlite3.Connection:
    path = _cfg().db_path
    if not path.exists():
        raise SystemExit("No database yet. Run `bankhub auth start <bank>` first.")
    return connect_readonly(path)


def _eb() -> EnableBanking:
    cfg = _cfg()
    return EnableBanking(cfg.app_id, cfg.key_path)


def _money(v: float | None) -> str:
    return "—" if v is None else f"{v:,.2f}"


def _alerts():
    from .web.alerts import Alerts
    from .web.auth import AuthStore
    cfg = _cfg()
    AuthStore(cfg.data_dir / "web.db")      # makes sure the devices table exists
    return Alerts(cfg.data_dir / "web.db", cfg.data_dir)


def _run_alerts(conn: sqlite3.Connection) -> None:
    try:
        sent = _alerts().run(conn)
        if sent:
            typer.echo(f"Alerts: {len(sent)} new")
    except Exception as e:   # an alert problem must never break a sync
        typer.echo(f"Alerts skipped: {type(e).__name__}: {e}")


def _rebuild(conn: sqlite3.Connection) -> dict:
    fx.ensure_rates(conn)
    return ledger.rebuild(conn, load_rules(_cfg().rules_path), one_off_ron=_cfg().one_off_ron)


# --- linking ------------------------------------------------------------------------

@app.command()
def banks():
    """List banks available for linking."""
    cfg = _cfg()
    with _eb() as eb:
        for a in eb.aspsps(cfg.country):
            typer.echo(f"{a['name']:<45} max consent {(a.get('maximum_consent_validity') or 0) // 86400} days")


@auth_app.command("start")
def auth_start(bank: str):
    """Print the consent URL for BANK (open it in a real browser, not an in-app one)."""
    cfg = _cfg()
    with _eb() as eb:
        match = [a for a in eb.aspsps(cfg.country) if a["name"].lower() == bank.lower()]
        if not match:
            raise SystemExit(f"No bank named {bank!r}; see `bankhub banks`.")
        url, _, valid_until = eb.start_auth(match[0], cfg.country, cfg.redirect_url)
    typer.echo(f"Consent until {valid_until:%Y-%m-%d}. Open in a browser, approve, then run:\n"
               f"  bankhub auth finish '<the whole URL you land on>'\n\n{url}")


@auth_app.command("finish")
def auth_finish(code_or_url: str):
    """Exchange the code, register accounts and immediately fetch the full history."""
    cfg = _cfg()
    conn = _db()
    with _eb() as eb:
        session = eb.create_session(sync.extract_code(code_or_url))
        ids = sync.register_session(conn, session, cfg.country)
        if not ids:
            typer.echo("The bank returned no accounts. In restricted mode, link this bank's accounts to the "
                       "application in the Enable Banking Control Panel first, then start a new consent.")
            return
        for acc in conn.execute(f"SELECT * FROM accounts WHERE id IN ({','.join('?' * len(ids))})", ids):
            r = sync.sync_account(conn, eb, acc, backfill=True)
            typer.echo(f"  {r.alias}: {r.fetched} transactions")
    typer.echo(f"Rebuilt: {_rebuild(conn)}")


@app.command("import-spike", hidden=True)
def import_spike(spike_dir: Path = typer.Option(None, help="Defaults to the data directory")):
    """Import a Phase 0 spike session and its raw payloads, without new bank requests."""
    cfg = _cfg()
    base = spike_dir or cfg.data_dir
    state = json.loads((base / "spike_state.json").read_text())
    conn = _db()
    sessions = sorted((base / "raw").glob("*_session.json"))
    for s in state["sessions"]:
        payload = next((json.loads(p.read_text()) for p in sessions
                        if json.loads(p.read_text()).get("session_id") == s["session_id"]), None)
        if not payload or not payload.get("accounts"):
            typer.echo(f"  {s['bank']}: no accounts in session, skipped")
            continue
        sync.register_session(conn, payload, cfg.country)
        for acc in payload["accounts"]:
            row = conn.execute("SELECT * FROM accounts WHERE uid=?", (acc["uid"],)).fetchone()
            files = sorted((base / "raw").glob(f"*_{acc['uid'][:8]}_transactions.json"))
            if not files:
                continue
            txs = json.loads(files[-1].read_text())["transactions"]
            started = utcnow()
            with transaction(conn):
                n = sync._store_transactions(conn, row["id"], txs)
                conn.execute("UPDATE accounts SET backfilled_at=? WHERE id=?", (started, row["id"]))
            bal_files = sorted((base / "raw").glob(f"*_{acc['uid'][:8]}_balances.json"))
            if bal_files:
                for b in json.loads(bal_files[-1].read_text()).get("balances", []):
                    amt = b.get("balance_amount") or {}
                    conn.execute("INSERT INTO balance_snapshots(account_id, fetched_at, balance_type, amount_minor,"
                                 " currency, reference_date) VALUES (?,?,?,?,?,?)",
                                 (row["id"], started, b.get("balance_type") or "?",
                                  sync.to_minor(amt.get("amount", 0)), amt.get("currency") or row["currency"],
                                  b.get("reference_date")))
            typer.echo(f"  {row['alias']}: {n} transactions imported")
    typer.echo(f"Rebuilt: {_rebuild(conn)}")


# --- sync & rebuild -------------------------------------------------------------------

@app.command("sync")
def sync_cmd(force: bool = typer.Option(False, help="Ignore the daily request budget (you are present).")):
    """Fetch new transactions and balances for every linked account, then rebuild."""
    conn = _db()
    sync.expire_consents(conn)
    with _eb() as eb:
        result = sync.sync_all(conn, eb, force=force)
    for a in result.accounts:
        typer.echo(f"  {a.alias}: " + (f"skipped ({a.skipped})" if a.skipped else f"{a.fetched} fetched, {a.new} new"))
    typer.echo(f"Rebuilt: {_rebuild(conn)}")
    _run_alerts(conn)
    for b in queries.sync_status(conn)["banks"]:
        if b["renew_soon"]:
            typer.echo(f"WARNING: {b['institution']} consent expires in {b['consent_days_left']} days; "
                       f"run `bankhub auth start {b['institution']}`.")


@app.command()
def rebuild():
    """Re-run FX, transfer detection, categorization and recurring detection (no bank requests)."""
    typer.echo(_rebuild(_db()))


@app.command("import-statement")
def import_statement(files: list[Path]):
    """Add statements (PDF): a credit card the bank does not share, or payments the sync has not fetched yet. Prints counts only."""
    conn = _db()
    for f in files:
        try:
            st = statements.parse_statement(statements.pdf_text(f.read_bytes()))
            r = statements.store(conn, st)
        except statements.StatementError as e:
            typer.echo(f"{f.name}: {e}")
            continue
        typer.echo(f"{f.name}: {r['period_start']} to {r['period_end']}, {r['new']} new of {r['lines']} lines"
                   + "".join(f"\n  warning: {w}" for w in r["warnings"]))
    typer.echo(_rebuild(conn))


@app.command()
def status():
    """Consents, last sync and newest transaction."""
    with closing(_ro()) as conn:
        s = queries.sync_status(conn)
    for b in s["banks"]:
        flag = "  RENEW SOON" if b["renew_soon"] else ""
        typer.echo(f"{b['institution']:<12} consent until {b['consent_valid_until']} ({b['consent_days_left']} days)"
                   f"  last sync {b['last_sync'] or 'never'}{flag}")
    typer.echo(f"Newest transaction: {s['latest_transaction_date']}")
    if s["last_run"] and s["last_run"]["status"] == "error":
        typer.echo(f"Last sync FAILED: {s['last_run']['error']}")


@app.command()
def balances():
    """Latest balances and the consolidated total."""
    with closing(_ro()) as conn:
        b = queries.balances(conn)
    for a in b["accounts"]:
        typer.echo(f"{a['account']:<24} {_money(a['amount']):>12} {a['currency']}  ({_money(a['amount_ron'])} RON)")
    typer.echo(f"{'Total':<24} {_money(b['total_ron']):>12} RON  ({_money(b['total_eur'])} EUR)")


# --- reports ------------------------------------------------------------------------

@report_app.command("month")
def report_month(month: str = typer.Argument(None, help="YYYY-MM; defaults to last month")):
    """Where did my money go: spending by category and top merchants, own transfers excluded."""
    month = month or (date.today().replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
    start, end = queries.month_bounds(month)
    prev = (date.fromisoformat(start) - timedelta(days=1)).strftime("%Y-%m")
    pstart, pend = queries.month_bounds(prev)
    with closing(_ro()) as conn:
        cur = queries.spending_summary(conn, date_from=start, date_to=end, group_by="category")
        old = {g["name"]: g for g in queries.spending_summary(conn, date_from=pstart, date_to=pend)["groups"]}
        merchants = queries.spending_summary(conn, date_from=start, date_to=end, group_by="merchant", top=15)
    typer.echo(f"\n{month}: spent {_money(cur['total_outflow_ron'])} RON, income {_money(cur['income_ron'])} RON,"
               f" other money in {_money(cur['other_money_in_ron'])} RON")
    for o in cur["one_offs"]:
        typer.echo(f"  one-off: {_money(o['amount_ron'])} RON ({o['category']}, {o['booking_date']})")
    ex = cur["internal_transfers_excluded"]
    typer.echo(f"(excluded {ex['transactions']} own-account transfers, {_money(ex['volume_ron'])} RON)\n")
    typer.echo(f"{'Category':<22}{'Spent':>12}{prev:>12}{'Change':>9}")
    for g in cur["groups"]:
        if not g["outflow_ron"]:
            continue
        before = old.get(g["name"], {}).get("outflow_ron") or 0
        change = f"{(g['outflow_ron'] - before) / before * 100:+.0f}%" if before else "new"
        typer.echo(f"{g['name']:<22}{_money(g['outflow_ron']):>12}{_money(before):>12}{change:>9}")
    typer.echo(f"\n{'Top payees':<22}{'Spent':>12}{'Count':>7}")
    for g in merchants["groups"]:
        if g["outflow_ron"]:
            typer.echo(f"{g['name']:<22}{_money(g['outflow_ron']):>12}{g['transactions']:>7}")


@report_app.command("recurring")
def report_recurring():
    """What am I paying regularly: subscriptions and bills, with monthly and annual totals."""
    with closing(_ro()) as conn:
        r = queries.recurring_payments(conn)
    typer.echo(f"\n{'Payee':<22}{'Cadence':<9}{'Last':>10} {'Cur':<4}{'Per month':>11}{'Next':>12}  Notes")
    for s in r["series"]:
        notes = []
        if s["price_change_pct"]:
            notes.append(f"price {s['price_change_pct']:+.0f}%")
        if s["status"] == "missed":
            notes.append("MISSED")
        typer.echo(f"{s['merchant'][:21]:<22}{s['cadence']:<9}{_money(s['last_amount']):>10} {s['currency']:<4}"
                   f"{_money(s['monthly_ron']):>11}{s['next_expected']:>12}  {', '.join(notes)}")
    typer.echo(f"\nActive: {_money(r['active_monthly_total_ron'])} RON/month,"
               f" {_money(r['active_annual_total_ron'])} RON/year")


# --- maintenance (local only: these read the raw layer) ----------------------------

@app.command()
def uncategorized(top: int = 30):
    """Top unrecognised payees by amount, to write new rules. Shows raw names: local terminal only."""
    with closing(_ro()) as conn:
        rows = conn.execute(
            """SELECT coalesce(t.counterparty_name, t.remittance) AS payee, count(*) AS n,
                      round(-sum(c.amount_ron), 2) AS ron
               FROM transactions_clean c JOIN transactions_raw t ON t.id = c.id
               WHERE c.category = 'uncategorized' AND c.direction = 'debit'
               GROUP BY payee ORDER BY ron DESC LIMIT ?""", (top,)).fetchall()
        share = conn.execute(
            """SELECT round(100.0 * sum(CASE WHEN category='uncategorized' THEN -amount_ron END)
                      / sum(-amount_ron), 1) FROM transactions_clean
               WHERE direction='debit' AND is_internal_transfer=0 AND status='booked'""").fetchone()[0]
    typer.echo(f"Uncategorized: {share}% of spending. Add patterns to {_cfg().rules_path} (copy of the defaults).")
    for r in rows:
        typer.echo(f"{r['ron']:>12,.2f} RON {r['n']:>5}x  {r['payee']}")


@app.command("inspect-redaction")
def inspect_redaction(samples: int = typer.Option(5, help="Raw-vs-redacted rows to print (0 for scan only)")):
    """Leak scan of the agent-facing layer, plus side-by-side samples (local terminal only)."""
    rules = load_rules(_cfg().rules_path)
    with closing(_ro()) as conn:
        leaks = redaction.scan(conn, rules)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(transactions_clean)")]
        extra = sorted(set(cols) - set(ledger.CLEAN_COLUMNS))
        if samples:
            for r in conn.execute(
                    """SELECT t.counterparty_name, t.counterparty_iban, t.remittance, c.merchant,
                              c.counterparty_type, c.category
                       FROM transactions_clean c JOIN transactions_raw t ON t.id=c.id
                       ORDER BY random() LIMIT ?""", (samples,)):
                typer.echo(f"RAW   {r[0]!r} {r[1]!r} {r[2]!r}\nCLEAN {r[3]!r} ({r[4]}, {r[5]})\n")
    if extra:
        typer.echo(f"FAIL: columns outside the allowlist: {extra}")
    by_kind: dict[tuple, int] = {}
    for leak in leaks:
        by_kind[(leak.table, leak.column, leak.kind)] = by_kind.get((leak.table, leak.column, leak.kind), 0) + 1
    for (t, c, k), n in sorted(by_kind.items()):
        typer.echo(f"LEAK? {t}.{c}: {n} value(s) look like {k}")
    if not leaks and not extra:
        typer.echo("PASS: no PII patterns or raw counterparty names in the agent-facing layer.")
    raise typer.Exit(1 if leaks or extra else 0)


@transfers_app.command("review")
def transfers_review():
    """List uncertain (amount-matched) transfer pairs."""
    with closing(_ro()) as conn:
        rows = conn.execute(
            """SELECT p.debit_id, p.credit_id, d.account_alias da, c.account_alias ca, d.booking_date dd,
                      c.booking_date cd, d.amount_ron dr, c.amount_ron cr, p.confirmed
               FROM transfer_pairs p JOIN transactions_clean d ON d.id=p.debit_id
               JOIN transactions_clean c ON c.id=p.credit_id
               WHERE p.method='amount_match' ORDER BY d.booking_date DESC""").fetchall()
    if not rows:
        typer.echo("No amount-matched pairs to review.")
    for r in rows:
        state = {1: "confirmed", -1: "rejected", 0: "unreviewed"}[r["confirmed"]]
        typer.echo(f"{r['debit_id']}->{r['credit_id']}  {r['da']} {r['dd']} {_money(r['dr'])}  ->  "
                   f"{r['ca']} {r['cd']} {_money(r['cr'])}  [{state}]")


def _decide(debit_id: int, credit_id: int, value: int):
    conn = _db()
    with transaction(conn):
        conn.execute("""INSERT INTO transfer_pairs(debit_id, credit_id, method, confidence, confirmed)
                        VALUES (?,?,'manual',1.0,?) ON CONFLICT(debit_id, credit_id)
                        DO UPDATE SET confirmed=excluded.confirmed""", (debit_id, credit_id, value))
    typer.echo(f"Rebuilt: {_rebuild(conn)}")


@transfers_app.command("confirm")
def transfers_confirm(debit_id: int, credit_id: int):
    """Mark a pair as a transfer between own accounts."""
    _decide(debit_id, credit_id, 1)


@transfers_app.command("reject")
def transfers_reject(debit_id: int, credit_id: int):
    """Mark a pair as NOT a transfer (both sides count as spending/income again)."""
    _decide(debit_id, credit_id, -1)


@app.command()
def override(transaction_id: int, category: str = typer.Option(None), subcategory: str = typer.Option(None),
             merchant: str = typer.Option(None, help="Name to show; only use names you are happy agents see"),
             internal: bool = typer.Option(None, "--internal/--not-internal")):
    """Manually set category, merchant or the internal-transfer flag of one transaction."""
    conn = _db()
    with transaction(conn):
        conn.execute(
            """INSERT INTO transaction_overrides(raw_id, merchant, category, subcategory, is_internal_transfer,
                   updated_at) VALUES (?,?,?,?,?,?)
               ON CONFLICT(raw_id) DO UPDATE SET merchant=coalesce(excluded.merchant, merchant),
                   category=coalesce(excluded.category, category),
                   subcategory=coalesce(excluded.subcategory, subcategory),
                   is_internal_transfer=coalesce(excluded.is_internal_transfer, is_internal_transfer),
                   updated_at=excluded.updated_at""",
            (transaction_id, merchant, category, subcategory, None if internal is None else int(internal), utcnow()))
    typer.echo(f"Rebuilt: {_rebuild(conn)}")


@app.command()
def mcp():
    """Run the read-only MCP server over stdio."""
    from .mcp_server import main
    main()


@app.command()
def web():
    """Run the owner's PWA (app + admin listeners on loopback)."""
    from .web.server import serve
    serve()


@app.command()
def renormalize():
    """Re-read every stored transaction with the current bank parsers, then rebuild (no bank requests)."""
    conn = _db()
    typer.echo(f"Re-read {sync.renormalize(conn)} transactions.")
    typer.echo(f"Rebuilt: {_rebuild(conn)}")


@app.command()
def backup():
    """Take a validated local snapshot (databases, rules, settings, key) and print its path."""
    from .backup import snapshot
    typer.echo(snapshot(_cfg().data_dir))


# --- first-time setup and devices (no external console needed) -----------------

@app.command()
def init(app_id: str = typer.Option(None, help="Enable Banking application ID, once you have registered it"),
         base_url: str = typer.Option(None, help="Address the app will be reached at, e.g. https://host.ts.net/bank")):
    """Create the private data directory, a signing key and the settings file; print the next steps."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    cfg = _cfg()
    d = cfg.data_dir
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    (d / "secrets").mkdir(mode=0o700, exist_ok=True)
    key = d / "secrets" / "enable_banking.pem"
    if not key.exists():
        k = rsa.generate_private_key(public_exponent=65537, key_size=4096)
        key.write_bytes(k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                        serialization.NoEncryption()))
        key.chmod(0o600)
        typer.echo(f"Created a signing key: {key}")
    env_file = d / ".env"
    env = {}
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k_, v_ = line.split("=", 1)
                env[k_.strip()] = v_.strip()
    env.setdefault("BANKHUB_KEY_PATH", str(key))
    if app_id:
        env["ENABLE_BANKING_APP_ID"] = app_id.strip()
    if base_url:
        env["PUBLIC_BASE_URL"] = base_url.rstrip("/")
        env["BANKHUB_REDIRECT_URL"] = base_url.rstrip("/") + "/callback"
    env_file.write_text("".join(f"{k_}={v_}\n" for k_, v_ in env.items()))
    env_file.chmod(0o600)
    connect(cfg.db_path).close()

    from cryptography.hazmat.primitives.serialization import load_pem_private_key
    public = load_pem_private_key(key.read_bytes(), None).public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    typer.echo(f"Data directory: {d} (private; never commit it)")
    if not env.get("ENABLE_BANKING_APP_ID"):
        typer.echo("\nNext: register a PRODUCTION application in the Enable Banking Control Panel with this public key,\n"
                   "add your redirect URL(s), link your own accounts, then run:\n"
                   "  bankhub init --app-id <the application ID>\n")
        typer.echo(public)
    elif not env.get("PUBLIC_BASE_URL"):
        typer.echo("\nNext: decide the private address of the app and run:  bankhub init --base-url https://.../bank\n"
                   "(register <that address>/callback as a redirect URL in the Control Panel), or link banks from\n"
                   "the terminal with:  bankhub auth start <bank>")
    else:
        typer.echo("\nReady. Start the app with `bankhub web`, publish it on your private network, then create\n"
                   "an invite for your phone with `bankhub invite --label phone`.")


@app.command()
def invite(label: str = typer.Option("Device", help="Name for the device this invite will register")):
    """Create a one-time invitation that registers one device; prints its link."""
    from .web.auth import AuthStore
    from .web.server import load_web_config
    cfg = load_web_config()
    inv = AuthStore(cfg.core.data_dir / "web.db").create_invite(label, cfg.base_url)
    typer.echo(f"{inv['url']}\nCode {inv['code']}, valid until {inv['expires_at'][:10]}, registers one device.")


@app.command()
def devices(revoke: int = typer.Option(None, help="Revoke the device with this ID"),
            restore: int = typer.Option(None, help="Restore access for the device with this ID")):
    """List registered devices, or revoke / restore one."""
    from .web.auth import AuthStore
    store = AuthStore(_cfg().data_dir / "web.db")
    if revoke is not None or restore is not None:
        ok = store.set_device_revoked(revoke if revoke is not None else restore, revoke is not None)
        typer.echo("Done." if ok else "No such device.")
        return
    for dev in store.list_devices():
        state = "revoked" if dev["revoked"] else "active"
        typer.echo(f"{dev['id']:>3}  {dev['label']:<24} {state:<8} last seen {dev['last_seen'] or 'never'}")


@app.command()
def demo(months: int = typer.Option(14, help="How many months of made-up history"),
         force: bool = typer.Option(False, help="Add demo data even if real accounts exist")):
    """Fill the data directory with a year of made-up transactions, to try the app without a bank."""
    from . import demo as demo_data
    cfg = _cfg()
    conn = _db()
    real = conn.execute("SELECT count(*) FROM accounts WHERE uid NOT LIKE 'demo-%'").fetchone()[0]
    if real and not force:
        raise SystemExit("This data directory has real accounts. Use an empty one, e.g. "
                         "BANKHUB_DATA_DIR=~/bankhub-demo bankhub demo")
    n = demo_data.load(conn, months)
    typer.echo(f"Added {n} made-up transactions in {cfg.data_dir}.")
    typer.echo(f"Rebuilt: {_rebuild(conn)}")
    typer.echo("Next: bankhub invite --label demo, then bankhub web, and open the printed link.")


@app.command()
def alerts(send: bool = typer.Option(False, help="Send new alerts now instead of only listing them")):
    """List the alerts that would go out now (they are sent automatically after each sync)."""
    conn = _db()
    found = _alerts().run(conn, send=send)
    if not found:
        typer.echo("Nothing new to alert about.")
    for a in found:
        typer.echo(f"{a.title}: {a.body}")
