"""Detect money moving between my own accounts, so it is not counted as spending or income."""

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import date

from .fx import RateTable
from .rules import Rules

MAX_DAYS_APART = 3
AMOUNT_TOLERANCE = 0.02   # Revolut converts at its own rate and may charge a fee


@dataclass
class TransferResult:
    internal_ids: set[int]
    pairs: list[tuple[int, int, str, float]]   # debit_id, credit_id, method, confidence
    to_dependent: set[int] = field(default_factory=set)     # my money sent to a child's account
    from_dependent: set[int] = field(default_factory=set)   # money from a child's account to mine
    dependent_accounts: set[int] = field(default_factory=set)


def detect(conn: sqlite3.Connection, rules: Rules, rates: RateTable) -> TransferResult:
    rows = conn.execute(
        """SELECT t.id, t.account_id, a.institution, t.entry_reference, t.booking_date, t.amount_minor,
                  t.currency, t.counterparty_iban, t.counterparty_has_account, t.remittance, t.bank_code,
                  t.counterparty_name
           FROM transactions_raw t JOIN accounts a ON a.id = t.account_id
           WHERE t.status = 'BOOK'""").fetchall()
    own_ibans, dep_ibans = set(), set()
    dependent = set()
    for acc_id, ibans, role in conn.execute("SELECT id, own_ibans, role FROM accounts"):
        if role == "dependent":
            dependent.add(acc_id)
            dep_ibans.update(json.loads(ibans or "[]"))
        else:
            own_ibans.update(json.loads(ibans or "[]"))
    rejected = {(r[0], r[1]) for r in conn.execute("SELECT debit_id, credit_id FROM transfer_pairs WHERE confirmed=-1")}

    internal: set[int] = set()
    pairs: list[tuple[int, int, str, float]] = []

    # 1. Same entry reference on two of my accounts, opposite directions (Revolut pocket moves).
    by_ref: dict[tuple[str, str], list[sqlite3.Row]] = {}
    for r in rows:
        if r["entry_reference"]:
            by_ref.setdefault((r["institution"], r["entry_reference"]), []).append(r)
    for group in by_ref.values():
        debits = [r for r in group if r["amount_minor"] < 0]
        credits = [r for r in group if r["amount_minor"] > 0]
        if len(debits) == 1 and len(credits) == 1 and debits[0]["account_id"] != credits[0]["account_id"]:
            pairs.append((debits[0]["id"], credits[0]["id"], "same_reference", 1.0))
            internal.update((debits[0]["id"], credits[0]["id"]))

    # 2. One-sided rules: the counterparty is one of my IBANs, or a known own-account description.
    for r in rows:
        code_rule = rules.match_bank_code(r["bank_code"], r["amount_minor"] < 0)
        if code_rule and code_rule.internal:
            internal.add(r["id"])
        elif r["counterparty_iban"] and r["counterparty_iban"].replace(" ", "") in own_ibans:
            internal.add(r["id"])
        elif not r["counterparty_has_account"] and any(
                rules.is_own_account_move(r["institution"], text) for text in (r["remittance"], r["counterparty_name"]) if text):
            internal.add(r["id"])

    # 3. Amount matching across institutions (e.g. ING -> Revolut top-up).
    candidates = [r for r in rows if r["id"] not in internal]
    debits = [r for r in candidates if r["amount_minor"] < 0]
    credits = [r for r in candidates if r["amount_minor"] > 0]
    used: set[int] = set()
    for d in sorted(debits, key=lambda r: r["booking_date"]):
        d_ron = rates.to_ron(-d["amount_minor"] / 100, d["currency"], d["booking_date"])
        if d_ron is None:
            continue
        d_day = date.fromisoformat(d["booking_date"])
        best = None
        for c in credits:
            if c["id"] in used or c["institution"] == d["institution"] or (d["id"], c["id"]) in rejected:
                continue
            days = (date.fromisoformat(c["booking_date"]) - d_day).days
            if not 0 <= days <= MAX_DAYS_APART:
                continue
            c_ron = rates.to_ron(c["amount_minor"] / 100, c["currency"], c["booking_date"])
            if c_ron is None or abs(c_ron - d_ron) > AMOUNT_TOLERANCE * d_ron:
                continue
            score = days + abs(c_ron - d_ron) / d_ron
            if best is None or score < best[0]:
                best = (score, c)
        if best:
            c = best[1]
            used.add(c["id"])
            pairs.append((d["id"], c["id"], "amount_match", 0.7))
            internal.update((d["id"], c["id"]))

    to_dep, from_dep = _dependent_transfers(rows, pairs, internal, dependent, dep_ibans, rejected)

    # Manual decisions win.
    for debit_id, credit_id in conn.execute("SELECT debit_id, credit_id FROM transfer_pairs WHERE confirmed=1 AND method='manual'"):
        internal.update((debit_id, credit_id))
    for raw_id, flag in conn.execute("SELECT raw_id, is_internal_transfer FROM transaction_overrides"
                                     " WHERE is_internal_transfer IS NOT NULL"):
        (internal.add if flag else internal.discard)(raw_id)
    internal -= to_dep | from_dep
    return TransferResult(internal, pairs, to_dep, from_dep, dependent)


