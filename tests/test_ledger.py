from datetime import date

from bankhub import ledger, queries, redaction, sync

from conftest import session, store, tx

PERSON = "Jane Example"
PERSON_IBAN = "RO49AAAA1B31007593840000"


def _setup(conn):
    sync.register_session(conn, session("Revolut", [("rev-ron", "RON", "CACC"), ("rev-sav", "RON", "SVGS")]))
    sync.register_session(conn, session("ING", [("ing-ron", "RON", "CACC")], session_id="sess-2"))
    pocket = "11111111-1111-4111-8111-111111111111"
    store(conn, "rev-ron", [
        tx("45.20", "2026-10-03", name="Lidl Bucuresti"),
        tx("38.00", "2026-10-04", name="Small Bistro SRL"),
        tx("12.00", "2026-10-05", name="Obscure Trading SRL"),
        tx("200", "2026-10-06", name=PERSON, iban=PERSON_IBAN, text=f"rent for {PERSON}, thanks"),
        tx("500", "2026-10-07", text="To RON Savings"),                      # pocket move, one-sided
        tx("300", "2026-10-08", ref=pocket, text="To RON Holiday"),          # pocket move, paired
        tx("1000", "2026-10-11", debit=False, name="ING top-up", text="Top-up"),
        *[tx(p, d, name="Netflix.com") for p, d in
          [("49.99", "2026-06-27"), ("49.99", "2026-07-27"), ("49.99", "2026-08-27"),
           ("49.99", "2026-09-27"), ("59.20", "2026-10-27")]],
    ])
    store(conn, "rev-sav", [tx("300", "2026-10-08", debit=False, ref=pocket, text="To RON Holiday")])
    store(conn, "ing-ron", [tx("1000", "2026-10-10", name="Own Name", text="Transfer to Revolut")])


def test_clean_layer_classification(conn, rules):
    _setup(conn)
    stats = ledger.rebuild(conn, rules, today=date(2026, 11, 5))
    rows = {(r["booking_date"], r["amount"]): dict(r) for r in conn.execute("SELECT * FROM transactions_clean")}

    assert rows[("2026-10-03", -45.2)]["merchant"] == "Lidl"
    assert rows[("2026-10-03", -45.2)]["category"] == "groceries"
    # Keyword rule: categorized, never named.
    assert rows[("2026-10-04", -38.0)]["merchant"] == "unnamed_merchant"
    assert rows[("2026-10-04", -38.0)]["category"] == "restaurants"
    assert rows[("2026-10-05", -12.0)]["merchant"] == "unknown_merchant"
    assert rows[("2026-10-05", -12.0)]["category"] == "uncategorized"
    person = rows[("2026-10-06", -200.0)]
    assert (person["merchant"], person["counterparty_type"]) == ("individual", "individual")
    assert rows[("2026-10-07", -500.0)]["is_internal_transfer"] == 1
    assert rows[("2026-10-08", -300.0)]["is_internal_transfer"] == 1
    assert rows[("2026-10-08", 300.0)]["is_internal_transfer"] == 1
    # ING -> Revolut top-up matched by amount across institutions.
    assert rows[("2026-10-10", -1000.0)]["is_internal_transfer"] == 1
    assert rows[("2026-10-11", 1000.0)]["is_internal_transfer"] == 1
    assert stats["transfer_pairs"] == 2


def test_clean_layer_has_only_allowlisted_columns(conn):
    cols = [r[1] for r in conn.execute("PRAGMA table_info(transactions_clean)")]
    assert tuple(cols) == ledger.CLEAN_COLUMNS


def test_no_names_or_ibans_reach_the_clean_layer(conn, rules):
    _setup(conn)
    ledger.rebuild(conn, rules, today=date(2026, 11, 5))
    dump = " ".join(str(v) for r in conn.execute("SELECT * FROM transactions_clean") for v in tuple(r))
    dump += " ".join(str(v) for r in conn.execute("SELECT * FROM recurring_series") for v in tuple(r))
    for secret in (PERSON, "Jane", PERSON_IBAN, "rent for", "Small Bistro", "Obscure", "Own Name"):
        assert secret not in dump
    assert redaction.scan(conn, rules) == []


