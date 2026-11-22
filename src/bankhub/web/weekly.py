"""The weekly summary: last week's spending against your usual week, what changed, what is worth a look, and
what is coming up. Rules and arithmetic only, no AI. Owner-only (it names payees).

The summary covers the seven days before the day it is sent (Monday 08:00 by default: Monday to Sunday).
"""

import sqlite3
from datetime import date, datetime, timedelta
from statistics import median

from .data import BOOKED, PAYEE, SPENT

WEEKS_FOR_USUAL = 8


def last_scheduled(now: datetime, weekday: int, hour: int) -> datetime:
    """The latest scheduled moment at or before `now` (weekday 0 = Monday)."""
    at = (now - timedelta(days=(now.weekday() - weekday) % 7)).replace(hour=hour, minute=0, second=0, microsecond=0)
    return at if at <= now else at - timedelta(days=7)


# A card added from statements gets its purchases once a month, so last week never has them yet while the
# weeks before do: it is left out of every weekly figure, on both sides of the comparison.
SYNCED = "c.account_alias NOT IN (SELECT alias FROM accounts WHERE source = 'statement')"


def _spent(conn, start: date, end: date, extra: str = "", args: tuple = ()) -> float:
    r = conn.execute(f"""SELECT -coalesce(sum(c.amount_ron), 0) FROM transactions_clean c
                         WHERE {BOOKED} AND {SPENT} AND {SYNCED} AND c.booking_date BETWEEN ? AND ? {extra}""",
                     (start.isoformat(), end.isoformat(), *args)).fetchone()[0]
    return round(r or 0.0, 2)


def summary(conn: sqlite3.Connection, sent_on: date) -> dict:
    end = sent_on - timedelta(days=1)
    start = end - timedelta(days=6)
    weeks = [(start - timedelta(days=7 * i), end - timedelta(days=7 * i)) for i in range(1, WEEKS_FOR_USUAL + 1)]
    # Regular payments (rent, bills) land in some weeks and not others, so the week is compared with your
    # usual week on day-to-day spending only; the regular payments are shown apart.
    spent = _spent(conn, start, end)
    daily = _spent(conn, start, end, "AND c.is_recurring = 0")
    history = [_spent(conn, a, b, "AND c.is_recurring = 0") for a, b in weeks]
    known = [h for h in history if h > 0]
    usual = round(median(known), 2) if len(known) >= 3 else None

    # The month so far against the same days of the month before.
    m_start = end.replace(day=1)
    prev_end_month = m_start - timedelta(days=1)
    p_start = prev_end_month.replace(day=1)
    p_end = min(p_start + timedelta(days=end.day - 1), prev_end_month)
    month = {"month": m_start.strftime("%Y-%m"), "days": end.day, "spent_ron": _spent(conn, m_start, end),
             "previous_ron": _spent(conn, p_start, p_end), "previous_month": p_start.strftime("%Y-%m")}

    # Categories well above their usual week.
    rows = conn.execute(f"""SELECT c.category, c.booking_date, c.amount_ron FROM transactions_clean c
                            WHERE {BOOKED} AND {SPENT} AND {SYNCED} AND c.is_recurring = 0 AND c.booking_date BETWEEN ? AND ?""",
                        (weeks[-1][0].isoformat(), end.isoformat())).fetchall()
    by_cat: dict[str, list[float]] = {}
    for cat, day, amount in rows:
        i = (end - date.fromisoformat(day)).days // 7        # 0 = last week, 1.. = the weeks before
        by_cat.setdefault(cat, [0.0] * (WEEKS_FOR_USUAL + 1))[i] -= amount
    up = []
    for cat, w in by_cat.items():
        base = median(w[1:])
        if w[0] >= 100 and w[0] >= 1.5 * base + 50:
            up.append({"category": cat, "spent_ron": round(w[0], 2), "usual_ron": round(base, 2)})
    up.sort(key=lambda x: x["usual_ron"] - x["spent_ron"])

    look = _worth_a_look(conn, start, end)
    coming = _coming_up(conn, sent_on)
    return {"start": start.isoformat(), "end": end.isoformat(), "spent_ron": spent, "day_to_day_ron": daily,
            "regular_ron": round(spent - daily, 2), "usual_ron": usual,
            "change_pct": round((daily - usual) / usual * 100, 1) if usual else None, "month": month,
            "categories_up": up[:3], "worth_a_look": look[:6], "coming_up": coming[:8],
            "incomplete": _incomplete(conn, start, end),
            "cards_left_out": [r[0] for r in conn.execute(
                "SELECT coalesce(display_name, alias) FROM accounts WHERE source = 'statement' AND active = 1")]}


def _incomplete(conn, start: date, end: date) -> list[dict]:
    """Accounts that usually have payments every week but none in the last days of this one: the bank has not
    sent them yet (a sync cut short by its limit), so the week is probably not complete."""
    out = []
    for r in conn.execute(
            """SELECT a.alias, coalesce(a.display_name, a.alias) AS name, max(c.booking_date) AS newest,
                      sum(c.booking_date BETWEEN ? AND ?) AS before
               FROM accounts a JOIN transactions_clean c ON c.account_alias = a.alias
               WHERE a.source = 'bank' AND a.active = 1 AND a.group_id IS NULL AND c.status = 'booked'
               GROUP BY a.id""", ((start - timedelta(days=56)).isoformat(), (start - timedelta(days=1)).isoformat())):
        if r["before"] >= 16 and r["newest"] < (end - timedelta(days=3)).isoformat():
            out.append({"account": r["name"], "newest": r["newest"]})
    return out


