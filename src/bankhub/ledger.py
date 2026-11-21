"""Builds the redacted, categorized ledger (transactions_clean) and recurring series.

This is the privacy boundary. transactions_clean holds only allowlisted fields;
a counterparty is named only when it matched a known merchant rule.
"""

import sqlite3
from dataclasses import dataclass
from datetime import date

from . import recurring, transfers
from .db import transaction
from .fx import RateTable
from .rules import Rules

# Columns that may ever appear in the agent-facing layer (checked by tests).
CLEAN_COLUMNS = (
    "id", "account_alias", "booking_date", "value_date", "amount", "currency", "amount_ron", "direction",
    "status", "merchant", "counterparty_type", "category", "subcategory", "is_internal_transfer", "is_recurring",
    "kind", "is_one_off",
)
ONE_OFF_RON = 50_000          # default; BANKHUB_ONE_OFF_RON overrides
MIN_REGULAR_INCOME_RON = 100  # smaller regular credits (a friend's metro money) are not "income"


@dataclass
class Classification:
    merchant: str
    counterparty_type: str
    category: str
    subcategory: str | None
    kind: str = "spending"   # spending | refund | income | other_in | internal


def classify(row: sqlite3.Row, rules: Rules, internal: bool) -> Classification:
    """Category and kind of one transaction. Money in is income only when it is (interest here;
    salary is recognised later, as regular money in from the same payer)."""
    text = " | ".join(x for x in (row["counterparty_name"], row["remittance"]) if x)
    debit = row["amount_minor"] < 0
    if internal:
        return Classification("own_account", "own_account", "transfers", "internal", "internal")
    by_code = rules.match_bank_code(row["bank_code"], debit)
    if by_code and by_code.category:
        merchant = "cash_withdrawal" if by_code.category == "cash" else row["institution"]
        kind = "spending" if debit else ("income" if by_code.category == "income" else "other_in")
        return Classification(merchant, "bank", by_code.category, by_code.subcategory, kind)
    m = rules.match_merchant(row["counterparty_name"], row["remittance"], bool(row["counterparty_has_account"]))
    if m:
        if not debit:
            # A known merchant paying me back: it reduces spending in that merchant's category.
            return Classification(m.name, "merchant", m.category, m.subcategory, "refund")
        return Classification(m.name, "merchant", m.category, m.subcategory)
    label = rules.label(text)
    if label == "cash_withdrawal" and debit:
        return Classification("cash_withdrawal", "bank", "cash", None)
    if label == "bank_fee" and debit:
        return Classification("bank_fee", "bank", "fees", None)
    if label == "interest" and not debit:
        return Classification("interest", "bank", "income", "interest", "income")
    if label == "refund" and not debit:
        return Classification("unknown_merchant", "unknown_merchant", "refunds", None, "other_in")
    has_account = bool(row["counterparty_has_account"])
    if not debit:
        # Money from people or companies that is not (yet) known to be income.
        kind_cp = "individual" if has_account else "unknown_merchant"
        return Classification("individual" if has_account else "unknown_merchant", kind_cp,
                              "money_in", "transfer_in" if has_account else None, "other_in")
    cat = rules.match_category(row["counterparty_name"], row["remittance"], has_account)
    if cat:
        kind_cp = "individual" if has_account else "unknown_merchant"
        return Classification(f"unnamed_{'payee' if has_account else 'merchant'}", kind_cp, cat.category, cat.subcategory)
    if has_account:
        # Bank transfer to a person or company: never named.
        return Classification("individual", "individual", "transfers_out", None)
    return Classification("unknown_merchant", "unknown_merchant", "uncategorized", None)


def _cluster_by_amount(charges: list[recurring.Charge]) -> list[list[recurring.Charge]]:
    """Splits one payee's charges into groups of similar amounts (e.g. a subscription vs one-off purchases)."""
    clusters: list[list[recurring.Charge]] = []
    for c in sorted(charges, key=lambda c: c.amount):
        if clusters and c.amount <= clusters[-1][0].amount * (1 + 2 * recurring.AMOUNT_BAND):
            clusters[-1].append(c)
        else:
            clusters.append([c])
    return clusters


def _series_rows(charges_by_key, meta, today, direction, start_index=0):
    """Recurring series from grouped charges. Returns (rows, member id lists)."""
    series_rows, members = [], []
    unnamed = start_index
    for key, charges in sorted(charges_by_key.items()):
        variable = direction == "out" and meta[charges[0].raw_id]["category"] in recurring.VARIABLE_CATEGORIES
        whole = recurring.detect_series(key, charges[-12:], today, variable=True) if variable else None
        # Variable bills: the whole history as one bill; otherwise (or if that fails) split by amount.
        candidates = [(charges, whole)] if whole else \
            [(c, recurring.detect_series(key, c[-12:], today)) for c in _cluster_by_amount(charges)]
        for cluster, s in candidates:
            if not s or s.status == "ended":
                continue
            if s.cadence == "annual":
                years = max(1.0, (charges[-1].day - charges[0].day).days / 365)
                if len(charges) / years > 4:
                    continue   # two similar amounts among frequent charges is not a yearly bill
            if direction == "in" and (s.monthly_ron or 0) < MIN_REGULAR_INCOME_RON:
                continue
            first = meta[s.charges[0].raw_id]
            if key.startswith("raw:"):
                # Unrecognised payee or payer: describe it without naming it.
                unnamed += 1
                if direction == "in":
                    label = f"regular income #{unnamed}"
                else:
                    category = first["category"]
                    label = f"unnamed {category} #{unnamed}" if category not in ("uncategorized", "transfers_out") \
                        else f"{first['counterparty_type']} #{unnamed}"
            else:
                label = key
            # Detection looks at recent charges; membership keeps the whole history of that bill,
            # so monthly totals further back are not understated.
            members.append([ch.raw_id for ch in cluster])
            series_rows.append((label, "income" if direction == "in" else first["category"], s.cadence, s.currency,
                                len(s.charges), s.charges[0].day.isoformat(), s.charges[-1].day.isoformat(),
                                s.next_expected.isoformat(), s.last_amount, s.previous_amount, s.price_change_pct,
                                s.status, s.monthly_ron, s.annual_ron, direction))
    return series_rows, members, unnamed


