"""An invented BRD credit card statement, laid out like the real one (as pdftotext -layout writes it)."""

CARD_IBAN = "RO00TEST0000000000000777"


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