def test_spending_excludes_own_transfers(conn, rules):
    _setup(conn)
    ledger.rebuild(conn, rules, today=date(2026, 11, 5))
    s = queries.spending_summary(conn, date_from="2026-10-01", date_to="2026-10-31")
    # Lidl 45.20 + bistro 38 + obscure 12 + person 200 + Netflix 59.20; no pocket moves, no top-up.
    assert s["total_outflow_ron"] == 354.4
    assert s["total_inflow_ron"] == 0
    assert s["internal_transfers_excluded"]["transactions"] == 5


def test_recurring_subscription_with_price_change(conn, rules):
    _setup(conn)
    ledger.rebuild(conn, rules, today=date(2026, 11, 5))
    r = queries.recurring_payments(conn)
    netflix = [s for s in r["series"] if s["merchant"] == "Netflix"]
    assert len(netflix) == 1
    s = netflix[0]
    assert s["cadence"] == "monthly" and s["status"] == "active"
    assert s["next_expected"] == "2026-11-26"
    assert s["price_change_pct"] == 18.4


def test_rejected_pair_counts_as_spending_again(conn, rules):
    _setup(conn)
    ledger.rebuild(conn, rules, today=date(2026, 11, 5))
    d, c = conn.execute("SELECT debit_id, credit_id FROM transfer_pairs WHERE method='amount_match'").fetchone()
    conn.execute("UPDATE transfer_pairs SET confirmed=-1 WHERE debit_id=? AND credit_id=?", (d, c))
    ledger.rebuild(conn, rules, today=date(2026, 11, 5))
    flags = conn.execute("SELECT is_internal_transfer FROM transactions_clean WHERE id IN (?,?)", (d, c)).fetchall()
    assert [f[0] for f in flags] == [0, 0]


def _kinds(conn):
    return {(r["booking_date"], r["amount"]): dict(r) for r in conn.execute("SELECT * FROM transactions_clean")}


def test_one_offs_auto_and_manual(conn, rules):
    sync.register_session(conn, session("Revolut", [("rev-ron", "RON", "CACC")]))
    store(conn, "rev-ron", [tx("75000", "2026-10-02", name="Notary Office", iban=PERSON_IBAN),
                            tx("120", "2026-10-03", name="Lidl"),
                            tx("900", "2026-10-04", name="Furniture Shop SRL")])
    ledger.rebuild(conn, rules, today=date(2026, 11, 5), one_off_ron=50_000)
    rows = _kinds(conn)
    big = rows[("2026-10-02", -75000.0)]
    assert (big["kind"], big["is_one_off"], big["category"]) == ("one_off", 1, "transfers_out")
    s = queries.spending_summary(conn, date_from="2026-10-01", date_to="2026-10-31")
    assert s["total_outflow_ron"] == 1020.0 and len(s["one_offs"]) == 1
    # By hand: the small one becomes a one-off, the big one is forced back into the totals.
    shop = rows[("2026-10-04", -900.0)]["id"]
    conn.execute("INSERT INTO transaction_overrides(raw_id, one_off, updated_at) VALUES (?,1,'x')", (shop,))
    conn.execute("INSERT INTO transaction_overrides(raw_id, one_off, updated_at) VALUES (?,0,'x')", (big["id"],))
    ledger.rebuild(conn, rules, today=date(2026, 11, 5), one_off_ron=50_000)
    s = queries.spending_summary(conn, date_from="2026-10-01", date_to="2026-10-31")
    assert s["total_outflow_ron"] == 75120.0 and [o["amount_ron"] for o in s["one_offs"]] == [-900.0]


def test_refund_reduces_its_category_and_people_money_is_other_in(conn, rules):
    sync.register_session(conn, session("Revolut", [("rev-ron", "RON", "CACC")]))
    store(conn, "rev-ron", [tx("300", "2026-10-02", name="eMAG"),
                            tx("100", "2026-10-09", debit=False, name="eMAG", text="Refund from eMAG"),
                            tx("50", "2026-10-10", debit=False, name=PERSON, iban=PERSON_IBAN)])
    ledger.rebuild(conn, rules, today=date(2026, 11, 5))
    s = queries.spending_summary(conn, date_from="2026-10-01", date_to="2026-10-31")
    assert s["total_outflow_ron"] == 200.0
    assert [(g["name"], g["outflow_ron"]) for g in s["groups"]] == [("shopping", 200.0)]
    assert (s["income_ron"], s["other_money_in_ron"]) == (0, 50.0)