def rebuild(conn: sqlite3.Connection, rules: Rules, today: date | None = None,
            one_off_ron: float = ONE_OFF_RON) -> dict:
    today = today or date.today()
    rates = RateTable(conn)
    found = transfers.detect(conn, rules, rates)
    overrides = {r["raw_id"]: r for r in conn.execute("SELECT * FROM transaction_overrides")}
    rows = conn.execute(
        """SELECT t.*, a.alias, a.institution FROM transactions_raw t JOIN accounts a ON a.id = t.account_id
           ORDER BY t.booking_date, t.id""").fetchall()

    clean: list[dict] = []
    out_by_key: dict[str, list[recurring.Charge]] = {}
    in_by_key: dict[str, list[recurring.Charge]] = {}
    for r in rows:
        on_dependent = r["account_id"] in found.dependent_accounts
        internal = r["id"] in found.internal_ids and not on_dependent
        c = classify(r, rules, internal)
        if r["id"] in found.to_dependent:
            c = Classification("dependent_account", "dependent", "kids", "allowance", "spending")
        elif r["id"] in found.from_dependent:
            c = Classification("dependent_account", "dependent", "money_in", "from_kids", "other_in")
        o = overrides.get(r["id"])
        if o:
            if o["merchant"]:
                c.merchant, c.counterparty_type = o["merchant"], "merchant"
            if o["category"]:
                c.category, c.subcategory = o["category"], o["subcategory"]
        amount = r["amount_minor"] / 100
        amount_ron = rates.to_ron(amount, r["currency"], r["booking_date"])
        # One-off: very large, or marked by hand. It keeps its category but stays out of regular totals.
        flag = o["one_off"] if o is not None else None
        one_off = (not internal) and not on_dependent and (
            bool(flag) if flag is not None else amount_ron is not None and abs(amount_ron) >= one_off_ron)
        # A child's own payments stay visible but out of my totals.
        kind = "dependent" if on_dependent else ("one_off" if one_off else c.kind)
        row = {"id": r["id"], "account_alias": r["alias"], "booking_date": r["booking_date"],
               "value_date": r["value_date"], "amount": amount, "currency": r["currency"],
               "amount_ron": round(amount_ron, 2) if amount_ron is not None else None,
               "direction": "debit" if amount < 0 else "credit",
               "status": "booked" if r["status"] == "BOOK" else "pending", "merchant": c.merchant,
               "counterparty_type": c.counterparty_type, "category": c.category, "subcategory": c.subcategory,
               "is_internal_transfer": int(internal), "is_recurring": 0, "kind": kind, "is_one_off": int(one_off)}
        clean.append(row)
        if r["status"] != "BOOK" or one_off:
            continue
        charge = recurring.Charge(r["id"], date.fromisoformat(r["booking_date"]), abs(amount),
                                  abs(amount_ron) if amount_ron is not None else None, r["currency"])
        key = c.merchant if c.counterparty_type == "merchant" else \
            "raw:" + recurring.group_key(r["counterparty_name"], r["remittance"])
        if kind == "spending" and c.category not in ("cash", "fees"):
            out_by_key.setdefault(key, []).append(charge)
        elif kind == "other_in" and c.counterparty_type != "merchant":
            in_by_key.setdefault(key, []).append(charge)

    meta = {row["id"]: row for row in clean}
    out_rows, out_members, n = _series_rows(out_by_key, meta, today, "out")
    in_rows, in_members, _ = _series_rows(in_by_key, meta, today, "in", n)
    # Regular money in from the same payer is income (typically salary).
    for ids in in_members:
        for i in ids:
            if meta[i]["category"] == "money_in" or meta[i]["kind"] == "other_in":
                meta[i].update(category="income", subcategory="regular", kind="income")
    for ids in out_members + in_members:
        for i in ids:
            meta[i]["is_recurring"] = 1

    with transaction(conn):
        transfers.store_pairs(conn, found.pairs)
        conn.execute("DELETE FROM transactions_clean")
        conn.executemany(f"INSERT INTO transactions_clean({', '.join(CLEAN_COLUMNS)}) VALUES "
                         f"({', '.join('?' * len(CLEAN_COLUMNS))})",
                         [tuple(row[c] for c in CLEAN_COLUMNS) for row in clean])
        conn.execute("DELETE FROM recurring_members")
        conn.execute("DELETE FROM recurring_series")
        for row, ids in zip(out_rows + in_rows, out_members + in_members):
            sid = conn.execute(
                """INSERT INTO recurring_series(merchant, category, cadence, currency, occurrences, first_date,
                       last_date, next_expected, last_amount, previous_amount, price_change_pct, status,
                       monthly_ron, annual_ron, direction) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", row).lastrowid
            conn.executemany("INSERT INTO recurring_members(series_id, clean_id) VALUES (?,?)",
                             [(sid, i) for i in ids])
    return {"transactions": len(clean), "internal_transfers": len(found.internal_ids),
            "transfer_pairs": len(found.pairs), "recurring_series": len(out_rows), "income_series": len(in_rows),
            "one_offs": sum(r["is_one_off"] for r in clean)}
