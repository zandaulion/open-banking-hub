"""Storing statements: a card becomes an account of its own; a current account statement fills the gaps in a
linked account's history without duplicating what the sync already fetched."""

import hashlib
import json
import sqlite3
from datetime import date, timedelta

from .. import sync
from ..db import transaction, utcnow
from .base import AccountLine, AccountStatement, Line, Statement, StatementError


def _find_account(conn: sqlite3.Connection, st: AccountStatement):
    """The linked account a current account statement belongs to: by IBAN, or by alias for a CSV."""
    rows = conn.execute("SELECT id, alias, own_ibans, coalesce(display_name, alias) AS name FROM accounts"
                        " WHERE source = 'bank' AND active = 1").fetchall()
    found = next((r for r in rows if (st.iban and st.iban in json.loads(r["own_ibans"] or "[]"))
                  or (st.alias and r["alias"] == st.alias)), None)
    if not found:
        raise StatementError("This statement is for an account that is not linked. Link the bank first.")
    return found


def _account_tx(ln: AccountLine, ref: str, currency: str) -> dict:
    """A current account line in the shape the bank API uses, so it is stored and read like any other payment."""
    debit = ln.amount_minor < 0
    cents = abs(ln.amount_minor)
    nobody, none = {"name": None}, {"iban": None, "other": None}
    party = {"name": ln.counterparty}
    account = {"iban": ln.iban, "other": None} if ln.iban else none
    return {"entry_reference": ref, "status": "BOOK", "booking_date": ln.date, "value_date": ln.value_date,
            "credit_debit_indicator": "DBIT" if debit else "CRDT",
            "transaction_amount": {"amount": f"{cents // 100}.{cents % 100:02d}", "currency": currency},
            "creditor": party if debit else nobody, "creditor_account": account if debit else none,
            "debtor": nobody if debit else party, "debtor_account": none if debit else account,
            "remittance_information": [ln.text], "bank_transaction_code": {}}



def _card_account(conn: sqlite3.Connection, st: Statement) -> int:
    ident = hashlib.sha256(f"{st.bank}:{st.iban}".encode()).hexdigest()[:32]
    row = conn.execute("SELECT id FROM accounts WHERE identity_hash=?", (ident,)).fetchone()
    if row:
        return row["id"]
    session = f"statement:{ident}"
    conn.execute("INSERT INTO consents(session_id, institution, country, valid_until, created_at, status)"
                 " VALUES (?,?,?,?,?,'statement')", (session, st.bank, "RO", "9999-12-31T00:00:00Z", utcnow()))
    taken = {r[0] for r in conn.execute("SELECT alias FROM accounts")}
    alias, n = f"{st.bank.lower()}_card", 2
    while alias in taken:
        alias, n = f"{st.bank.lower()}_card_{n}", n + 1
    return conn.execute(
        "INSERT INTO accounts(uid, session_id, institution, alias, currency, cash_account_type, identity_hash, own_ibans,"
        " backfilled_at, display_name, source) VALUES (?,?,?,?,?,?,?,?,?,?,'statement')",
        (session, session, st.bank, alias, "RON", "CARD", ident, json.dumps([st.iban]), utcnow(),
         f"{st.bank} credit card")).lastrowid


def _eb(ln: Line, ref: str) -> dict:
    """A statement line in the shape the bank API uses, so it is stored and read like any other payment."""
    debit = ln.amount_minor > 0
    cents = abs(ln.amount_minor)
    nobody = {"name": None}
    none = {"iban": None, "other": None}
    return {
        "entry_reference": ref,
        "transaction_amount": {"amount": f"{cents // 100}.{cents % 100:02d}", "currency": "RON"},
        "credit_debit_indicator": "DBIT" if debit else "CRDT", "status": "BOOK",
        "booking_date": ln.date, "value_date": ln.settled,
        "creditor": {"name": ln.merchant} if debit else nobody, "creditor_account": none,
        "debtor": nobody if debit else {"name": ln.merchant},
        "debtor_account": {"iban": ln.iban, "other": None} if ln.iban else none,
        "remittance_information": [ln.text or ln.kind], "bank_transaction_code": {},
    }


