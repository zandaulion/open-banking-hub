"""Data for the owner's UI.

Unlike bankhub.queries (the redacted layer agents see), this module shows the
owner their own data, including payee names and descriptions. It is only ever
served to the owner's registered devices, over a private network.
"""

import re
import sqlite3
from datetime import date, timedelta
from pathlib import Path

import yaml

from .. import queries
from ..recurring import group_key

PAYEE = """CASE WHEN c.counterparty_type = 'merchant' THEN c.merchant
                WHEN c.is_internal_transfer = 1 THEN 'Own account'
                ELSE coalesce(nullif(t.counterparty_name, ''), nullif(t.remittance, ''), c.merchant) END"""
BOOKED = "c.status = 'booked'"
BASE = "c.status = 'booked' AND c.is_internal_transfer = 0"
SPENT = "c.kind IN ('spending', 'refund')"     # refunds are positive: they net against their category


def _rows(cur) -> list[dict]:
    return [dict(r) for r in cur.fetchall()]


def _shift_month(month: str, delta: int) -> str:
    y, m = int(month[:4]), int(month[5:7]) + delta
    while m < 1:
        y, m = y - 1, m + 12
    while m > 12:
        y, m = y + 1, m - 12
    return f"{y:04d}-{m:02d}"


def _totals(conn, start: str, end: str) -> dict:
    r = conn.execute(
        f"""SELECT -sum(CASE WHEN {SPENT} THEN c.amount_ron ELSE 0 END),
                   sum(CASE WHEN c.kind = 'income' THEN c.amount_ron ELSE 0 END),
                   sum(CASE WHEN c.kind = 'other_in' THEN c.amount_ron ELSE 0 END)
            FROM transactions_clean c WHERE {BOOKED} AND c.booking_date BETWEEN ? AND ?""", (start, end)).fetchone()
    spent, income, other = (round(x or 0, 2) for x in r)
    return {"spent_ron": spent, "income_ron": income, "other_in_ron": other, "left_ron": round(income - spent, 2)}


def overview(conn: sqlite3.Connection, month: str) -> dict:
    start, end = queries.month_bounds(month)
    prev = _shift_month(month, -1)
    pstart, pend = queries.month_bounds(prev)
    now, before = _totals(conn, start, end), _totals(conn, pstart, pend)
    # Same point in the previous month, so a half-finished month compares fairly.
    today = date.today()
    to_date = None
    if start <= today.isoformat() <= end:
        cut = min(date.fromisoformat(pstart) + timedelta(days=today.day - 1), date.fromisoformat(pend))
        to_date = {"day": today.day, "previous_spent_ron": _totals(conn, pstart, cut.isoformat())["spent_ron"]}

    def by_category(a, b):
        return _rows(conn.execute(
            f"""SELECT c.category, round(-sum(c.amount_ron), 2) AS outflow_ron,
                       sum(c.amount_ron < 0) AS n, round(sum(CASE WHEN c.kind = 'refund' THEN c.amount_ron END), 2)
                       AS refunds_ron
                FROM transactions_clean c WHERE {BOOKED} AND {SPENT} AND c.booking_date BETWEEN ? AND ?
                GROUP BY c.category HAVING sum(c.amount_ron) < 0 ORDER BY outflow_ron DESC""", (a, b)))
    cats = by_category(start, end)
    prev_cats = {c["category"]: c["outflow_ron"] for c in by_category(pstart, pend)}
    for cat in cats:
        cat["previous_ron"] = prev_cats.get(cat["category"], 0.0)

    payees = _rows(conn.execute(
        f"""SELECT {PAYEE} AS payee, round(-sum(c.amount_ron), 2) AS outflow_ron, count(*) AS n,
                   max(c.category) AS category
            FROM transactions_clean c JOIN transactions_raw t ON t.id = c.id
            WHERE {BOOKED} AND c.kind = 'spending' AND c.booking_date BETWEEN ? AND ?
            GROUP BY payee ORDER BY outflow_ron DESC LIMIT 10""", (start, end)))
    one_offs = _rows(conn.execute(
        f"""SELECT c.id, c.booking_date, c.amount_ron, c.category, c.subcategory, {PAYEE} AS payee
            FROM transactions_clean c JOIN transactions_raw t ON t.id = c.id
            WHERE {BOOKED} AND c.kind = 'one_off' AND c.booking_date BETWEEN ? AND ?
            ORDER BY abs(c.amount_ron) DESC""", (start, end)))

    # The whole history up to the current month, independent of the month being viewed,
    # so the chart stays put while the selection moves.
    current = today.strftime("%Y-%m")
    oldest = conn.execute("SELECT min(booking_date) FROM transactions_clean").fetchone()[0]
    first = oldest[:7] if oldest else month
    per_month = {r[0]: r[1:] for r in conn.execute(
        f"""SELECT substr(c.booking_date, 1, 7),
                   -sum(CASE WHEN {SPENT} THEN c.amount_ron ELSE 0 END),
                   sum(CASE WHEN c.kind = 'income' THEN c.amount_ron ELSE 0 END),
                   sum(CASE WHEN c.kind = 'one_off' AND c.amount_ron < 0 THEN -c.amount_ron ELSE 0 END),
                   sum(CASE WHEN c.kind = 'one_off' AND c.amount_ron > 0 THEN c.amount_ron ELSE 0 END)
            FROM transactions_clean c WHERE {BOOKED} GROUP BY 1""")}
    trend, m = [], first
    while m <= current:
        spent, income, oo_out, oo_in = (round(x or 0, 2) for x in per_month.get(m, (0, 0, 0, 0)))
        # The month in progress, and a first month whose history starts after the 1st, are incomplete.
        starts_late = oldest is not None and m == oldest[:7] and oldest[8:10] != "01"
        trend.append({"month": m, "outflow_ron": spent, "income_ron": income, "one_off_out_ron": oo_out,
                      "one_off_in_ron": oo_in, "partial": m == current or starts_late})
        m = _shift_month(m, 1)
    excluded = conn.execute(
        "SELECT count(*), round(sum(abs(amount_ron)), 2) FROM transactions_clean"
        " WHERE is_internal_transfer = 1 AND booking_date BETWEEN ? AND ?", (start, end)).fetchone()
    newest = conn.execute("SELECT max(booking_date) FROM transactions_clean").fetchone()[0]
    # Income only means something to compare against once a salary-like regular income is known.
    has_regular_income = conn.execute(
        "SELECT 1 FROM recurring_series WHERE direction = 'in' LIMIT 1").fetchone() is not None
    return {"month": month, **now, "has_regular_income": has_regular_income, "outflow_ron": now["spent_ron"], "previous": {"month": prev, **before},
            "to_date": to_date, "categories": cats, "payees": payees, "one_offs": one_offs, "trend": trend,
            "internal_excluded": {"n": excluded[0], "volume_ron": excluded[1] or 0.0},
            "newest_transaction": newest}


