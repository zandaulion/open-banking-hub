"""Consent linking and data sync, from Enable Banking into the raw tables."""

import json
import re
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

from .db import transaction, utcnow
from .enablebanking import EnableBanking, EnableBankingError
from .normalize import normalize, to_minor

# Banks that cap a transaction list without offering a continuation key. A full answer holds the oldest
# transactions of the range asked for, so they are fetched going forward from the last day reached.
WINDOWED = {"BRD": 20}
BACKFILL_DAYS = 730
MAX_SPAN_DAYS = 364            # BRD rejects a single request spanning more than a year
WINDOWED_REQUESTS_PER_RUN = 3  # transaction requests per account and sync, besides the balance
# Unattended access (no customer present) reaches back 90 days at most; ING refuses anything older.
RECENT_DAYS = 89

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
        twin = None
        if not exists and not (n.entry_reference or "").startswith("stmt-"):
            # A payment added from a statement before the sync got to it: the bank's own record replaces it.
            twin = conn.execute(
                "SELECT id FROM transactions_raw WHERE account_id=? AND entry_reference LIKE 'stmt-%' AND amount_minor=?"
                " AND booking_date BETWEEN date(?, '-2 days') AND date(?, '+2 days')"
                " ORDER BY abs(julianday(booking_date) - julianday(?)) LIMIT 1",
                (account_id, n.amount_minor, n.booking_date, n.booking_date, n.booking_date)).fetchone() \
                if n.booking_date else None
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
        row_id = conn.execute("SELECT id FROM transactions_raw WHERE account_id=? AND dedup_key=?",
                              (account_id, n.dedup_key)).fetchone()[0]
        seen.add(row_id)
        if twin:
            conn.execute("UPDATE OR IGNORE transaction_overrides SET raw_id=? WHERE raw_id=?", (row_id, twin[0]))
            conn.execute("DELETE FROM transaction_overrides WHERE raw_id=?", (twin[0],))
            conn.execute("DELETE FROM transfer_pairs WHERE debit_id=? OR credit_id=?", (twin[0], twin[0]))
            conn.execute("DELETE FROM transactions_raw WHERE id=?", (twin[0],))
            new -= 1                     # it was already counted when the statement added it
    # Pending rows not returned again have settled (under a new reference) or been cancelled.
    stale = [] if not prune_pending else \
        [r[0] for r in conn.execute("SELECT id FROM transactions_raw WHERE account_id=? AND status='PDNG'",
                                    (account_id,)) if r[0] not in seen]
    for raw_id in stale:
        conn.execute("DELETE FROM transaction_overrides WHERE raw_id=?", (raw_id,))
        conn.execute("DELETE FROM transfer_pairs WHERE debit_id=? OR credit_id=?", (raw_id, raw_id))
        conn.execute("DELETE FROM transactions_raw WHERE id=?", (raw_id,))
    return new


def _day(t: dict) -> date:
    return date.fromisoformat((t.get("booking_date") or t.get("value_date") or t.get("transaction_date"))[:10])


