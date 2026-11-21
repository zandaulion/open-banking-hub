"""An invented BRD credit card statement, laid out like the real one (as pdftotext -layout writes it)."""

CARD_IBAN = "RO00TEST0000000000000777"
LOAN_IBAN = "RO00TEST0000000000000444"       # the loan account a repayment goes to


def _money(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    whole, frac = divmod(abs(cents), 100)
    return f"{sign}{whole:,}".replace(",", ".") + f",{frac:02d}"


def _row(date: str, settled: str, kind: str, details: list[str], kind2: str | None = None, cents: int = 0) -> list[str]:
    first = f"{date}       {settled}       " + kind.ljust(18 if kind.startswith("Rate") else 19) + details[0]
    out = [first.ljust(108) + _money(cents).rjust(30)]
    for i, d in enumerate(details[1:]):
        out.append(" " * 34 + ((kind2 or "") if i == 0 else "").ljust(19) + d)
    return out


def statement(payer_iban: str, repaid_extra: int = 0, used_extra: int = 0) -> str:
    """Two same-day rides, a subscription, a 1 200 RON purchase turned into instalments, a refund and a repayment."""
    pos = "Utilizare POS"
    shop = lambda m, ref="1234567/12345": f"Card/Terminale/OP{ref}/{m}/Card"
    rows = []
    rows += _row("30/09/2026", "30/09/2026", "Incasare aceeasi", [
        f"Mobile Omnichannel/OPH123456789012345/{payer_iban[:20]}", f"{payer_iban[20:]}Transfer"], "sucursala BRD", -400000)
    rows += _row("02/10/2026", "03/10/2026", pos, [shop("Example Shop"), "nr....1234"], "comerciant BRD", 120000)
    rows += _row("03/10/2026", "03/10/2026", pos, ["Card/Terminale/NC1234567890123456/Example Shop /", "stornare plata in rate"],
                 "comerciant BRD", -120000)
    rows += _row("03/10/2026", "03/10/2026", "Rate fara dobanda", ["Card/Terminale/NC1234567890123456/Example Shop / Nr.",
                                                                 "rate: 6 Dob: 0.0000%"], None, 120000)
    rows += _row("05/10/2026", "07/10/2026", pos, [shop("Example Metro Bucharest ROM"), "nr....1234"], "comerciant alte BC", 500)
    rows += _row("05/10/2026", "07/10/2026", pos, [shop("Example Metro Bucharest ROM"), "nr....1234"], "comerciant alte BC", 500)
    rows += _row("13/10/2026", "15/10/2026", pos, [shop("EXAMPLE CLOUD*PLAN SUBSCR DUBLIN IRL"), "nr....1234"], "comerciant strain.", 9999)
    rows += _row("14/10/2026", "15/10/2026", pos, [shop("Example Market Bucharest ROM"), "nr....1234"], "comerciant BRD", 4500)
    rows += _row("20/10/2026", "21/10/2026", "Utilizare POS", [shop("Example Market Bucharest ROM"), "nr....1234"], "comerciant BRD", -1500)
    used = 120000 + 500 + 500 + 9999 + 4500 + used_extra
    repaid = 400000 + 1500 + repaid_extra
    head = f"""                                                                                               Cardul de Credit BRD
                                                                                                      Extras de cont

  Dl EXAMPLE PERSON                                            Card principal:                                1234******5678
                                                               Limita de credit:                                        10.000,00
                                                               Moneda:                                                           Lei
                                                               Perioada de referinta:                 26/09/2026-25/10/2026

Sumar tranzactii la 25/10/2026

Sold initial:                                         -10.100,13      Sold disponibil:                                         150,16
Sume utilizate pe parcursul perioadei:                 {_money(-used).rjust(10)}      Total de plata (fara rate scadente in viitor):         3.500,69
Sume rambursate pe parcursul perioadei:                 {_money(repaid).rjust(9)}      Suma minima de plata:                                     350,28
Soldul liniei de credit:                               -9.849,84      Data scadenta:                                      19/11/2026
Extrasold:                                                    0,00    Contul de plati:          {CARD_IBAN}

BRD-Groupe Societe Generale S.A.Turn BRD - Example
                                                                                               Cardul de Credit BRD
                                                                                                      Extras de cont

 Card Principal 1234******5678 Dl EXAMPLE PERSON

Tranzactii card de credit

                                                                                                                         Suma
Data             Data
                                  Tip tranzactie       Detalii tranzactie                                                Debit/Credit (Lei)
tranzactie       decontare
"""
    plans = """
Planuri de rate

                                                 Data         Valoare         Valoare                                        Valoare
   ID.   Detalii trz.                                                                      Dobanda %          Nr. Rata
                                              tranzactiei   initiala (Lei)   rata (Lei)                                    Ramasa (Lei)

         Example Shop
   8                                          03/10/2026       1.200,00         200,00             0.0%              1/6       1.000,00
         1.200,00 03/10/2026
         Example Flights
  11                                          11/06/2026       3.000,00         500,00             0.0%              5/6         500,00
         3.000,00 11/06/2026
Total rate curente: 700,00 Lei
Total rate scadente in viitor: 1.500,00 Lei
"""
    return head + "\n".join(rows) + "\n" + plans


def _entry(date: str, desc: str, cents: int, details: list[str], value: str | None = None) -> list[str]:
    amount = _money(abs(cents))
    end = 116 if cents > 0 else 92
    first = date.ljust(13) + desc
    return [first.ljust(end - len(amount)) + amount + "   " + (value or date)] + [" " * 14 + d for d in details]


def account_statement(iban: str, extra_debit: int = 0) -> str:
    """An invented BRD current account statement: payroll, a loan instalment, a Revolut top-up by card, with a page
    break in the middle of the payroll entry."""
    rows = []
    rows += _entry("02/10/2026", "Plata instant", -19000, ["Mobile Omnichannel", "OPH000000000001", "Jane Example",
                                                           "RO00TEST0000000000000555", "Example Bank N.V.", "Lunch"])
    rows += _entry("05/10/2026", "Utilizare POS comerciant alte BC", -4550, ["Card/Terminale", "OP 1234567/12345", "Card nr....1234",
                                                                             "Example Market Bucharest ROM"], "07/10/2026")
    rows += _entry("10/10/2026", "Transfer credit- Alim. conventie", 2000000, ["Automat", "OPH0001"])
    rows += ["", "BRD-Groupe Societe Generale S.A.Turn BRD - Example", "Mihalache nr. 1-7, Bucuresti",
             " " * 63 + "999/99.99.9999, inregistrata in Registrul Public al CNVM cu nr.", " " * 110 + "Pag. 2 /     3", "",
             " Data oper.                       Descriere operatiune                        Debit                 Credit"
             "                Data val.", " Trans.Date                     Transaction description", "",
             " " * 14 + "1234abcd-1234-1234-1234-123456789012", " " * 14 + "Dl EXAMPLE PERSON", " " * 14 + iban,
             " " * 14 + "Plata Salarii", ""]
    rows += _entry("12/10/2026", "Rambursare credit", -285000, ["Automat", "OP 1234567890123456", "Dl EXAMPLE PERSON", LOAN_IBAN,
                                                              "Rata-12/10/2026", "Credit 2650.00", "Dobanda 200.00"])
    rows += _entry("15/10/2026", "Plata asigurare", -7000, ["Automat", "OP 1234567890123457", "Dl EXAMPLE PERSON", LOAN_IBAN])
    rows += _entry("20/10/2026", "Utilizare POS comerciant strain.", -300000, ["Card/Terminale", "OP 1234567/12346", "Card nr....1234",
                                                                              "Revolut**1234* Dublin IRL"])
    debits = 19000 + 4550 + 285000 + 7000 + 300000 + extra_debit
    head = f"""
Domicilierea contului / Account Domiciliation                           Extras de cont / Statement
Suc. EXAMPLE                                                            De la / From 01/10/2026 La / To         31/10/2026

Referinte bancare / Banking references
Titular / Account holder       Dl EXAMPLE PERSON
IBAN          {iban}
Valuta / Currency           RON

 Data oper.                       Descriere operatiune                        Debit                 Credit                Data val.
 Trans.Date                     Transaction description

                                Sold initial / Start balance                                                 {_money(100000)}

"""
    tail = f"""
                                    Total debit / Total credit                                         {_money(debits)}               {_money(2000000)}
                                Sold final / End balance                                                                    {_money(100000 + 2000000 - debits)}
"""
    return head + "\n".join(rows) + "\n" + tail
