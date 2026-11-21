import json

import pytest

from bankhub import ledger, queries, statements, sync
from bankhub.statements import StatementError, csv_import

from conftest import session, store, tx

SEMICOLON = """Data;Descriere;Beneficiar;Suma;Sold
01.10.2026;Plata card Example Market;Example Market SRL;-45,50;954,50
02.10.2026;Salariu octombrie;Example Employer SRL;2.000,00;2.954,50
03.10.2026;Transfer;Jane Example;-120,00;2.834,50
"""
SPLIT = """Date,Description,Debit,Credit,Balance
2026-10-01,Coffee,3.50,,996.50
2026-10-02,Salary,,2000.00,2996.50
2026-10-03,Rent,800.00,,2196.50
"""


def test_sniffing_finds_the_columns_of_two_different_layouts():
    a = csv_import.sniff(SEMICOLON)["guess"]
    assert (a["delimiter"], a["skip"], a["date"], a["date_format"], a["decimal"], a["amount"]) == (";", 1, 0, "%d.%m.%Y", ",", 3)
    assert a["description"] in (1, 2)
    b = csv_import.sniff(SPLIT)["guess"]
    assert (b["delimiter"], b["date_format"], b["decimal"], b["debit"], b["credit"], b["amount"]) == (",", "%Y-%m-%d", ".", 2, 3, None)


@pytest.mark.parametrize("text, decimal, cents", [("1.234,56", ",", 123456), ("-45,5", ",", -4550), ("(12.50)", ".", -1250),
                                                  ("12,50-", ",", -1250), ("1,234.56 RON", ".", 123456), ("", ".", None), ("abc", ".", None)])
def test_numbers(text, decimal, cents):
    assert csv_import.parse_number(text, decimal) == cents


def _mapping(text, **over):
    return {**csv_import.sniff(text)["guess"], **over}


def test_a_csv_fills_the_gaps_once_and_the_sync_replaces_it(conn, rules):
    sync.register_session(conn, session("ING", [("ing", "RON", "CACC")]))
    store(conn, "ing", [tx("120", "2026-10-04", name="Jane Example")])          # the sync already has the transfer, a day off
    st = csv_import.statement_from_csv(SEMICOLON, _mapping(SEMICOLON, description=1, counterparty=2),
                                       bank="ING", alias="ing_ron", currency="RON")
    assert (st.period_start, st.period_end, st.warnings) == ("2026-10-01", "2026-10-03", [])
    first, again = statements.store(conn, st), statements.store(conn, st)
    assert (first["new"], first["already_had"], again["new"]) == (2, 1, 0) and first["account"]
    assert sorted(r[0] for r in conn.execute("SELECT amount_minor FROM transactions_raw")) == [-12000, -4550, 200000]
    assert conn.execute("SELECT count(*) FROM transactions_raw WHERE counterparty_name='Example Market SRL'").fetchone()[0] == 1
    ledger.rebuild(conn, rules)
    assert queries.spending_summary(conn, date_from="2026-10-01", date_to="2026-10-31")["total_outflow_ron"] == pytest.approx(165.5)
    # The sync later fetches the card payment itself: its own record replaces the CSV's line.
    store(conn, "ing", [tx("45.50", "2026-10-01", name="Example Market SRL")])
    assert conn.execute("SELECT count(*) FROM transactions_raw").fetchone()[0] == 3


def test_split_debit_and_credit_columns_and_an_inverted_sign(conn):
    sync.register_session(conn, session("Revolut", [("rev", "RON", "CACC")]))
    st = csv_import.statement_from_csv(SPLIT, _mapping(SPLIT), bank="Revolut", alias="revolut_ron", currency="RON")
    assert [ln.amount_minor for ln in st.lines] == [-350, 200000, -80000]
    flipped = csv_import.statement_from_csv(SPLIT, _mapping(SPLIT, invert=True), bank="Revolut", alias="revolut_ron", currency="RON")
    assert [ln.amount_minor for ln in flipped.lines] == [350, -200000, 80000]


def test_bad_input_is_refused_without_quoting_the_file(conn):
    for bad in ({"date": None}, {"decimal": "x"}, {"amount": None, "debit": None}, {"date_format": None}):
        with pytest.raises(StatementError):
            csv_import.statement_from_csv(SEMICOLON, _mapping(SEMICOLON, **bad), bank="ING", alias="a", currency="RON")
    with pytest.raises(StatementError, match="Most rows"):
        csv_import.statement_from_csv(SEMICOLON, _mapping(SEMICOLON, date_format="%Y-%m-%d"), bank="ING", alias="a", currency="RON")
    sync.register_session(conn, session("ING", [("ing", "RON", "CACC")]))
    with pytest.raises(StatementError, match="not linked"):
        statements.store(conn, csv_import.statement_from_csv(SEMICOLON, _mapping(SEMICOLON), bank="ING", alias="nope", currency="RON"))


def test_headings_decide_the_description_and_the_payee():
    text = "Date;Description;Payee;Amount\n03.11.2026;Card;A much longer payee name SRL;-45,50\n04.11.2026;Rent;Another long payee name SRL;-120,00\n"
    g = csv_import.sniff(text)["guess"]
    assert (g["description"], g["counterparty"]) == (1, 2)
