"""Read-only queries over the redacted layer, shared by CLI reports and the MCP server.

Only transactions_clean, recurring_series, accounts (alias/currency/institution),
consents (dates), balance_snapshots and sync_runs are read here. Never the raw tables.
"""

import sqlite3
from datetime import date, datetime, timedelta, timezone

from .fx import RateTable

MAX_ROWS = 500
GROUPS = {"category": "category", "merchant": "merchant", "month": "substr(booking_date, 1, 7)",
          "account": "account_alias", "subcategory": "category || '/' || coalesce(subcategory, '-')"}


def _rows(cur) -> list[dict]:
    return [dict(r) for r in cur.fetchall()]


def month_bounds(month: str) -> tuple[str, str]:
    start = date.fromisoformat(month + "-01")
    end = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
    return start.isoformat(), (end - timedelta(days=1)).isoformat()


def sync_status(conn: sqlite3.Connection) -> dict:
    now = datetime.now(timezone.utc)
    banks = []
    for c in conn.execute("SELECT institution, valid_until FROM consents WHERE status='active' ORDER BY institution"):
        valid = datetime.fromisoformat(c["valid_until"].replace("Z", "+00:00"))
        last = conn.execute(
            """SELECT MAX(r.ts) FROM api_requests r JOIN accounts a ON a.id=r.account_id
               WHERE a.institution=?""", (c["institution"],)).fetchone()[0]
        banks.append({"institution": c["institution"], "consent_valid_until": c["valid_until"][:10],
                      "consent_days_left": (valid - now).days, "last_sync": last,
                      "renew_soon": (valid - now).days <= 14})
    last_run = conn.execute("SELECT started_at, status, error FROM sync_runs ORDER BY id DESC LIMIT 1").fetchone()
    latest = conn.execute("SELECT MAX(booking_date) FROM transactions_clean").fetchone()[0]
    return {"banks": banks, "last_run": dict(last_run) if last_run else None, "latest_transaction_date": latest}


def balances(conn: sqlite3.Connection) -> dict:
    rows = _rows(conn.execute(
        """SELECT a.alias AS account, a.institution, CASE WHEN a.group_id IS NOT NULL THEN 'dependent' WHEN a.source = 'statement' THEN 'card' ELSE 'own' END AS role, b.currency, b.amount_minor / 100.0 AS amount,
                  b.balance_type, b.fetched_at
           FROM accounts a JOIN balance_snapshots b ON b.account_id = a.id
           WHERE a.active = 1 AND b.id = (SELECT id FROM balance_snapshots WHERE account_id=a.id
                                          ORDER BY fetched_at DESC, id DESC LIMIT 1)
           ORDER BY a.alias"""))
    rates = RateTable(conn)
    today = date.today().isoformat()
    total_ron = 0.0
    dependent_ron = 0.0
    card_ron = 0.0
    for r in rows:
        ron = rates.to_ron(r["amount"], r["currency"], today)
        r["amount_ron"] = round(ron, 2) if ron is not None else None
        # An account in a group (someone else's money) is shown, but is not part of my total.
        if r["role"] == "dependent":
            dependent_ron += ron or 0
        elif r["role"] == "card":
            card_ron += ron or 0      # a negative number: what is owed
        else:
            total_ron += ron or 0
    eur = rates.to_ron(1, "EUR", today)
    return {"accounts": rows, "total_ron": round(total_ron, 2), "dependent_total_ron": round(dependent_ron, 2),
            "card_owed_ron": round(-card_ron, 2),
            "total_eur": round(total_ron / eur, 2) if eur else None, "fx_basis": "BNR official rate"}


