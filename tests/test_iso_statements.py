import pytest

from bankhub import ledger, statements, sync
from bankhub.statements import NotLinked, StatementError, parse_statement

from conftest import session, store, tx
from reader_conformance import check_reader

IBAN = "RO00TEST0000000000000321"


def camt(closing="1234.50", version="08"):
    """An invented CAMT.053 statement: a salary, a card payment and a transfer, opening 500.00."""
    entry = lambda amt, ind, day, party, iban, text: f"""
      <Ntry><Amt Ccy="RON">{amt}</Amt><CdtDbtInd>{ind}</CdtDbtInd><Sts><Cd>BOOK</Cd></Sts>
        <BookgDt><Dt>{day}</Dt></BookgDt><ValDt><Dt>{day}</Dt></ValDt>
        <NtryDtls><TxDtls>
          <RltdPties><{party[0]}><Pty><Nm>{party[1]}</Nm></Pty></{party[0]}>{f"<{party[0]}Acct><Id><IBAN>{iban}</IBAN></Id></{party[0]}Acct>" if iban else ""}</RltdPties>
          <RmtInf><Ustrd>{text}</Ustrd></RmtInf>
        </TxDtls></NtryDtls></Ntry>"""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Document xmlns="urn:iso:std:iso:20022:tech:xsd:camt.053.001.{version}"><BkToCstmrStmt>
  <GrpHdr><MsgId>EXAMPLE-1</MsgId></GrpHdr>
  <Stmt><Id>1</Id>
    <FrToDt><FrDtTm>2026-10-01T00:00:00</FrDtTm><ToDtTm>2026-10-31T23:59:59</ToDtTm></FrToDt>
    <Acct><Id><IBAN>{IBAN}</IBAN></Id><Ccy>RON</Ccy></Acct>
    <Bal><Tp><CdOrPrtry><Cd>OPBD</Cd></CdOrPrtry></Tp><Amt Ccy="RON">500.00</Amt><CdtDbtInd>CRDT</CdtDbtInd><Dt><Dt>2026-10-01</Dt></Dt></Bal>
    <Bal><Tp><CdOrPrtry><Cd>CLBD</Cd></CdOrPrtry></Tp><Amt Ccy="RON">{closing}</Amt><CdtDbtInd>CRDT</CdtDbtInd><Dt><Dt>2026-10-31</Dt></Dt></Bal>
    {entry("1000.00", "CRDT", "2026-10-10", ("Dbtr", "Example Employer SRL"), "RO00TEST0000000000000999", "Salary October")}
    {entry("45.50", "DBIT", "2026-10-12", ("Cdtr", "Example Market"), None, "Card payment")}
    {entry("220.00", "DBIT", "2026-10-15", ("Cdtr", "Jane Example"), "RO00TEST0000000000000555", "Rent share")}
    <Ntry><Amt Ccy="RON">10.00</Amt><CdtDbtInd>DBIT</CdtDbtInd><Sts><Cd>PDNG</Cd></Sts><BookgDt><Dt>2026-10-31</Dt></BookgDt></Ntry>
  </Stmt>
</BkToCstmrStmt></Document>"""


OFX1 = """OFXHEADER:100
DATA:OFXSGML
VERSION:102

<OFX><BANKMSGSRSV1><STMTTRNRS><STMTRS><CURDEF>RON
<BANKACCTFROM><BANKID>EXAMPLE<ACCTID>12345678</BANKACCTFROM>
<BANKTRANLIST><DTSTART>20261001<DTEND>20261031
<STMTTRN><TRNTYPE>DEBIT<DTPOSTED>20261012120000<TRNAMT>-45.50<FITID>A1<NAME>Example Market<MEMO>Card payment</STMTTRN>
<STMTTRN><TRNTYPE>CREDIT<DTPOSTED>20261010<TRNAMT>1000.00<FITID>A2<NAME>Example Employer SRL</STMTTRN>
</BANKTRANLIST><LEDGERBAL><BALAMT>1454.50<DTASOF>20261031</LEDGERBAL>
</STMTRS></STMTTRNRS></BANKMSGSRSV1></OFX>
"""


def test_camt_reads_parties_dates_and_checks_the_balances():
    st = parse_statement(camt())
    assert (st.iban, st.currency, st.period_start, st.period_end, st.warnings) == (IBAN, "RON", "2026-10-01", "2026-10-31", [])
    assert [(ln.date, ln.amount_minor, ln.counterparty) for ln in st.lines] == [
        ("2026-10-10", 100000, "Example Employer SRL"), ("2026-10-12", -4550, "Example Market"), ("2026-10-15", -22000, "Jane Example")]
    assert st.lines[2].iban == "RO00TEST0000000000000555" and st.lines[0].text == "Salary October"
    assert parse_statement(camt(version="02")).lines == st.lines                       # older versions, same fields


def test_ofx_version_1_is_read():
    st = parse_statement(OFX1)
    assert (st.iban, st.currency, st.period_start, st.period_end, st.end_minor) == ("12345678", "RON", "2026-10-01", "2026-10-31", 145450)
    assert [(ln.date, ln.amount_minor, ln.counterparty) for ln in st.lines] == [("2026-10-12", -4550, "Example Market"),
                                                                                ("2026-10-10", 100000, "Example Employer SRL")]


def test_the_standard_readers_follow_the_reader_convention():
    check_reader("camt053", camt(), camt(closing="1234.60"))
    check_reader("ofx", OFX1, None)


def test_xml_with_a_doctype_is_refused():
    evil = '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><Document><BkToCstmrStmt/></Document>'
    with pytest.raises(StatementError):
        parse_statement(evil)


def test_a_camt_for_a_linked_iban_is_stored_once_and_an_unknown_account_can_be_named(conn, rules):
    sync.register_session(conn, session("Example Bank", [("acc", "RON", "CACC")]))
    alias = conn.execute("SELECT alias FROM accounts").fetchone()[0]
    st = parse_statement(OFX1)                                  # ACCTID is not an IBAN Bank Hub knows
    with pytest.raises(NotLinked):
        statements.store(conn, st)
    st.alias = alias
    assert statements.store(conn, st)["new"] == 2
    conn.execute("UPDATE accounts SET own_ibans=?", (f'["{IBAN}"]',))
    store(conn, "acc", [tx("1000", "2026-10-10", debit=False, name="Example Employer SRL")])   # the sync already has it
    r = statements.store(conn, parse_statement(camt()))
    assert (r["new"], r["already_had"]) == (1, 2)               # only the rent is new: the rest came with the OFX or the sync
    names = {r[0] for r in conn.execute("SELECT counterparty_name FROM transactions_raw")}
    assert {"Example Market", "Jane Example", "Example Employer SRL"} <= names
    ledger.rebuild(conn, rules)