AMOUNT = re.compile(r"^\s*(?P<op>[<>]=?)?\s*(?P<a>\d+(?:[.,]\d{1,2})?)\s*(?:-\s*(?P<b>\d+(?:[.,]\d{1,2})?))?\s*(?:ron|lei|eur|€)?\s*$", re.I)


def amount_filter(q: str) -> tuple[str, list] | None:
    """'38.68' exact, '38' any 38.xx, '>500', '<=20', '100-200': on the absolute amount, original currency."""
    m = AMOUNT.match(q or "")
    if not m:
        return None
    a = float(m["a"].replace(",", "."))
    if m["b"]:
        b = float(m["b"].replace(",", "."))
        return "abs(c.amount) BETWEEN ? AND ?", [min(a, b), max(a, b)]
    if m["op"]:
        return f"abs(c.amount) {m['op']} ?", [a]
    if "." in m["a"] or "," in m["a"]:
        return "abs(abs(c.amount) - ?) < 0.005", [a]
    return "abs(c.amount) >= ? AND abs(c.amount) < ?", [a, a + 1]


def transactions(conn: sqlite3.Connection, *, month: str | None = None, category: str | None = None,
                 q: str | None = None, account: str | None = None, include_internal: bool = True,
                 kind: str | None = None, limit: int = 50, offset: int = 0) -> dict:
    where, args = [], []
    if month:
        start, end = queries.month_bounds(month)
        where.append("c.booking_date BETWEEN ? AND ?")
        args += [start, end]
    if category:
        where.append("c.category = ?")
        args.append(category)
    if account:
        where.append("c.account_alias = ?")
        args.append(account)
    if kind:
        where.append("c.kind = ?")
        args.append(kind)
    if not include_internal:
        where.append("c.is_internal_transfer = 0")
    amount = amount_filter(q) if q else None
    if amount:
        where.append(amount[0])
        args += amount[1]
    elif q:
        where.append("(t.counterparty_name LIKE ? OR t.remittance LIKE ? OR c.merchant LIKE ? OR c.category LIKE ?)")
        args += [f"%{q}%"] * 4
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    limit = max(1, min(limit, 200))
    total = conn.execute(f"SELECT count(*) FROM transactions_clean c JOIN transactions_raw t ON t.id=c.id {clause}",
                         args).fetchone()[0]
    rows = _rows(conn.execute(
        f"""SELECT c.id, c.booking_date, c.account_alias, c.amount, c.currency, c.amount_ron, c.status,
                   c.category, c.subcategory, c.is_internal_transfer, c.is_recurring, c.counterparty_type,
                   {PAYEE} AS payee, t.remittance AS description, c.kind, c.is_one_off,
                   coalesce(a.display_name, c.account_alias) AS account_name,
                   (o.category IS NOT NULL) AS overridden, o.one_off AS one_off_setting
            FROM transactions_clean c JOIN transactions_raw t ON t.id = c.id
            JOIN accounts a ON a.id = t.account_id
            LEFT JOIN transaction_overrides o ON o.raw_id = c.id {clause}
            ORDER BY c.booking_date DESC, c.id DESC LIMIT ? OFFSET ?""", args + [limit, offset]))
    return {"total": total, "offset": offset, "transactions": rows}


