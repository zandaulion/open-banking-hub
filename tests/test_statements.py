import json
from datetime import date

import pytest

from bankhub import ledger, queries, statements, sync
from bankhub.statements import StatementError, parse_brd_card, store_statement

from conftest import session, store, tx
from statement_fixture import CARD_IBAN, statement

MAIN_IBAN = "RO00TEST0000000000000001"


def test_parses_the_lines_the_summary_and_the_plans():
    st = parse_brd_card(statement(MAIN_IBAN))
    assert (st.period_start, st.period_end, st.due_date, st.iban) == ("2026-09-26", "2026-10-25", "2026-11-19", CARD_IBAN)
    assert (st.owed_minor, st.limit_minor, st.available_minor, st.min_due_minor) == (984984, 1000000, 15016, 35028)
    assert (st.installments_due_minor, st.installments_future_minor) == (70000, 150000)
    assert st.warnings == []
    kinds = [(ln.kind, ln.amount_minor, ln.merchant) for ln in st.lines]
    # The reversal and the plan cancel: the 1 200 purchase stays once, where it was made.
    assert ("purchase", 120000, "Example Shop") in kinds and len(st.lines) == 7
    assert [k for k in kinds if k[1] == 500] == [("purchase", 500, "Example Metro Bucharest ROM")] * 2
    assert ("refund", -1500, "Example Market Bucharest ROM") in kinds
    pay = next(ln for ln in st.lines if ln.kind == "payment")
    assert pay.iban == MAIN_IBAN and pay.date == "2026-09-30"            # an IBAN wrapped over two lines
    assert sorted((p.merchant, p.number, p.remaining_minor) for p in st.plans) == [
        ("Example Flights", "5/6", 50000), ("Example Shop", "1/6", 100000)]


def test_says_so_when_the_lines_do_not_add_up():
    st = parse_brd_card(statement(MAIN_IBAN, used_extra=100))
    assert any("amounts used" in w for w in st.warnings)
    assert any("amounts repaid" in w for w in parse_brd_card(statement(MAIN_IBAN, repaid_extra=5)).warnings)


def test_refuses_other_documents():
    with pytest.raises(StatementError):
        parse_brd_card("Some other statement")
    with pytest.raises(StatementError):
        statements.pdf_text(b"not a pdf")


def _main(conn):
    sync.register_session(conn, session("BRD", [("main", "RON", "CACC")]))
    conn.execute("UPDATE accounts SET own_ibans=? WHERE uid='main'", (json.dumps([MAIN_IBAN]),))


def test_card_account_is_added_once_and_kept_apart(conn, rules):
    _main(conn)
    store(conn, "main", [tx("4000", "2026-09-30", name="Bank", iban=CARD_IBAN)])
    st = parse_brd_card(statement(MAIN_IBAN))
    first = store_statement(conn, st)
    again = store_statement(conn, st)
    assert (first["new"], again["new"], again["already_had"]) == (7, 0, 7)
    acc = conn.execute("SELECT * FROM accounts WHERE source='statement'").fetchone()
    assert (acc["alias"], acc["cash_account_type"], json.loads(acc["own_ibans"])) == ("brd_card", "CARD", [CARD_IBAN])
    assert conn.execute("SELECT count(*) FROM card_statements").fetchone()[0] == 1       # not duplicated
    assert conn.execute("SELECT count(*) FROM transactions_raw WHERE account_id=?", (acc["id"],)).fetchone()[0] == 7
    conn.execute("INSERT INTO balance_snapshots(account_id, fetched_at, balance_type, amount_minor, currency)"
                 " SELECT id, '2026-11-01', 'ITAV', 100000, 'RON' FROM accounts WHERE uid='main'")
    b = queries.balances(conn)
    assert (b["total_ron"], b["card_owed_ron"]) == (1000.0, 9849.84)              # the debt is not in the total
    ledger.rebuild(conn, rules, today=date(2026, 11, 5))
    rows = {(r["account_alias"], r["amount"]): dict(r) for r in conn.execute("SELECT * FROM transactions_clean")}
    assert rows[("brd_card", 4000.0)]["is_internal_transfer"] == 1                  # the repayment
    assert rows[("brd_ron", -4000.0)]["is_internal_transfer"] == 1                  # and its other side
    s = queries.spending_summary(conn, date_from="2026-10-01", date_to="2026-10-31")
    # 1 200 + 2 x 5 + 99.99 + 45: neither the repayment nor the instalment plan counts again. The 15 back
    # from a shop the rules do not know is other money in.
    assert s["total_outflow_ron"] == pytest.approx(1354.99)
    assert s["other_money_in_ron"] == pytest.approx(15.0)


def test_a_newer_statement_is_the_one_shown(conn):
    _main(conn)
    store_statement(conn, parse_brd_card(statement(MAIN_IBAN)))
    older = parse_brd_card(statement(MAIN_IBAN))
    older.period_end, older.period_start, older.owed_minor = "2026-09-25", "2026-08-26", 111100
    store_statement(conn, older)
    assert queries.balances(conn)["card_owed_ron"] == 9849.84
