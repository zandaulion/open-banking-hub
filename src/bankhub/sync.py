"""Consent linking and data sync, from Enable Banking into the raw tables."""

import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

from .db import transaction, utcnow
from .enablebanking import EnableBanking, EnableBankingError
from .normalize import normalize, to_minor

# Banks that cap a transaction list without offering a continuation key: fetch them in date windows,
# splitting any window that comes back full.
WINDOWED = {"BRD": 20}
BACKFILL_DAYS = 730      # BRD rejects a single request spanning more than about a year
WINDOW_DAYS = 90
BACKFILL_WINDOWS_PER_RUN = 1   # older history, one window per sync, within the bank's daily request limit

# PSD2 allows about 4 unattended requests per account per day; keep headroom.
DAILY_SYNC_LIMIT = 4
OVERLAP_DAYS = 10


@dataclass
class AccountResult:
    alias: str
    fetched: int = 0
    new: int = 0
    skipped: str | None = None


@dataclass
class SyncResult:
    accounts: list[AccountResult] = field(default_factory=list)


def extract_code(code_or_url: str) -> str:
    """Accepts the bare code or the whole redirect URL pasted from the browser."""
    text = code_or_url.strip()
    if "code=" in text:
        return parse_qs(urlparse(text).query)["code"][0]
    return text


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def _alias(conn: sqlite3.Connection, institution: str, acc: dict) -> str:
    base = f"{_slug(institution)}_{(acc.get('currency') or 'xxx').lower()}"
    if acc.get("cash_account_type") == "SVGS":
        base += "_savings"
    taken = {r[0] for r in conn.execute("SELECT alias FROM accounts")}
    alias, n = base, 2
    while alias in taken:
        alias, n = f"{base}_{n}", n + 1
    return alias


def _own_ibans(acc: dict) -> list[str]:
    ids = [acc.get("account_id") or {}] + list(acc.get("all_account_ids") or [])
    return sorted({i.get("iban") for i in ids if i.get("iban")} |
                  {i.get("identification") for i in ids if i.get("scheme_name") == "IBAN" and i.get("identification")})


def register_session(conn: sqlite3.Connection, session: dict, country: str = "RO") -> list[int]:
    """Stores a new consent and its accounts. Re-consents keep existing aliases and history."""
    institution = session["aspsp"]["name"]
    ids = []
    with transaction(conn):
        conn.execute("UPDATE consents SET status='replaced' WHERE institution=? AND status='active'", (institution,))
        conn.execute(
            "INSERT INTO consents(session_id, institution, country, valid_until, created_at) VALUES (?,?,?,?,?)",
            (session["session_id"], institution, country, session["access"]["valid_until"], utcnow()))
        for acc in session.get("accounts", []):
            ident = acc.get("identification_hash")
            row = conn.execute("SELECT id FROM accounts WHERE identity_hash=?", (ident,)).fetchone() if ident else None
            if row:
                conn.execute("UPDATE accounts SET uid=?, session_id=?, active=1, backfilled_at=NULL WHERE id=?",
                             (acc["uid"], session["session_id"], row["id"]))
                ids.append(row["id"])
            else:
                cur = conn.execute(
                    "INSERT INTO accounts(uid, session_id, institution, alias, currency, cash_account_type,"
                    " identity_hash, own_ibans) VALUES (?,?,?,?,?,?,?,?)",
                    (acc["uid"], session["session_id"], institution, _alias(conn, institution, acc),
                     acc.get("currency") or "XXX", acc.get("cash_account_type"), ident,
                     json.dumps(_own_ibans(acc))))
                ids.append(cur.lastrowid)
    return ids


def requests_last_24h(conn: sqlite3.Connection, account_id: int) -> int:
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(timespec="seconds")
    return conn.execute(
        "SELECT COUNT(*) FROM api_requests WHERE account_id=? AND kind='transactions' AND ts>=?",
        (account_id, since)).fetchone()[0]