def store_statement(conn: sqlite3.Connection, st: Statement) -> dict:
    """Adds a statement. Adding the same one again changes nothing."""
    with transaction(conn):
        acc = _card_account(conn, st)
        seen: dict[tuple, int] = {}
        txs = []
        for ln in st.lines:
            # Identical lines on one day (two 5 RON rides) are told apart by their order in the statement.
            basis = (ln.date, ln.amount_minor, ln.merchant, ln.text)
            seen[basis] = seen.get(basis, 0) + 1
            digest = hashlib.sha256(json.dumps([st.iban, *basis, seen[basis]]).encode()).hexdigest()[:24]
            txs.append(_eb(ln, f"stmt-{digest}"))
        new = sync._store_transactions(conn, acc, txs, prune_pending=False)
        old = conn.execute("SELECT id FROM card_statements WHERE account_id=? AND period_end=?",
                           (acc, st.period_end)).fetchone()
        if old:
            conn.execute("DELETE FROM card_plans WHERE statement_id=?", (old["id"],))
            conn.execute("DELETE FROM card_statements WHERE id=?", (old["id"],))
        sid = conn.execute(
            """INSERT INTO card_statements(account_id, period_start, period_end, owed_minor, limit_minor, available_minor,
                   total_due_minor, min_due_minor, due_date, installments_due_minor, installments_future_minor, imported_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (acc, st.period_start, st.period_end, st.owed_minor, st.limit_minor, st.available_minor, st.total_due_minor,
             st.min_due_minor, st.due_date, st.installments_due_minor, st.installments_future_minor, utcnow())).lastrowid
        conn.executemany(
            "INSERT INTO card_plans(statement_id, merchant, started, initial_minor, installment_minor, number, remaining_minor)"
            " VALUES (?,?,?,?,?,?,?)",
            [(sid, p.merchant, p.started, p.initial_minor, p.installment_minor, p.number, p.remaining_minor) for p in st.plans])
        if st.owed_minor is not None:
            # What is owed is a negative balance; the latest statement is the one shown.
            stamp = f"{st.period_end}T23:59:59+00:00"
            conn.execute("DELETE FROM balance_snapshots WHERE account_id=? AND fetched_at=? AND balance_type='STMT'", (acc, stamp))
            conn.execute("INSERT INTO balance_snapshots(account_id, fetched_at, balance_type, amount_minor, currency)"
                         " VALUES (?,?,?,?,?)", (acc, stamp, "STMT", -st.owed_minor, "RON"))
    return {"period_start": st.period_start, "period_end": st.period_end, "lines": len(st.lines), "new": new,
            "already_had": len(st.lines) - new, "warnings": st.warnings}


def store_account_statement(conn: sqlite3.Connection, st: AccountStatement) -> dict:
    """Adds the payments of a current account statement that the account does not have yet. A payment the sync
    already fetched (same amount, within two days) is left alone; the sync later replaces what is added here."""
    acc = _find_account(conn, st)
    with transaction(conn):
        lo = (date.fromisoformat(st.period_start) - timedelta(days=2)).isoformat()
        hi = (date.fromisoformat(st.period_end) + timedelta(days=2)).isoformat()
        pool = [dict(r) for r in conn.execute(
            "SELECT id, booking_date, amount_minor FROM transactions_raw WHERE account_id=? AND booking_date BETWEEN ? AND ?",
            (acc["id"], lo, hi))]
        taken: set[int] = set()

        def match(line: AccountLine, slack: int) -> bool:
            day = date.fromisoformat(line.date)
            for r in pool:
                if r["id"] not in taken and r["amount_minor"] == line.amount_minor \
                        and abs((date.fromisoformat(r["booking_date"]) - day).days) <= slack:
                    taken.add(r["id"])
                    return True
            return False

        # Same day first, so that two equal payments on neighbouring days pair up correctly.
        rest = [ln for ln in st.lines if not match(ln, 0)]
        missing = [ln for ln in rest if not match(ln, 2)]
        seen: dict[tuple, int] = {}
        txs = []
        for ln in missing:
            basis = (ln.date, ln.amount_minor, ln.text, ln.counterparty)
            seen[basis] = seen.get(basis, 0) + 1
            digest = hashlib.sha256(json.dumps([st.iban or st.alias, *basis, seen[basis]]).encode()).hexdigest()[:24]
            txs.append(_account_tx(ln, f"stmt-{digest}", st.currency))
        new = sync._store_transactions(conn, acc["id"], txs, prune_pending=False)
    return {"kind": "account", "account": acc["name"], "period_start": st.period_start, "period_end": st.period_end,
            "lines": len(st.lines), "new": new, "already_had": len(st.lines) - new, "warnings": st.warnings}


def store(conn: sqlite3.Connection, st: Statement | AccountStatement) -> dict:
    if isinstance(st, AccountStatement):
        return store_account_statement(conn, st)
    result = store_statement(conn, st)
    name = conn.execute("SELECT coalesce(display_name, alias) FROM accounts WHERE source='statement'"
                        " AND identity_hash=?", (hashlib.sha256(f"{st.bank}:{st.iban}".encode()).hexdigest()[:32],)).fetchone()
    return {"kind": "card", "account": name[0] if name else None, **result}