def recurring(conn: sqlite3.Connection) -> dict:
    """The redacted recurring view, with real payee names put back for the owner."""
    data = queries.recurring_payments(conn)
    names = {}
    for sid, payee in conn.execute(
            f"""SELECT m.series_id, {PAYEE} FROM recurring_members m
                JOIN transactions_clean c ON c.id = m.clean_id JOIN transactions_raw t ON t.id = c.id
                ORDER BY c.booking_date"""):
        names[sid] = payee
    for series in data["series"] + data["income"]:
        series["payee"] = names.get(series["id"], series["merchant"])
    return data


def accounts(conn: sqlite3.Connection) -> dict:
    balances = queries.balances(conn)
    names = dict(conn.execute("SELECT alias, display_name FROM accounts"))
    history = {r[0]: r[1] for r in conn.execute(
        "SELECT a.alias, min(t.booking_date) FROM accounts a JOIN transactions_raw t ON t.account_id = a.id GROUP BY a.id")}
    for b in balances["accounts"]:
        b["alias"] = b["account"]
        b["name"] = names.get(b["account"]) or b["account"]
        b["history_from"] = history.get(b["account"])
    return {"balances": balances, "status": queries.sync_status(conn)}


def rename_account(conn: sqlite3.Connection, alias: str, name: str | None) -> bool:
    name = (name or "").strip()[:40] or None
    return conn.execute("UPDATE accounts SET display_name=? WHERE alias=?", (name, alias)).rowcount > 0


def suggest_pattern(example: str) -> str:
    """A pattern that is a real substring of the payee text: 'Ep*example.ro' -> 'example.ro'."""
    text = (example or "").lower().strip()
    if "*" in text:
        head, tail = text.split("*", 1)
        text = tail.strip() if len(head.strip()) <= 5 else head.strip()
    words = []
    for w in text.split():
        if any(ch.isdigit() for ch in w) or len(words) == 2:
            break
        words.append(w)
    return " ".join(words) or text[:20]


def inbox(conn: sqlite3.Connection, limit: int = 40) -> dict:
    """Unrecognised payees, grouped the same way recurring detection groups them, biggest first."""
    groups: dict[str, dict] = {}
    for name, desc, ron, day, has_acct in conn.execute(
            """SELECT t.counterparty_name, t.remittance, c.amount_ron, c.booking_date, t.counterparty_has_account
               FROM transactions_clean c JOIN transactions_raw t ON t.id = c.id
               WHERE c.category = 'uncategorized' AND c.direction = 'debit' AND c.status = 'booked'"""):
        key = group_key(name, desc)
        if not key:
            continue
        g = groups.setdefault(key, {"key": key, "example": name or desc, "pattern": suggest_pattern(name or desc),
                                    "n": 0, "outflow_ron": 0.0,
                                    "last_date": day, "bank_transfer": bool(has_acct)})
        g["n"] += 1
        g["outflow_ron"] = round(g["outflow_ron"] - (ron or 0), 2)
        g["last_date"] = max(g["last_date"], day)
    items = sorted(groups.values(), key=lambda g: -g["outflow_ron"])
    share = conn.execute(
        f"""SELECT round(100.0 * sum(CASE WHEN category = 'uncategorized' THEN -amount_ron ELSE 0 END)
                   / nullif(sum(-amount_ron), 0), 1)
            FROM transactions_clean c WHERE {BASE} AND amount_ron < 0""").fetchone()[0]
    return {"items": items[:limit], "remaining": len(items), "uncategorized_share_pct": share or 0.0}


def category_choices(conn: sqlite3.Connection, rules_text: str) -> list[str]:
    cats = {r[0] for r in conn.execute("SELECT DISTINCT category FROM transactions_clean")}
    data = yaml.safe_load(rules_text) or {}
    cats |= {m["category"] for m in data.get("merchants") or []}
    cats |= {c["category"] for c in data.get("category_rules") or []}
    return sorted(cats - {"uncategorized", "transfers", "income"})