def _store_transactions(conn: sqlite3.Connection, account_id: int, txs: list[dict],
                        prune_pending: bool = True) -> int:
    institution = conn.execute("SELECT institution FROM accounts WHERE id=?", (account_id,)).fetchone()[0]
    new = 0
    now = utcnow()
    seen: set[int] = set()
    for tx in txs:
        n = normalize(tx, institution)
        if not n.booking_date:
            continue
        exists = conn.execute("SELECT 1 FROM transactions_raw WHERE account_id=? AND dedup_key=?",
                              (account_id, n.dedup_key)).fetchone()
        conn.execute(
            """INSERT INTO transactions_raw(account_id, dedup_key, entry_reference, status, booking_date, value_date,
                   amount_minor, currency, counterparty_name, counterparty_iban, counterparty_has_account,
                   remittance, payload, first_seen, last_seen, bank_code)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(account_id, dedup_key) DO UPDATE SET
                   status=excluded.status, booking_date=excluded.booking_date, value_date=excluded.value_date,
                   amount_minor=excluded.amount_minor, currency=excluded.currency,
                   counterparty_name=excluded.counterparty_name, counterparty_iban=excluded.counterparty_iban,
                   counterparty_has_account=excluded.counterparty_has_account, remittance=excluded.remittance,
                   payload=excluded.payload, last_seen=excluded.last_seen, bank_code=excluded.bank_code""",
            (account_id, n.dedup_key, n.entry_reference, n.status, n.booking_date, n.value_date, n.amount_minor,
             n.currency, n.counterparty_name, n.counterparty_iban, int(n.counterparty_has_account), n.remittance,
             n.payload, now, now, n.bank_code))
        new += 0 if exists else 1
        seen.add(conn.execute("SELECT id FROM transactions_raw WHERE account_id=? AND dedup_key=?",
                              (account_id, n.dedup_key)).fetchone()[0])
    # Pending rows not returned again have settled (under a new reference) or been cancelled.
    stale = [] if not prune_pending else \
        [r[0] for r in conn.execute("SELECT id FROM transactions_raw WHERE account_id=? AND status='PDNG'",
                                    (account_id,)) if r[0] not in seen]
    for raw_id in stale:
        conn.execute("DELETE FROM transaction_overrides WHERE raw_id=?", (raw_id,))
        conn.execute("DELETE FROM transfer_pairs WHERE debit_id=? OR credit_id=?", (raw_id, raw_id))
        conn.execute("DELETE FROM transactions_raw WHERE id=?", (raw_id,))
    return new


def fetch_window(eb: EnableBanking, uid: str, start: date, end: date, cap: int) -> list[dict]:
    """All transactions between start and end from a bank that silently truncates long lists
    (a full page means there may be more: split the window and ask again)."""
    out: list[dict] = []
    stack = [(start, end)]
    while stack:
        a, b = stack.pop()
        txs = list(eb.transactions(uid, date_from=a.isoformat(), date_to=b.isoformat()))
        if len(txs) >= cap and b > a:
            mid = a + (b - a) / 2
            stack += [(a, mid), (mid + timedelta(days=1), b)]
            continue
        out.extend(txs)
    return out


def _record(conn: sqlite3.Connection, account_id: int, calls: int, balance_calls: int, ts: str) -> None:
    conn.executemany("INSERT INTO api_requests(ts, account_id, kind) VALUES (?,?,?)",
                     [(ts, account_id, "transactions")] * calls + [(ts, account_id, "balances")] * balance_calls)


def _store_balances(conn, account, balances, ts) -> None:
    for b in balances:
        amt = b.get("balance_amount") or {}
        conn.execute(
            "INSERT INTO balance_snapshots(account_id, fetched_at, balance_type, amount_minor, currency,"
            " reference_date) VALUES (?,?,?,?,?,?)",
            (account["id"], ts, b.get("balance_type") or "?", to_minor(amt.get("amount", 0)),
             amt.get("currency") or account["currency"], b.get("reference_date")))


def sync_account(conn: sqlite3.Connection, eb: EnableBanking, account: sqlite3.Row, *,
                 backfill: bool, force: bool = False) -> AccountResult:
    result = AccountResult(alias=account["alias"])
    if not backfill and not force and requests_last_24h(conn, account["id"]) >= DAILY_SYNC_LIMIT:
        result.skipped = f"daily limit of {DAILY_SYNC_LIMIT} requests reached"
        return result
    run_started = utcnow()
    cap = WINDOWED.get(account["institution"])
    last = conn.execute("SELECT MAX(booking_date) FROM transactions_raw WHERE account_id=? AND status='BOOK'",
                        (account["id"],)).fetchone()[0]
    calls_before, balances_before = eb.data_calls, eb.balance_calls
    try:
        balances = eb.balances(account["uid"])
        if cap:
            result.fetched, result.new = _sync_windowed(conn, eb, account, cap, backfill, last)
            with transaction(conn):
                _store_balances(conn, account, balances, run_started)
                if backfill:
                    conn.execute("UPDATE accounts SET backfilled_at=? WHERE id=?", (run_started, account["id"]))
            return result
        if backfill:
            txs = list(eb.transactions(account["uid"], longest=True))
        else:
            start = (date.fromisoformat(last) - timedelta(days=OVERLAP_DAYS)).isoformat() if last else None
            txs = list(eb.transactions(account["uid"], date_from=start, longest=start is None))
        with transaction(conn):
            _store_balances(conn, account, balances, run_started)
            result.new = _store_transactions(conn, account["id"], txs)
            if backfill:
                conn.execute("UPDATE accounts SET backfilled_at=? WHERE id=?", (run_started, account["id"]))
        result.fetched = len(txs)
        return result
    finally:
        calls, bcalls = eb.data_calls - calls_before, eb.balance_calls - balances_before
        if calls or bcalls:
            with transaction(conn):
                _record(conn, account["id"], calls, bcalls, run_started)


