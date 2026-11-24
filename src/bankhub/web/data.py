"""Data for the owner's UI.

Unlike bankhub.queries (the redacted layer agents see), this module shows the
owner their own data, including payee names and descriptions. It is only ever
served to the owner's registered devices, over a private network.
"""

import json
import re
import sqlite3
from datetime import date, datetime, timedelta, timezone
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
        "SELECT 1 FROM recurring_series WHERE direction = 'in' UNION ALL"
        " SELECT 1 FROM transactions_clean WHERE kind = 'income' AND subcategory = 'salary' LIMIT 1").fetchone() is not None
    return {"month": month, **now, "has_regular_income": has_regular_income, "outflow_ron": now["spent_ron"], "previous": {"month": prev, **before},
            "to_date": to_date, "categories": cats, "groups": group_month(conn, start, end), "payees": payees, "one_offs": one_offs, "trend": trend,
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


EXPORT_COLUMNS = ["date", "value_date", "account", "account_alias", "amount", "currency", "amount_ron", "payee",
                  "description", "category", "subcategory", "kind", "own_transfer", "one_off", "recurring", "status"]


def export_transactions(conn: sqlite3.Connection) -> list[dict]:
    """Every transaction as the owner sees it in the app, oldest first (owner-only: it has names)."""
    return _rows(conn.execute(
        f"""SELECT c.booking_date AS date, t.value_date, coalesce(a.display_name, c.account_alias) AS account,
                   c.account_alias, c.amount, c.currency, c.amount_ron, {PAYEE} AS payee, t.remittance AS description,
                   c.category, c.subcategory, c.kind, c.is_internal_transfer AS own_transfer, c.is_one_off AS one_off,
                   c.is_recurring AS recurring, c.status
            FROM transactions_clean c JOIN transactions_raw t ON t.id = c.id JOIN accounts a ON a.id = t.account_id
            ORDER BY c.booking_date, c.id"""))


def export_everything(conn: sqlite3.Connection, rules_text: str) -> dict:
    """The owner's data in one file: accounts, transactions, groups, cards, recurring payments and their own rules.
    Nothing secret: no keys, tokens, consents or devices."""
    return {
        "format": "bankhub-export-1",
        "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "accounts": _rows(conn.execute(
            """SELECT a.alias, coalesce(a.display_name, a.alias) AS name, a.institution, a.currency, a.cash_account_type,
                      a.source, g.name AS group_name FROM accounts a LEFT JOIN account_groups g ON g.id = a.group_id
               ORDER BY a.alias""")),
        "balances": queries.balances(conn)["accounts"],
        "transactions": export_transactions(conn),
        "groups": [{k: g[k] for k in ("name", "behaviour", "category", "subcategory", "money_back", "sent_as", "payees", "ignored", "accounts")}
                   for g in groups(conn)],
        "card_statements": _rows(conn.execute(
            """SELECT a.alias AS account, s.period_start, s.period_end, s.owed_minor / 100.0 AS owed, s.limit_minor / 100.0 AS credit_limit,
                      s.due_date, s.min_due_minor / 100.0 AS minimum_due FROM card_statements s JOIN accounts a ON a.id = s.account_id
               ORDER BY a.alias, s.period_end""")),
        "recurring": _rows(conn.execute("SELECT * FROM recurring_series ORDER BY direction, merchant")),
        "net_worth": queries.net_worth_history(conn)["months"],
        "rules_yaml": rules_text,
    }


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
    group_of = dict(conn.execute("SELECT alias, group_id FROM accounts"))
    history = {r[0]: r[1] for r in conn.execute(
        "SELECT a.alias, min(t.booking_date) FROM accounts a JOIN transactions_raw t ON t.account_id = a.id GROUP BY a.id")}
    for b in balances["accounts"]:
        b["alias"] = b["account"]
        b["name"] = names.get(b["account"]) or b["account"]
        b["history_from"] = history.get(b["account"])
        b["group_id"] = group_of.get(b["account"])
    gs = groups(conn)
    for g in gs:
        g["accounts"] = json.loads(g["accounts"])
    return {"balances": balances, "status": queries.sync_status(conn), "groups": gs, "cards": cards(conn),
            "net_worth": queries.net_worth_history(conn)}