def query_transactions(conn: sqlite3.Connection, *, date_from: str | None = None, date_to: str | None = None,
                       account: str | None = None, category: str | None = None, merchant: str | None = None,
                       min_amount: float | None = None, max_amount: float | None = None,
                       currency: str | None = None, direction: str | None = None,
                       include_internal: bool = True, include_pending: bool = True,
                       limit: int = 100, offset: int = 0) -> dict:
    where, args = [], []
    for col, val in (("account_alias", account), ("category", category), ("currency", currency),
                     ("direction", direction)):
        if val:
            where.append(f"{col} = ?")
            args.append(val)
    if merchant:
        where.append("merchant LIKE ?")
        args.append(f"%{merchant}%")
    if date_from:
        where.append("booking_date >= ?")
        args.append(date_from)
    if date_to:
        where.append("booking_date <= ?")
        args.append(date_to)
    if min_amount is not None:
        where.append("abs(amount_ron) >= ?")
        args.append(min_amount)
    if max_amount is not None:
        where.append("abs(amount_ron) <= ?")
        args.append(max_amount)
    if not include_internal:
        where.append("is_internal_transfer = 0")
    if not include_pending:
        where.append("status = 'booked'")
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    limit = max(1, min(limit, MAX_ROWS))
    total = conn.execute(f"SELECT COUNT(*) FROM transactions_clean {clause}", args).fetchone()[0]
    rows = _rows(conn.execute(
        f"SELECT * FROM transactions_clean {clause} ORDER BY booking_date DESC, id DESC LIMIT ? OFFSET ?",
        args + [limit, offset]))
    return {"total": total, "returned": len(rows), "offset": offset, "transactions": rows,
            "note": "amount is in the original currency (negative = money out); amount_ron uses the BNR rate"}


SPENT = "kind IN ('spending', 'refund')"   # refunds are positive and net against their category


def spending_summary(conn: sqlite3.Connection, *, date_from: str, date_to: str, group_by: str = "category",
                     include_internal: bool = False, top: int = 50) -> dict:
    """Regular spending (net of refunds) grouped, plus income, other money in and one-offs, listed apart.
    include_internal is kept for compatibility: own-account transfers are never spending."""
    if group_by not in GROUPS:
        raise ValueError(f"group_by must be one of {sorted(GROUPS)}")
    base = "booking_date BETWEEN ? AND ? AND status = 'booked'"
    args = [date_from, date_to]
    expr = GROUPS[group_by]
    groups = _rows(conn.execute(
        f"""SELECT {expr} AS name, round(-sum(amount_ron), 2) AS outflow_ron, count(*) AS transactions
            FROM transactions_clean WHERE {base} AND {SPENT}
            GROUP BY name HAVING sum(amount_ron) < 0 ORDER BY outflow_ron DESC LIMIT ?""", args + [top]))
    total = lambda where: round(conn.execute(
        f"SELECT coalesce(sum(amount_ron), 0) FROM transactions_clean WHERE {base} AND {where}", args).fetchone()[0], 2)
    spent = -total(SPENT)
    income = total("kind = 'income'")
    other_in = total("kind = 'other_in'")
    one_offs = _rows(conn.execute(
        f"""SELECT booking_date, amount_ron, category, subcategory, merchant FROM transactions_clean
            WHERE {base} AND kind = 'one_off' ORDER BY abs(amount_ron) DESC""", args))
    excluded = conn.execute(
        "SELECT count(*), round(sum(abs(amount_ron)), 2) FROM transactions_clean"
        " WHERE booking_date BETWEEN ? AND ? AND is_internal_transfer = 1", args).fetchone()
    # A salary is known from a regular series, or from a payroll credit the bank labels as such.
    regular_income = conn.execute(
        "SELECT 1 FROM recurring_series WHERE direction = 'in' UNION ALL"
        " SELECT 1 FROM transactions_clean WHERE kind = 'income' AND subcategory = 'salary' LIMIT 1").fetchone() is not None
    return {"date_from": date_from, "date_to": date_to, "group_by": group_by, "groups": groups,
            "total_outflow_ron": spent, "income_ron": income, "other_money_in_ron": other_in,
            "total_inflow_ron": round(income + other_in, 2),
            "regular_income_detected": regular_income,
            # Without a known salary, "left" would only restate spending as a loss.
            "left_ron": round(income - spent, 2) if regular_income else None,
            "net_ron": round(income + other_in - spent, 2), "one_offs": one_offs,
            "internal_transfers_excluded": {"transactions": excluded[0], "volume_ron": excluded[1] or 0.0},
            "note": "booked transactions only; spending is net of refunds; income is salary-like regular money "
                    "in plus interest; one-offs (very large payments such as property or loan closure) and "
                    "transfers between own accounts are not in the totals; amounts in RON at the BNR rate"}