def _norm(name: str | None) -> str:
    return " ".join((name or "").lower().split())


def _dependent_transfers(rows, pairs, internal, dependent, dep_ibans, rejected):
    """Money between my accounts and a child's account. Those are not own transfers: what I send is
    spending (an allowance), what comes back is money in. Found by shared reference, the child
    account's IBAN, matching amounts, and then by the names seen in those."""
    to_dep: set[int] = set()
    from_dep: set[int] = set()
    if not dependent:
        return to_dep, from_dep
    by_id = {r["id"]: r for r in rows}

    def mark(own_row):
        (to_dep if own_row["amount_minor"] < 0 else from_dep).add(own_row["id"])

    for d_id, c_id, method, _ in list(pairs):
        d, c = by_id.get(d_id), by_id.get(c_id)
        if not d or not c:
            continue
        d_dep, c_dep = d["account_id"] in dependent, c["account_id"] in dependent
        if d_dep != c_dep:
            mark(c if d_dep else d)
    for r in rows:
        if r["account_id"] not in dependent and r["counterparty_iban"] and \
                r["counterparty_iban"].replace(" ", "") in dep_ibans:
            mark(r)
    # Same amount, same currency, opposite directions, within two days (child accounts often have no IBAN).
    dep_rows = [r for r in rows if r["account_id"] in dependent]
    own_rows = [r for r in rows if r["account_id"] not in dependent and r["id"] not in to_dep | from_dep
                and (r["id"] not in internal or r["counterparty_has_account"])]
    used: set[int] = set()
    for o in sorted(own_rows, key=lambda r: r["booking_date"]):
        o_day = date.fromisoformat(o["booking_date"])
        for k in dep_rows:
            if k["id"] in used or k["amount_minor"] != -o["amount_minor"] or k["currency"] != o["currency"]:
                continue
            pair = (o["id"], k["id"]) if o["amount_minor"] < 0 else (k["id"], o["id"])
            if pair in rejected or abs((date.fromisoformat(k["booking_date"]) - o_day).days) > 2:
                continue
            used.add(k["id"])
            pairs.append((*pair, "dependent", 0.8))
            mark(o)
            break
    # Names learned from the transfers found so far (e.g. "To KID EXAMPLE") cover the rest.
    names = {_norm(by_id[i]["counterparty_name"]) for i in to_dep | from_dep if by_id[i]["counterparty_name"]}
    for r in rows:
        if r["account_id"] not in dependent and r["id"] not in to_dep | from_dep and \
                r["counterparty_has_account"] and _norm(r["counterparty_name"]) in names:
            mark(r)
    return to_dep, from_dep


def store_pairs(conn: sqlite3.Connection, pairs: list[tuple[int, int, str, float]]) -> None:
    """Keeps manual decisions; replaces automatic pairs."""
    conn.execute("DELETE FROM transfer_pairs WHERE confirmed=0")
    conn.executemany(
        "INSERT OR IGNORE INTO transfer_pairs(debit_id, credit_id, method, confidence) VALUES (?,?,?,?)", pairs)