def cards(conn: sqlite3.Connection) -> list[dict]:
    """Credit cards added from statements: what the latest statement says (owner-only: plans name merchants)."""
    out = []
    for a in conn.execute("SELECT id, alias, coalesce(display_name, alias) AS name, institution FROM accounts"
                          " WHERE source = 'statement' AND active = 1 ORDER BY alias"):
        s = conn.execute("SELECT * FROM card_statements WHERE account_id = ? ORDER BY period_end DESC LIMIT 1",
                         (a["id"],)).fetchone()
        if not s:
            continue
        first = conn.execute("SELECT min(period_start), count(*) FROM card_statements WHERE account_id = ?", (a["id"],)).fetchone()
        money = lambda v: None if v is None else v / 100
        out.append({
            "alias": a["alias"], "name": a["name"], "institution": a["institution"], "period_end": s["period_end"],
            "statements": first[1], "history_from": first[0],
            "owed_ron": money(s["owed_minor"]), "limit_ron": money(s["limit_minor"]), "available_ron": money(s["available_minor"]),
            "total_due_ron": money(s["total_due_minor"]), "min_due_ron": money(s["min_due_minor"]), "due_date": s["due_date"],
            "installments_due_ron": money(s["installments_due_minor"]),
            "installments_future_ron": money(s["installments_future_minor"]),
            "plans": [{"merchant": p["merchant"], "started": p["started"], "initial_ron": money(p["initial_minor"]),
                       "installment_ron": money(p["installment_minor"]), "number": p["number"],
                       "remaining_ron": money(p["remaining_minor"])}
                      for p in conn.execute("SELECT * FROM card_plans WHERE statement_id = ? ORDER BY started", (s["id"],))]})
    return out


BEHAVIOURS = ("supported", "separate")


def groups(conn: sqlite3.Connection) -> list[dict]:
    out = _rows(conn.execute(
        """SELECT g.*, (SELECT json_group_array(alias) FROM accounts WHERE group_id = g.id) AS accounts
           FROM account_groups g ORDER BY g.name"""))
    for g in out:
        g["payees"], g["ignored"] = json.loads(g["payees"]), json.loads(g["ignored"])
        # The names my payments to and from this group went to, however they were found (owner-only).
        g["found"] = _rows(conn.execute(
            """SELECT t.counterparty_name AS name, count(*) AS n FROM transaction_groups tg
               JOIN transactions_raw t ON t.id = tg.raw_id JOIN accounts a ON a.id = t.account_id
               WHERE tg.group_id = ? AND a.group_id IS NULL AND t.counterparty_has_account = 1
                     AND t.counterparty_name IS NOT NULL
               GROUP BY lower(t.counterparty_name) ORDER BY n DESC""", (g["id"],)))
    return out


def save_group(conn: sqlite3.Connection, group_id: int | None, body: dict) -> int:
    """Creates or updates a group. body: name, behaviour, category, subcategory, money_back, sent_as, accounts."""
    name = (body.get("name") or "").strip()[:40]
    if not name:
        raise ValueError("A group needs a name.")
    if body.get("behaviour") not in BEHAVIOURS:
        raise ValueError("behaviour must be 'supported' or 'separate'")
    if body.get("money_back", "money_in") not in ("money_in", "reduce") or \
            body.get("sent_as", "spending") not in ("spending", "transfer"):
        raise ValueError("unknown option")
    values = (name, body["behaviour"], body.get("category") or "family", body.get("subcategory"),
              body.get("money_back", "money_in"), body.get("sent_as", "spending"))
    try:
        if group_id is None:
            group_id = conn.execute("INSERT INTO account_groups(name, behaviour, category, subcategory, money_back, sent_as)"
                                    " VALUES (?,?,?,?,?,?)", values).lastrowid
        elif not conn.execute("UPDATE account_groups SET name=?, behaviour=?, category=?, subcategory=?, money_back=?,"
                              " sent_as=? WHERE id=?", (*values, group_id)).rowcount:
            raise LookupError(group_id)
    except sqlite3.IntegrityError:
        raise ValueError("Another group already has that name.")
    names = lambda key: json.dumps(sorted({" ".join(n.split())[:80] for n in body.get(key) or [] if n.strip()}))
    conn.execute("UPDATE account_groups SET payees=?, ignored=? WHERE id=?", (names("payees"), names("ignored"), group_id))
    if "accounts" in body:
        conn.execute("UPDATE accounts SET group_id = NULL WHERE group_id = ?", (group_id,))
        conn.executemany("UPDATE accounts SET group_id = ? WHERE alias = ?", [(group_id, a) for a in body["accounts"]])
    return group_id