def _months_back(today: date, n: int) -> list[str]:
    y, m = today.year, today.month
    out = []
    for _ in range(n):
        out.append(f"{y:04d}-{m:02d}")
        y, m = (y, m - 1) if m > 1 else (y - 1, 12)
    return out[::-1]


def _change(now: float | None, before: float | None) -> float | None:
    if not now or not before:
        return None
    return round((now - before) / before * 100, 1)


def recurring_payments(conn: sqlite3.Connection, *, include_missed: bool = True,
                       today: date | None = None) -> dict:
    today = today or date.today()
    months = _months_back(today, 13)
    statuses = ("active", "missed") if include_missed else ("active",)
    rows = _rows(conn.execute(
        f"""SELECT id, merchant, category, cadence, currency, last_amount, previous_amount, price_change_pct,
                   occurrences, first_date, last_date, next_expected, status, monthly_ron, annual_ron
            FROM recurring_series WHERE status IN ({','.join('?' * len(statuses))}) AND direction = 'out'
            ORDER BY monthly_ron DESC""", statuses))
    income_rows = _rows(conn.execute(
        f"""SELECT id, merchant, cadence, currency, last_amount, previous_amount, occurrences, first_date,
                   last_date, next_expected, status, monthly_ron, annual_ron
            FROM recurring_series WHERE status IN ({','.join('?' * len(statuses))}) AND direction = 'in'
            ORDER BY monthly_ron DESC""", statuses))
    per_month: dict[int, dict[str, float]] = {}
    for sid, month, ron in conn.execute(
            """SELECT m.series_id, substr(c.booking_date, 1, 7), -sum(c.amount_ron)
               FROM recurring_members m JOIN transactions_clean c ON c.id = m.clean_id
               GROUP BY 1, 2"""):
        per_month.setdefault(sid, {})[month] = round(ron or 0, 2)

    charges_by_series: dict[int, list[tuple[str, float]]] = {}
    for sid, day, ron in conn.execute(
            """SELECT m.series_id, c.booking_date, -c.amount_ron FROM recurring_members m
               JOIN transactions_clean c ON c.id = m.clean_id ORDER BY c.booking_date, c.id"""):
        charges_by_series.setdefault(sid, []).append((day, round(ron or 0, 2)))
    this_month = months[-1]
    first_month = min((m for hist in per_month.values() for m in hist), default=this_month)
    all_months = [m for m in _months_back(today, 600) if m >= min(first_month, months[0])]
    totals = {m: 0.0 for m in all_months}
    expected_rest = 0.0
    for r in rows:
        hist = per_month.get(r["id"], {})
        r["history"] = [{"month": m, "amount_ron": hist.get(m, 0.0)} for m in months]
        for m in all_months:
            totals[m] += hist.get(m, 0.0)
        charges = charges_by_series.get(r["id"], [])
        if len(charges) >= 2:
            (prev_day, prev_ron), (last_day, last_ron) = charges[-2], charges[-1]
            # Bill against the previous bill: month-over-month for monthly payments, year-over-year for annual.
            r["change"] = {"basis": {"annual": "YoY", "weekly": "WoW"}.get(r["cadence"], "MoM"),
                           "date": last_day, "amount_ron": last_ron, "previous_date": prev_day,
                           "previous_ron": prev_ron, "change_pct": _change(last_ron, prev_ron)}
        else:
            r["change"] = None
        if r["status"] == "active" and r["next_expected"][:7] == this_month and this_month not in hist:
            expected_rest += r["monthly_ron"] if r["cadence"] != "annual" else (r["annual_ron"] or 0)
    active = [r for r in rows if r["status"] == "active"]
    oldest = conn.execute("SELECT min(booking_date) FROM transactions_clean").fetchone()[0] or ""
    series_totals = [{"month": m, "amount_ron": round(totals[m], 2),
                      "partial": m == this_month or (m == oldest[:7] and oldest[8:10] != "01")}
                     for m in all_months]
    last_full, before = series_totals[-2], series_totals[-3]
    monthly = [r for r in rows if r["status"] == "active" and r["cadence"] == "monthly" and r["change"]]
    latest_total = round(sum(r["change"]["amount_ron"] for r in monthly), 2)
    previous_total = round(sum(r["change"]["previous_ron"] for r in monthly), 2)
    for r in income_rows:
        charges = charges_by_series.get(r["id"], [])
        r["change"] = {"basis": "MoM", "date": charges[-1][0], "amount_ron": charges[-1][1],
                       "previous_date": charges[-2][0], "previous_ron": charges[-2][1],
                       "change_pct": _change(charges[-1][1], charges[-2][1])} if len(charges) >= 2 else None
    return {"series": rows, "income": income_rows,
            "bills_change": {"basis": "MoM", "latest_total_ron": latest_total, "previous_total_ron": previous_total,
                             "change_pct": _change(latest_total, previous_total), "bills": len(monthly)},
            "active_monthly_total_ron": round(sum(r["monthly_ron"] or 0 for r in active), 2),
            "active_annual_total_ron": round(sum(r["annual_ron"] or 0 for r in active), 2),
            "totals_by_month": series_totals,
            "last_full_month": {**last_full, "previous_ron": before["amount_ron"],
                                "change_pct": _change(last_full["amount_ron"], before["amount_ron"])},
            "this_month": {"month": this_month, "paid_ron": series_totals[-1]["amount_ron"],
                           "expected_rest_ron": round(expected_rest, 2)},
            "note": "change compares the latest charge with the previous one: month-over-month (MoM) for monthly "
                    "bills, year-over-year (YoY) for annual ones; totals_by_month is by calendar month; "
                    "unrecognised payees are labelled by type, never by name"}