def _sync_windowed(conn, eb, account, cap, backfill, last) -> tuple[int, int]:
    """Recent transactions, then one older window of history; each window is saved as soon as it arrives,
    so a rate limit part-way keeps everything fetched so far."""
    today = date.today()
    fetched = new = 0
    recent_from = date.fromisoformat(last) - timedelta(days=OVERLAP_DAYS) if last else today - timedelta(days=WINDOW_DAYS)
    txs = fetch_window(eb, account["uid"], max(recent_from, today - timedelta(days=BACKFILL_DAYS)), today, cap)
    with transaction(conn):
        new += _store_transactions(conn, account["id"], txs)
        if account["history_from"] is None:
            conn.execute("UPDATE accounts SET history_from=? WHERE id=?", (recent_from.isoformat(), account["id"]))
    fetched += len(txs)
    oldest_wanted = today - timedelta(days=BACKFILL_DAYS)
    history_from = date.fromisoformat(
        conn.execute("SELECT history_from FROM accounts WHERE id=?", (account["id"],)).fetchone()[0])
    for _ in range(BACKFILL_WINDOWS_PER_RUN):
        if history_from <= oldest_wanted:
            break
        a = max(oldest_wanted, history_from - timedelta(days=WINDOW_DAYS))
        txs = fetch_window(eb, account["uid"], a, history_from - timedelta(days=1), cap)
        with transaction(conn):
            new += _store_transactions(conn, account["id"], txs, prune_pending=False)
            conn.execute("UPDATE accounts SET history_from=? WHERE id=?", (a.isoformat(), account["id"]))
        fetched += len(txs)
        history_from = a
    return fetched, new


def sync_all(conn: sqlite3.Connection, eb: EnableBanking, *, force: bool = False) -> SyncResult:
    out = SyncResult()
    run = conn.execute("INSERT INTO sync_runs(started_at, kind) VALUES (?, 'incremental')", (utcnow(),)).lastrowid
    try:
        now = utcnow()
        accounts = conn.execute(
            "SELECT a.* FROM accounts a JOIN consents c ON c.session_id=a.session_id"
            " WHERE a.active=1 AND c.status='active' AND c.valid_until>? ORDER BY a.alias", (now,)).fetchall()
        for acc in accounts:
            try:
                out.accounts.append(sync_account(conn, eb, acc, backfill=acc["backfilled_at"] is None, force=force))
            except EnableBankingError as e:
                limited = "RATE_LIMIT" in str(e) or e.status == 429
                out.accounts.append(AccountResult(alias=acc["alias"], skipped=(
                    "the bank's request limit was reached; it continues at the next sync" if limited
                    else f"bank error {e.status}")))
        conn.execute("UPDATE sync_runs SET finished_at=?, status='ok', requests=? WHERE id=?",
                     (utcnow(), eb.data_calls, run))
    except Exception as e:
        conn.execute("UPDATE sync_runs SET finished_at=?, status='error', error=? WHERE id=?",
                     (utcnow(), type(e).__name__ + ": " + str(e)[:300], run))
        raise
    return out


def expire_consents(conn: sqlite3.Connection) -> None:
    conn.execute("UPDATE consents SET status='expired' WHERE status='active' AND valid_until<=?", (utcnow(),))


def renormalize(conn: sqlite3.Connection) -> int:
    """Re-derives the canonical fields of every stored transaction from its payload (after parser changes).
    Keys are left alone, so overrides and transfer decisions stay attached."""
    rows = conn.execute("""SELECT t.id, t.payload, a.institution FROM transactions_raw t
                           JOIN accounts a ON a.id = t.account_id""").fetchall()
    with transaction(conn):
        for raw_id, payload, institution in rows:
            n = normalize(json.loads(payload), institution)
            conn.execute(
                """UPDATE transactions_raw SET counterparty_name=?, counterparty_iban=?, counterparty_has_account=?,
                       remittance=?, bank_code=? WHERE id=?""",
                (n.counterparty_name, n.counterparty_iban, int(n.counterparty_has_account), n.remittance,
                 n.bank_code, raw_id))
    return len(rows)