def fetch_forward(eb: EnableBanking, uid: str, start: date, end: date, cap: int, budget: list[int], save) -> bool:
    """Fetches start..end from a bank that silently truncates long lists, a year at most per request.
    A full answer is taken to hold the oldest transactions of the range (BRD lists them oldest first), so
    the next request starts at the last day it reached; `save(txs, complete_until)` keeps each answer
    with the first day not yet complete. A full answer listed newest first halves the range instead.
    Returns whether the whole range was fetched within the request budget."""
    cursor = start
    while cursor <= end:
        stop = min(end, cursor + timedelta(days=MAX_SPAN_DAYS))
        while cursor <= stop:
            if budget[0] <= 0:
                return False
            budget[0] -= 1
            txs = list(eb.transactions(uid, date_from=cursor.isoformat(), date_to=stop.isoformat()))
            days = [_day(t) for t in txs]
            if len(txs) < cap:
                save(txs, stop + timedelta(days=1))
                cursor = stop + timedelta(days=1)
            elif days[0] > days[-1] and stop > cursor:
                # Listed newest first, so the missing part could be anywhere before: ask for half the range.
                save(txs, cursor)
                stop = cursor + (stop - cursor) / 2
            else:
                # The last day reached may continue past the cap: fetch it again (duplicates are dropped).
                reached = max(days)
                nxt = reached if reached > cursor else cursor + timedelta(days=1)
                save(txs, nxt)
                cursor = nxt
    return True


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
    # Banks fetched in windows spend a few requests per sync on purpose; their own limit stops them, and the
    # progress is kept.
    if not backfill and not force and account["institution"] not in WINDOWED \
            and requests_last_24h(conn, account["id"]) >= DAILY_SYNC_LIMIT:
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
            start = max(date.fromisoformat(last) - timedelta(days=OVERLAP_DAYS),
                        date.today() - timedelta(days=RECENT_DAYS)).isoformat() if last else None
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
    """The current month first, then the recent days before it, then older history going forward from two
    years back, a few requests per sync. Every answer is saved with how far it got, so a rate limit or the request budget only
    pauses the work until the next sync."""
    today = date.today()
    counts = [0, 0]
    budget = [WINDOWED_REQUESTS_PER_RUN]
    acc_id = account["id"]

    def state():
        return conn.execute("SELECT history_from, backfill_from, backfill_until, recent_until, recent_cursor"
                            " FROM accounts WHERE id=?", (acc_id,)).fetchone()

    def save(column, recent):
        def _save(txs, complete_until):
            with transaction(conn):
                # Pending payments not returned again have settled or gone; only an answer that reaches today
                # can tell (an earlier slice of the month simply does not include them).
                counts[1] += _store_transactions(conn, acc_id, txs, prune_pending=recent and complete_until > today)
                if column:
                    conn.execute(f"UPDATE accounts SET {column}=? WHERE id=?", (complete_until.isoformat(), acc_id))
            counts[0] += len(txs)
        return _save

    row = state()
    if row["recent_cursor"]:
        start = date.fromisoformat(row["recent_cursor"])        # carry on where the last sync stopped
    else:
        earliest = today - timedelta(days=RECENT_DAYS)
        # From the last complete sync (a quiet account has no recent transaction to go by).
        since = row["recent_until"] or last
        wanted = date.fromisoformat(since) - timedelta(days=OVERLAP_DAYS) if since else earliest
        start = max(wanted, earliest)
        with transaction(conn):
            if row["history_from"] is None or wanted < earliest:
                # First sync, or a gap older than unattended access reaches: the backfill fills it in.
                conn.execute("UPDATE accounts SET history_from=? WHERE id=?", (start.isoformat(), acc_id))
                if row["backfill_until"] and wanted < date.fromisoformat(row["backfill_until"]):
                    conn.execute("UPDATE accounts SET backfill_until=? WHERE id=?",
                                 (max(wanted.isoformat(), row["backfill_from"]), acc_id))
            conn.execute("UPDATE accounts SET recent_cursor=? WHERE id=?", (start.isoformat(), acc_id))
    row = state()
    if row["backfill_from"] is None:
        first = (today - timedelta(days=BACKFILL_DAYS)).isoformat()
        conn.execute("UPDATE accounts SET backfill_from=?, backfill_until=? WHERE id=?", (first, first, acc_id))
        row = state()

    # The current month comes first, so the app is up to date as soon as possible; days before it that the
    # last syncs did not reach are filled in after it, with what is left of the request budget.
    month_start = today.replace(day=1) - timedelta(days=OVERLAP_DAYS)
    head = [start < month_start]
    # Within the month, start a few days before the newest payment the bank already gave us, so a sync cut short
    # by the bank's limit carries on instead of asking for the same first page again.
    newest = conn.execute("SELECT max(booking_date) FROM transactions_raw WHERE account_id=? AND status='BOOK'"
                          " AND coalesce(entry_reference, '') NOT LIKE 'stmt-%'", (acc_id,)).fetchone()[0]
    head_from = max(month_start, date.fromisoformat(newest) - timedelta(days=3)) if newest else month_start

    def current():
        if head[0] and not fetch_forward(eb, account["uid"], head_from, today, cap, budget, save(None, True)):
            head[0] = None                                   # not finished: the gap waits for the next sync

    def recent():
        if head[0] is None:
            return
        end = month_start - timedelta(days=1) if head[0] else today
        if fetch_forward(eb, account["uid"], start, end, cap, budget, save("recent_cursor", True)):
            conn.execute("UPDATE accounts SET recent_until=?, recent_cursor=NULL WHERE id=?", (today.isoformat(), acc_id))

    def older():
        until, history = date.fromisoformat(state()["backfill_until"]), date.fromisoformat(state()["history_from"])
        if until < history and budget[0] > 0:
            fetch_forward(eb, account["uid"], until, history - timedelta(days=1), cap, budget, save("backfill_until", False))

    # Right after a new consent the bank still answers for old dates without the customer; later it
    # refuses them. So a fresh link spends its requests on older history first: the recent days are
    # always available, and the next sync fetches them.
    for step in ((older, current, recent) if backfill else (current, recent, older)):
        try:
            step()
        except EnableBankingError as e:
            # The bank's limit part-way: what arrived is saved, so this sync counts; the rest waits.
            if counts[0] and ("RATE_LIMIT" in str(e) or e.status == 429):
                break
            raise
    return tuple(counts)


def backfill_done(account) -> bool:
    return bool(account["backfill_until"] and account["history_from"]
                and account["backfill_until"] >= account["history_from"])


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
                r = sync_account(conn, eb, acc, backfill=acc["backfilled_at"] is None, force=force)
            except EnableBankingError as e:
                print(f"{acc['alias']}: {e}", file=sys.stderr)   # the bank's reason, for the journal
                limited = "RATE_LIMIT" in str(e) or e.status == 429
                r = AccountResult(alias=acc["alias"], skipped=(
                    "the bank's request limit was reached; it continues at the next sync" if limited
                    else f"bank error {e.status}"))
            out.accounts.append(r)
            conn.execute("INSERT INTO sync_results(run_id, account_id, at, fetched, new, skipped) VALUES (?,?,?,?,?,?)",
                         (run, acc["id"], utcnow(), r.fetched, r.new, r.skipped))
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