def _worth_a_look(conn, start: date, end: date) -> list[dict]:
    week = [dict(r) for r in conn.execute(
        f"""SELECT c.id, c.booking_date, c.amount_ron, {PAYEE} AS payee, c.counterparty_type
            FROM transactions_clean c JOIN transactions_raw t ON t.id = c.id
            WHERE {BOOKED} AND c.kind = 'spending' AND {SYNCED} AND c.booking_date BETWEEN ? AND ?
            ORDER BY c.booking_date""", (start.isoformat(), end.isoformat()))]
    out, seen = [], set()
    for p in week:
        before = [r[0] for r in conn.execute(
            f"""SELECT -c.amount_ron FROM transactions_clean c JOIN transactions_raw t ON t.id = c.id
                WHERE {BOOKED} AND c.kind = 'spending' AND {PAYEE} = ? AND c.booking_date < ?
                      AND c.booking_date >= ?""",
            (p["payee"], start.isoformat(), (start - timedelta(days=183)).isoformat()))]
        amount = -p["amount_ron"]
        if len(before) >= 3 and amount >= 100 and amount >= 2 * median(before):
            out.append({"kind": "bigger", "payee": p["payee"], "date": p["booking_date"], "amount_ron": round(amount, 2),
                        "usual_ron": round(median(before), 2)})
        elif not before and amount >= 300 and not conn.execute(
                f"""SELECT 1 FROM transactions_clean c JOIN transactions_raw t ON t.id = c.id
                    WHERE {PAYEE} = ? AND c.booking_date < ? LIMIT 1""", (p["payee"], start.isoformat())).fetchone():
            out.append({"kind": "new", "payee": p["payee"], "date": p["booking_date"], "amount_ron": round(amount, 2)})
        # The same amount to the same payee on the same or the next day: perhaps charged twice.
        twin = next((q for q in week if q["id"] != p["id"] and q["payee"] == p["payee"] and q["amount_ron"] == p["amount_ron"]
                     and 0 <= (date.fromisoformat(q["booking_date"]) - date.fromisoformat(p["booking_date"])).days <= 1
                     and q["id"] > p["id"]), None)
        if twin and amount >= 20 and (p["payee"], amount) not in seen:
            seen.add((p["payee"], amount))
            out.append({"kind": "twice", "payee": p["payee"], "date": twin["booking_date"], "amount_ron": round(amount, 2)})
    for r in conn.execute("""SELECT merchant, last_amount, currency, first_date FROM recurring_series
                             WHERE direction = 'out' AND status = 'active' AND first_date >= ?""",
                          ((start - timedelta(days=62)).isoformat(),)):
        if r["first_date"] >= (start - timedelta(days=62)).isoformat():
            out.append({"kind": "subscription", "payee": r["merchant"], "date": r["first_date"],
                        "amount": r["last_amount"], "currency": r["currency"]})
    order = {"twice": 0, "bigger": 1, "new": 2, "subscription": 3}
    return sorted(out, key=lambda x: order[x["kind"]])


def _coming_up(conn, sent_on: date) -> list[dict]:
    until = sent_on + timedelta(days=6)
    items = [{"kind": "bill", "payee": r["merchant"], "date": r["next_expected"], "amount": r["last_amount"],
              "currency": r["currency"]}
             for r in conn.execute("""SELECT merchant, next_expected, last_amount, currency FROM recurring_series
                                      WHERE direction = 'out' AND status = 'active' AND next_expected BETWEEN ? AND ?
                                      ORDER BY next_expected""", (sent_on.isoformat(), until.isoformat()))]
    for r in conn.execute("""SELECT coalesce(a.display_name, a.alias) AS name, s.due_date, s.min_due_minor, s.total_due_minor
                             FROM card_statements s JOIN accounts a ON a.id = s.account_id
                             WHERE s.period_end = (SELECT max(period_end) FROM card_statements WHERE account_id = s.account_id)
                               AND s.due_date BETWEEN ? AND ?""", (sent_on.isoformat(), until.isoformat())):
        items.append({"kind": "card", "payee": r["name"], "date": r["due_date"], "amount": (r["total_due_minor"] or 0) / 100,
                      "minimum": (r["min_due_minor"] or 0) / 100, "currency": "RON"})
    return sorted(items, key=lambda x: x["date"])


def notification(s: dict) -> tuple[str, str]:
    """(text with amounts, text without): the lock screen shows the second unless details are on."""
    if s["usual_ron"]:
        delta = s["change_pct"]
        how = "about usual" if abs(delta) < 5 else f"{abs(delta):.0f}% {'above' if delta > 0 else 'below'} usual"
        first = f"Week: {s['day_to_day_ron']:,.0f} RON day to day, {how}."
    else:
        first = f"Week: {s['day_to_day_ron']:,.0f} RON day to day."
    if s["regular_ron"] >= 1:
        first += f" Plus {s['regular_ron']:,.0f} RON of regular payments."
    extra = len(s["worth_a_look"])
    more = f" {extra} thing{'s' if extra != 1 else ''} to look at." if extra else ""
    soon = f" {len(s['coming_up'])} payment{'s' if len(s['coming_up']) != 1 else ''} due this week." if s["coming_up"] else ""
    partial = " Some accounts may not be complete yet." if s.get("incomplete") else ""
    return first + more + soon + partial, "Your weekly summary is ready."