def test_regular_money_in_from_one_payer_is_income(conn, rules):
    sync.register_session(conn, session("BRD", [("brd-ron", "RON", "CACC")]))
    store(conn, "brd-ron", [tx(a, d, debit=False, name="Employer SRL", iban=PERSON_IBAN)
                            for a, d in [("9000", "2026-07-10"), ("9000", "2026-08-10"), ("9100", "2026-09-10"),
                                         ("9100", "2026-10-09")]])
    ledger.rebuild(conn, rules, today=date(2026, 11, 2))
    s = queries.spending_summary(conn, date_from="2026-10-01", date_to="2026-10-31")
    assert (s["income_ron"], s["other_money_in_ron"]) == (9100.0, 0)
    income = queries.recurring_payments(conn, today=date(2026, 11, 2))["income"]
    assert len(income) == 1 and income[0]["merchant"].startswith("regular income")   # payer never named
    assert "Employer" not in str(income)


def test_card_top_up_is_an_own_transfer(conn, rules):
    sync.register_session(conn, session("Revolut", [("rev-ron", "RON", "CACC")]))
    store(conn, "rev-ron", [tx("500", "2026-10-02", debit=False, text="Top-Up by *1234")])
    ledger.rebuild(conn, rules, today=date(2026, 11, 5))
    row = next(iter(_kinds(conn).values()))
    assert (row["is_internal_transfer"], row["kind"]) == (1, "internal")


def test_childs_account_allowance_and_exclusion(conn, rules):
    sync.register_session(conn, session("Revolut", [("me", "RON", "CACC"), ("kid", "RON", "CACC")]))
    conn.execute("UPDATE accounts SET role='dependent', own_ibans='[]' WHERE uid='kid'")
    kid_id = {"other": {"identification": "kid-revolut-id"}}
    store(conn, "me", [
        tx("50", "2026-10-03", name="KID EXAMPLE", other=kid_id["other"], text="To KID EXAMPLE"),   # matched by amount
        tx("20", "2026-10-20", name="KID EXAMPLE", other=kid_id["other"], text="To KID EXAMPLE"),   # learned name
        tx("40", "2026-10-25", debit=False, name="KID EXAMPLE", other=kid_id["other"], text="half of the bike"),
        tx("100", "2026-10-05", name="Lidl"),
    ])
    store(conn, "kid", [
        tx("50", "2026-10-03", debit=False, name="ME EXAMPLE", text="From ME EXAMPLE"),
        tx("30", "2026-10-04", name="Lidl"),
        tx("40", "2026-10-25", name="ME EXAMPLE", other={"identification": "me-id"}, text="half of the bike"),
    ])
    ledger.rebuild(conn, rules, today=date(2026, 11, 5))
    rows = {(r["account_alias"], r["booking_date"], r["amount"]): dict(r) for r in conn.execute("SELECT * FROM transactions_clean")}
    me = lambda d, a: rows[("revolut_ron", d, a)]
    assert (me("2026-10-03", -50.0)["category"], me("2026-10-03", -50.0)["subcategory"]) == ("kids", "allowance")
    assert me("2026-10-20", -20.0)["category"] == "kids"                     # no matching credit: known by name
    assert (me("2026-10-25", 40.0)["kind"], me("2026-10-25", 40.0)["subcategory"]) == ("other_in", "from_kids")
    assert all(r["kind"] == "dependent" and r["is_internal_transfer"] == 0
               for k, r in rows.items() if k[0] == "revolut_ron_2")
    s = queries.spending_summary(conn, date_from="2026-10-01", date_to="2026-10-31")
    assert s["total_outflow_ron"] == 170.0          # 100 Lidl + 50 + 20 allowance; the kid's own Lidl is not counted
    assert s["other_money_in_ron"] == 40.0
    assert "KID" not in str(s) and "EXAMPLE" not in str(s)
    acc = {a["account"]: a for a in []}
    conn.executemany("INSERT INTO balance_snapshots(account_id, fetched_at, balance_type, amount_minor, currency)"
                     " SELECT id, '2026-11-01', 'ITAV', ?, 'RON' FROM accounts WHERE uid=?", [(100000, "me"), (5000, "kid")])
    b = queries.balances(conn)
    assert (b["total_ron"], b["dependent_total_ron"]) == (1000.0, 50.0)