def _continuous_from(counts: dict[str, int], first: str, last: str) -> str | None:
    """The first month from which an account's history has no gap. An account that usually has several
    payments a month and then has none for a month is missing that month (a sync or statement not fetched
    yet), and nothing before its first payment is known. A quiet account's empty months are real, before its
    first payment too, so it limits nothing (None)."""
    months, m = [], first
    while m <= last:
        months.append(m)
        y, mo = int(m[:4]), int(m[5:7])
        m = f"{y + mo // 12}-{mo % 12 + 1:02d}"
    busy = sorted(counts.get(x, 0) for x in months)
    if not busy or busy[len(busy) // 2] < 5:
        return None
    empty = [x for x in months if counts.get(x, 0) == 0 and x != last]
    if not empty:
        return first
    nxt = empty[-1]
    y, mo = int(nxt[:4]), int(nxt[5:7])
    return f"{y + mo // 12}-{mo % 12 + 1:02d}"


def net_worth_history(conn: sqlite3.Connection, *, months: int = 24, today: date | None = None) -> dict:
    """Total of your accounts minus what is owed on cards, at the end of each month, in RON.

    Balances are only fetched from now on, so earlier months are worked back from the latest balance and
    the booked transactions since. That is only right while an account's history has no gaps, so the series
    starts where every account's history is complete. Accounts in a group (someone else's money) are left out;
    a card counts from its first statement."""
    today = today or date.today()
    rates = RateTable(conn)
    end_month = today.strftime("%Y-%m")
    span = _months_back(today, months)
    ends = {m: min(month_bounds(m)[1], today.isoformat()) for m in span}
    series = {m: 0.0 for m in span}
    cards_by_month = {m: 0.0 for m in span}
    start = span[0]
    for acc in conn.execute("SELECT a.id, a.alias, a.currency, a.source FROM accounts a"
                            " WHERE a.active = 1 AND a.group_id IS NULL"):
        snaps = conn.execute("SELECT fetched_at, amount_minor, currency FROM balance_snapshots WHERE account_id = ?"
                             " ORDER BY fetched_at, id", (acc["id"],)).fetchall()
        if not snaps:
            continue
        if acc["source"] == "statement":
            first_card = snaps[0]["fetched_at"][:7]
            for m in span:
                known = [s for s in snaps if s["fetched_at"][:10] <= ends[m]]
                if m >= first_card and known:
                    value = rates.to_ron(known[-1]["amount_minor"] / 100, known[-1]["currency"], ends[m]) or 0.0
                    series[m] += value
                    cards_by_month[m] += value
            continue
        latest = snaps[-1]
        rows = conn.execute("SELECT booking_date, amount FROM transactions_clean WHERE account_alias = ? AND status = 'booked'",
                            (acc["alias"],)).fetchall()
        if rows:
            counts: dict[str, int] = {}
            for r in rows:
                counts[r["booking_date"][:7]] = counts.get(r["booking_date"][:7], 0) + 1
            start = max(start, _continuous_from(counts, min(counts), end_month) or start)
        snap_day = latest["fetched_at"][:10]
        for m in span:
            later = sum(r["amount"] for r in rows if ends[m] < r["booking_date"] <= snap_day)
            value = latest["amount_minor"] / 100 - later
            series[m] += rates.to_ron(value, latest["currency"], ends[m]) or 0.0
    points = [{"month": m, "net_worth_ron": round(series[m], 2), "cards_ron": round(cards_by_month[m], 2),
               "partial": m == end_month} for m in span if m >= start]
    return {"months": points, "from": points[0]["month"] if points else None, "fx_basis": "BNR official rate"}


def credit_cards(conn: sqlite3.Connection) -> dict:
    """Cards added from statements: what the latest statement says. Instalment plans are counted and summed,
    never named (their merchant names stay in the owner's app)."""
    out = []
    for a in conn.execute("SELECT id, alias FROM accounts WHERE source = 'statement' AND active = 1 ORDER BY alias"):
        s = conn.execute("SELECT * FROM card_statements WHERE account_id = ? ORDER BY period_end DESC LIMIT 1",
                         (a["id"],)).fetchone()
        if not s:
            continue
        plans = conn.execute("SELECT count(*), sum(installment_minor), sum(remaining_minor) FROM card_plans WHERE statement_id = ?",
                             (s["id"],)).fetchone()
        ron = lambda v: None if v is None else round(v / 100, 2)
        out.append({"account": a["alias"], "statement_period": [s["period_start"], s["period_end"]],
                    "owed_ron": ron(s["owed_minor"]), "credit_limit_ron": ron(s["limit_minor"]),
                    "available_ron": ron(s["available_minor"]), "due_date": s["due_date"],
                    "minimum_due_ron": ron(s["min_due_minor"]), "total_due_ron": ron(s["total_due_minor"]),
                    "instalment_plans": plans[0], "instalments_this_month_ron": ron(plans[1]),
                    "instalments_remaining_ron": ron(plans[2])})
    return {"cards": out, "note": "Owed on a card is not part of the balances total."}


def compare_periods(conn: sqlite3.Connection, *, a_from: str, a_to: str, b_from: str, b_to: str,
                    group_by: str = "category") -> dict:
    """Spending in period B against period A, per group, biggest changes first."""
    a = spending_summary(conn, date_from=a_from, date_to=a_to, group_by=group_by)
    b = spending_summary(conn, date_from=b_from, date_to=b_to, group_by=group_by)
    key = lambda g: g["name"]
    av = {key(g): g["outflow_ron"] for g in a["groups"]}
    bv = {key(g): g["outflow_ron"] for g in b["groups"]}
    rows = [{group_by: k, "a_ron": round(av.get(k, 0.0), 2), "b_ron": round(bv.get(k, 0.0), 2),
             "change_ron": round(bv.get(k, 0.0) - av.get(k, 0.0), 2), "change_pct": _change(bv.get(k), av.get(k))}
            for k in set(av) | set(bv)]
    rows.sort(key=lambda r: -abs(r["change_ron"]))
    return {"a": {"from": a_from, "to": a_to, "spent_ron": a["total_outflow_ron"], "income_ron": a["income_ron"]},
            "b": {"from": b_from, "to": b_to, "spent_ron": b["total_outflow_ron"], "income_ron": b["income_ron"]},
            "groups": rows}


def week_summary(conn: sqlite3.Connection, *, today: date | None = None) -> dict:
    """The last seven full days against the usual week (median of the eight before), on day-to-day spending:
    regular payments are shown apart, and cards added from statements (whose purchases arrive once a month)
    are left out. Categories well above their usual week, and regular payments due in the next seven days."""
    from statistics import median
    today = today or date.today()
    end = today - timedelta(days=1)
    start = end - timedelta(days=6)
    synced = "account_alias NOT IN (SELECT alias FROM accounts WHERE source = 'statement')"

    def spent(a: date, b: date, extra: str = "") -> float:
        return round(-(conn.execute(f"""SELECT coalesce(sum(amount_ron), 0) FROM transactions_clean
            WHERE status = 'booked' AND kind IN ('spending', 'refund') AND {synced} AND booking_date BETWEEN ? AND ? {extra}""",
            (a.isoformat(), b.isoformat())).fetchone()[0] or 0), 2)

    weeks = [(start - timedelta(days=7 * i), end - timedelta(days=7 * i)) for i in range(1, 9)]
    daily = spent(start, end, "AND is_recurring = 0")
    history = [h for h in (spent(a, b, "AND is_recurring = 0") for a, b in weeks) if h > 0]
    usual = round(median(history), 2) if len(history) >= 3 else None
    by_cat: dict[str, list[float]] = {}
    for cat, day, amount in conn.execute(f"""SELECT category, booking_date, amount_ron FROM transactions_clean
            WHERE status = 'booked' AND kind IN ('spending', 'refund') AND is_recurring = 0 AND {synced}
            AND booking_date BETWEEN ? AND ?""", (weeks[-1][0].isoformat(), end.isoformat())):
        by_cat.setdefault(cat, [0.0] * 9)[(end - date.fromisoformat(day)).days // 7] -= amount or 0
    up = sorted(({"category": c, "spent_ron": round(w[0], 2), "usual_ron": round(median(w[1:]), 2)}
                 for c, w in by_cat.items() if w[0] >= 100 and w[0] >= 1.5 * median(w[1:]) + 50),
                key=lambda x: x["usual_ron"] - x["spent_ron"])
    due = _rows(conn.execute("""SELECT merchant, next_expected, last_amount, currency FROM recurring_series
        WHERE direction = 'out' AND status = 'active' AND next_expected BETWEEN ? AND ? ORDER BY next_expected""",
        (today.isoformat(), (today + timedelta(days=6)).isoformat())))
    return {"from": start.isoformat(), "to": end.isoformat(), "spent_ron": spent(start, end), "day_to_day_ron": daily,
            "usual_day_to_day_ron": usual, "change_pct": _change(daily, usual), "categories_above_usual": up[:5],
            "due_next_7_days": due}