def delete_group(conn: sqlite3.Connection, group_id: int) -> bool:
    """Its accounts become mine again."""
    conn.execute("UPDATE accounts SET group_id = NULL WHERE group_id = ?", (group_id,))
    return conn.execute("DELETE FROM account_groups WHERE id = ?", (group_id,)).rowcount > 0


def group_month(conn: sqlite3.Connection, start: str, end: str) -> list[dict]:
    """What went to and came back from each group in a month (owner-only: groups have names)."""
    return _rows(conn.execute(
        f"""SELECT g.id, g.name, g.behaviour, g.category, g.subcategory,
                   round(-sum(CASE WHEN c.amount_ron < 0 THEN c.amount_ron ELSE 0 END), 2) AS sent_ron,
                   round(sum(CASE WHEN c.amount_ron > 0 THEN c.amount_ron ELSE 0 END), 2) AS back_ron,
                   count(*) AS n
            FROM transaction_groups tg JOIN account_groups g ON g.id = tg.group_id
            JOIN transactions_clean c ON c.id = tg.raw_id
            WHERE {BOOKED} AND c.booking_date BETWEEN ? AND ? GROUP BY g.id ORDER BY sent_ron DESC""", (start, end)))


def rename_account(conn: sqlite3.Connection, alias: str, name: str | None) -> bool:
    name = (name or "").strip()[:40] or None
    return conn.execute("UPDATE accounts SET display_name=? WHERE alias=?", (name, alias)).rowcount > 0


def suggest_pattern(example: str) -> str:
    """A pattern that is a real substring of the payee text and keeps what tells products apart:
    'Ep*example.ro' -> 'example.ro' (a payment processor before the '*'),
    'Google*cloud Vcpv4x' -> 'google*cloud' (a brand, then its product), 'Google Cloud V3nlhx' -> 'google cloud'."""
    text = (example or "").lower().strip()
    if "*" in text:
        star = text.index("*")
        head = text[:star].strip()
        if len(head) <= 5:
            text = text[star + 1:].strip()
        else:
            rest = text[star + 1:]
            words = rest.split()
            take = words[:1]
            # "Google *google Play": the brand repeated after the '*' says nothing; keep the next word too.
            if take and take[0] == head.split()[-1] and len(words) > 1 and not any(ch.isdigit() for ch in words[1]):
                take = words[:2]
            if take and not any(ch.isdigit() for ch in take[0]):
                last = take[-1]
                end = rest.index(last, rest.index(take[0])) + len(last)
                # Up to the end of the product, exactly as written (with or without a space before '*').
                return text[:star + 1 + end]
            text = head
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
               WHERE c.category = 'uncategorized' AND c.direction = 'debit' AND c.status = 'booked'
                 AND c.kind = 'spending'"""):
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
            FROM transactions_clean c WHERE {BASE} AND amount_ron < 0 AND c.kind = 'spending'""").fetchone()[0]
    return {"items": items[:limit], "remaining": len(items), "uncategorized_share_pct": share or 0.0}


def category_choices(conn: sqlite3.Connection, rules_text: str) -> list[str]:
    cats = {r[0] for r in conn.execute("SELECT DISTINCT category FROM transactions_clean")}
    data = yaml.safe_load(rules_text) or {}
    cats |= {m["category"] for m in data.get("merchants") or []}
    cats |= {c["category"] for c in data.get("category_rules") or []}
    return sorted(cats - {"uncategorized", "transfers", "income"})


