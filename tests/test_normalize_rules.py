from bankhub.fx import parse_bnr_xml
from bankhub.normalize import normalize
from bankhub.sync import extract_code

from conftest import tx


def test_debit_is_negative_and_counterparty_is_creditor():
    n = normalize(tx("12.34", "2026-01-05", name="Lidl Bucuresti"))
    assert n.amount_minor == -1234
    assert n.counterparty_name == "Lidl Bucuresti"
    assert not n.counterparty_has_account


def test_credit_counterparty_is_debtor_with_account():
    n = normalize(tx("100", "2026-01-05", debit=False, name="Jane Example", iban="RO49AAAA1B31007593840000"))
    assert n.amount_minor == 10000
    assert n.counterparty_name == "Jane Example"
    assert n.counterparty_has_account


def test_dedup_key_falls_back_to_hash_without_reference():
    a = tx("5", "2026-01-05", name="X")
    a["entry_reference"] = None
    assert normalize(a).dedup_key.startswith("hash:")
    assert normalize(a).dedup_key == normalize(dict(a)).dedup_key


def test_extract_code_accepts_url_or_bare_code():
    url = "https://enablebanking.com/?state=abc&code=11111111-2222-3333-4444-555555555555"
    assert extract_code(url) == "11111111-2222-3333-4444-555555555555"
    assert extract_code("  abc-code ") == "abc-code"


def test_plain_patterns_need_a_word_start(rules):
    assert rules.match_merchant("Netflix.com", None).name == "Netflix"
    assert rules.match_merchant("Thbobs shop", None) is None   # "hbo" inside a word


def test_bank_transfers_ignore_the_description(rules):
    # A personal message mentioning a merchant-like word must not name the payee.
    assert rules.match_merchant("Jane Example", "netflix money back", has_account=True) is None
    assert rules.match_merchant("Jane Example", "netflix money back", has_account=False).name == "Netflix"


def test_first_name_merchants_never_match_transfers(rules):
    assert rules.match_merchant("Luca Example", None, has_account=True) is None
    assert rules.match_merchant("Luca", None, has_account=False).name == "Luca"


def test_bnr_xml_applies_multiplier():
    xml = """<?xml version="1.0" encoding="utf-8"?>
<DataSet xmlns="https://www.bnr.ro/xsd"><Body><Cube date="2026-01-05">
<Rate currency="EUR">5.0905</Rate><Rate currency="HUF" multiplier="100">1.3247</Rate>
</Cube></Body></DataSet>"""
    rows = dict(((d, c), r) for d, c, r in parse_bnr_xml(xml))
    assert rows[("2026-01-05", "EUR")] == 5.0905
    assert abs(rows[("2026-01-05", "HUF")] - 0.013247) < 1e-9


def test_user_rules_extend_defaults(tmp_path):
    from bankhub.rules import load_rules
    user = tmp_path / "categories.yaml"
    user.write_text("merchants:\n  - {name: Lidl Override, category: groceries, patterns: [lidl]}\n"
                    "category_rules:\n  - {category: sports, patterns: [corner gym]}\n")
    r = load_rules(user)
    assert r.match_merchant("Lidl", None).name == "Lidl Override"      # user rules win
    assert r.match_merchant("Netflix.com", None).name == "Netflix"     # defaults still there
    assert r.match_category("The Corner Gym", None).category == "sports"


def test_group_key_handles_processor_prefixes():
    from bankhub.recurring import group_key
    assert group_key("Ep*amparcat.ro", None) != group_key("Ep*premierenergy.ro", None)
    assert group_key("Netflix*ab12cd", None) == group_key("Netflix*zz99yy", None) == "netflix"


def _ing_tx(text, debit=True, code="POS purchase", amount="25.00"):
    t = tx(amount, "2026-01-05", debit=debit, text=text)
    t["bank_transaction_code"] = {"code": None, "sub_code": None, "description": code}
    t["creditor_account"] = {"iban": "RO00INGB0000000000000001", "other": None}   # ING fills its own side here
    return t


def test_ing_card_payment_reads_the_merchant():
    n = normalize(_ing_tx("Card number, **** 1234, Transaction at, CORNER SHOP 12  RO  Townville, "
                          "Authorization date, 01-01-2026, Authorization number, 123456"), "ING")
    assert n.counterparty_name == "CORNER SHOP 12"
    assert not n.counterparty_has_account and n.counterparty_iban is None
    assert n.bank_code == "POS purchase"


def test_ing_transfer_reads_beneficiary_and_iban():
    n = normalize(_ing_tx("Reference number, 999, Beneficiary, Jane Example, To account, RO49AAAA1B31007593840000, "
                          "Details, rent, november", code="Transfer Home'Bank"), "ING")
    assert (n.counterparty_name, n.counterparty_iban) == ("Jane Example", "RO49AAAA1B31007593840000")
    assert n.counterparty_has_account and n.remittance == "rent, november"
    incoming = normalize(_ing_tx("Ordering party, Some Employer SRL, From account, RO49AAAA1B31007593840000",
                                 debit=False, code="Incoming funds"), "ING")
    assert incoming.counterparty_name == "Some Employer SRL"


def test_bank_code_rules(rules):
    assert rules.match_bank_code("Credit Repayment", debit=True).category == "loans"
    assert rules.match_bank_code("Deposit creation", debit=True).internal
    assert rules.match_bank_code("Interest adjustment", debit=False).category == "income"
    assert rules.match_bank_code("POS purchase", debit=True) is None


def _brd_tx(text, debit=True, name=None, other=None):
    t = tx("30", "2026-01-05", debit=debit, name=name, text=text)
    if other:
        t["creditor_account"] = {"iban": None, "other": other}
    t["bank_transaction_code"] = {"code": "BRDEROBU", "sub_code": None, "description": None}
    return t


def test_brd_card_payment_takes_the_shop_not_the_cardholder():
    n = normalize(_brd_tx("MCY-Plata comerciant RO|Card/Terminale|OP 1/2|JANE EXAMPLE|Card nr....1234|CORNER SHOP SRL|",
                          name="JANE EXAMPLE", other={"identification": "550000******1234"}), "BRD")
    assert (n.counterparty_name, n.counterparty_has_account) == ("CORNER SHOP SRL", False)
    assert n.bank_code == "MCY-Plata comerciant RO"
    unknown = normalize(_brd_tx("MCY-Plata comerciant UE/Intnal|Card/Terminale|OP 1/2|JANE EXAMPLE|9|",
                                name="JANE EXAMPLE"), "BRD")
    assert unknown.counterparty_name is None      # never the cardholder


def test_brd_transfer_reads_party_iban_and_details():
    n = normalize(_brd_tx("Plata instant|Mobile Omnichannel|OPH1|John Example|RO49AAAA1B31007593840000|"
                          "Some Bank|Transfer|rent november|"), "BRD")
    assert (n.counterparty_name, n.counterparty_iban, n.counterparty_has_account) == \
        ("John Example", "RO49AAAA1B31007593840000", True)
    assert "rent november" in n.remittance


def test_brd_rules(rules):
    assert rules.match_bank_code("Plata dobanda limita credit", debit=True).category == "loans"
    assert rules.match_bank_code("Incasare din schimb valutar", debit=False).internal
    assert rules.match_bank_code("Retrageri de Numerar-ATM/MBA BRD", debit=True).category == "cash"
    assert rules.is_own_account_move("BRD", "REVOLUT**1234* VILNIUS LTU")