def problems(h: dict, now: datetime | None = None) -> list[str]:
    """What needs a look, from `health()`: a bank silent for two days, backups that stopped, a card statement
    over 40 days old. Shown on Accounts and as a banner on Month."""
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(hours=48)).isoformat()
    out = []
    stale: dict[str, list[str | None]] = {}
    for a in h["accounts"]:
        if not a["last_ok"] or a["last_ok"] < cutoff:
            stale.setdefault(a["institution"], []).append(a["last_ok"])
    for bank, seen in stale.items():
        known = [x for x in seen if x]
        out.append(f"{bank} has not synced since {max(known)[:10]}" if known else f"{bank} has never synced")
    if h.get("backup_local") and h["backup_local"] < cutoff:
        out.append(f"The last backup is from {h['backup_local'][:10]}")
    for st in h.get("statements", []):
        if st["period_end"] < (now - timedelta(days=40)).date().isoformat():
            out.append(f"{st['name']}: the latest statement ended {st['period_end']}")
    return out


def health(conn: sqlite3.Connection, data_dir: Path) -> dict:
    """Is each bank connection alive, how far back is history, and when did backups last run."""
    from ..sync import BACKFILL_DAYS, RECENT_DAYS, WINDOWED, backfill_done
    target = (date.today() - timedelta(days=BACKFILL_DAYS)).isoformat()
    accounts = []
    for a in conn.execute(
            """SELECT a.id, a.alias, coalesce(a.display_name, a.alias) AS name, a.institution, a.history_from,
                      a.backfill_from, a.backfill_until, a.recent_cursor,
                      (SELECT min(booking_date) FROM transactions_raw WHERE account_id = a.id) AS oldest,
                      (SELECT max(at) FROM sync_results WHERE account_id = a.id AND skipped IS NULL) AS last_ok,
                      (SELECT max(ts) FROM api_requests WHERE account_id = a.id AND kind = 'transactions') AS last_request
               FROM accounts a WHERE a.active = 1 AND a.source = 'bank' ORDER BY a.institution, a.alias"""):
        last = conn.execute("SELECT at, skipped FROM sync_results WHERE account_id=? ORDER BY id DESC LIMIT 1",
                            (a["id"],)).fetchone()
        accounts.append({"name": a["name"], "institution": a["institution"],
                         "last_ok": a["last_ok"] or a["last_request"], "oldest": a["oldest"],
                         # Banks fetched in windows fill in their history over several syncs.
                         "backfilling": a["institution"] in WINDOWED and not backfill_done(a),
                         # Too old for unattended access: only a new link brings it (see sync._sync_windowed).
                         "needs_new_link": a["institution"] in WINDOWED and not backfill_done(a) and bool(a["backfill_until"])
                                           and a["backfill_until"] < (date.today() - timedelta(days=RECENT_DAYS)).isoformat(),
                         # Older history is fetched going forward from backfill_from: complete up to backfill_until.
                         "backfill": {"from": a["backfill_from"], "until": a["backfill_until"], "to": a["history_from"],
                                      "recent_pending": a["recent_cursor"]},
                         "last_skipped": last["skipped"] if last and last["skipped"] else None,
                         "last_skipped_at": last["at"] if last and last["skipped"] else None})
    statements = [{"name": r["name"], "period_end": r["period_end"]} for r in conn.execute(
        """SELECT coalesce(a.display_name, a.alias) AS name, max(s.period_end) AS period_end FROM accounts a
           JOIN card_statements s ON s.account_id = a.id WHERE a.active = 1 GROUP BY a.id ORDER BY a.alias""")]
    run = conn.execute("SELECT started_at, status, error FROM sync_runs ORDER BY id DESC LIMIT 1").fetchone()
    backups = data_dir / "backups"
    snaps = sorted(p.name for p in backups.iterdir() if p.is_dir() and len(p.name) == 16) if backups.exists() else []
    local = f"{snaps[-1][:4]}-{snaps[-1][4:6]}-{snaps[-1][6:8]}T{snaps[-1][9:11]}:{snaps[-1][11:13]}:00+00:00" \
        if snaps else None
    marker = backups / ".offsite-ok"
    offsite = marker.read_text().strip() if marker.exists() else None
    return {"accounts": accounts, "statements": statements, "history_target": target, "last_run": dict(run) if run else None,
            "backup_local": local, "backup_offsite": offsite}
