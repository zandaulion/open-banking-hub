"""BRD (Romania): the PDF statements of a credit card and of a current account (MyBRD, "Extras de cont").

Both are read from `pdftotext -layout` text, where the columns keep their positions. The BRD API's own
description of a payment is `type|channel|reference|party|account or card|bank or merchant|details`, and the
account reader writes the statement's lines in that shape, so a statement line and the API's record of the
same payment are read identically by `normalize._brd`.
"""

import re

from .base import (AccountLine, AccountStatement, Line, Plan, Reader, Statement, StatementError, iso, money,
                   register, warn_unless)

# ---------------------------------------------------------------- BRD credit card

AMOUNT = r"-?\d{1,3}(?:\.\d{3})*,\d\d"
DATE = r"\d\d/\d\d/\d{4}"
IBAN = re.compile(r"RO\d{2}[A-Z]{4}[0-9A-Z]{16}")
ENTRY = re.compile(rf"^\s*({DATE})\s+({DATE})\s+(\S.*?)\s+({AMOUNT})\s*$")
ENTRY_START = re.compile(rf"^\s*{DATE}\s+{DATE}\s")
PLAN_ROW = re.compile(rf"^\s*(\d+)\s+({DATE})\s+({AMOUNT})\s+({AMOUNT})\s+[\d.,]+%\s+(\d+/\d+)\s+({AMOUNT})\s*$")
NOT_CONTINUATION = ("BRD-Groupe", "CAPITAL SOCIAL", "brd.ro", "Cardul de Credit", "Extras de cont", "Card Principal",
                    "Tranzactii card", "Tip tranzactie", "Detalii tranzactie", "tranzactie", "Debit/Credit", "Suma",
                    "Data", "Planuri de rate", "Informatii utile")


def _find(text: str, label: str, pattern: str) -> str | None:
    m = re.search(re.escape(label) + r"\s*:?\s+(" + pattern + ")", text)
    return m.group(1) if m else None


def _merchant(details: str) -> tuple[str | None, str | None]:
    """'Card/Terminale/OP123/456/SHOP NAME CITY ROM/Card nr....1234' -> ('SHOP NAME CITY ROM', None)."""
    m = re.match(r"^Card/Terminale/[A-Z]{2}\d+(?:/\d+)?/(?P<rest>.*)$", details.strip())
    if not m:
        return None, None
    rest = re.sub(r"\s*/\s*Card nr\.*\s*\d*\s*$", "", m.group("rest"))
    note = None
    if " / " in rest:
        rest, note = (part.strip() for part in rest.split(" / ", 1))
    return rest.strip(" /") or None, note or None


def parse_brd_card(text: str) -> Statement:
    if "Cardul de Credit BRD" not in text:
        raise StatementError("This does not look like a BRD credit card statement.")
    rows = text.splitlines()
    period = re.search(rf"Perioada de referinta:\s+({DATE})\s*-\s*({DATE})", text)
    iban = re.search(r"Contul de plati:\s+(RO[0-9A-Z ]+?)\s*(?:\n|$)", text)
    if not period or not iban:
        raise StatementError("The statement's period or payment account was not found.")
    amount = lambda label, sign=1: (lambda v: None if v is None else sign * money(v))(_find(text, label, AMOUNT))
    owed = amount("Soldul liniei de credit")
    due = _find(text, "Data scadenta", DATE)
    st = Statement(
        bank="BRD", iban=iban.group(1).replace(" ", ""), period_start=iso(period.group(1)), period_end=iso(period.group(2)),
        owed_minor=None if owed is None else -owed, limit_minor=amount("Limita de credit"),
        available_minor=amount("Sold disponibil"), total_due_minor=amount("Total de plata (fara rate scadente in viitor)"),
        min_due_minor=amount("Suma minima de plata"), due_date=iso(due) if due else None,
        installments_due_minor=amount("Total rate curente"), installments_future_minor=amount("Total rate scadente in viitor"))

    # Column where the type text starts, from the table header; details start 18 characters later.
    header = next((r for r in rows if "Tip tranzactie" in r), None)
    type_col = header.index("Tip tranzactie") if header else 34
    split = type_col + 18
    start = next((i for i, r in enumerate(rows) if "Tranzactii card de credit" in r), None)
    if start is None:
        raise StatementError("The statement has no transactions table.")
    end = next((i for i, r in enumerate(rows) if i > start and ("Planuri de rate" in r or "Informatii utile" in r)), len(rows))

    entries: list[dict] = []
    for r in rows[start:end]:
        m = ENTRY.match(r)
        if m:
            body = r[: m.start(4)].rstrip()
            entries.append({"date": m.group(1), "settled": m.group(2), "amount": money(m.group(4)),
                            "type": [body[type_col:split].strip()], "details": [body[split:].strip()]})
        elif entries and not ENTRY_START.match(r) and r.strip() and 30 <= len(r) - len(r.lstrip()) < 60 \
                and not any(r.strip().startswith(p) for p in NOT_CONTINUATION):
            entries[-1]["type"].append(r[type_col:split].strip())
            entries[-1]["details"].append(r[split:].strip())

    for e in entries:
        kind_text = " ".join(p for p in e["type"] if p)
        details = " ".join(p for p in e["details"] if p)
        squeezed = "".join(e["details"]).replace(" ", "")      # an IBAN can wrap across lines
        if e["amount"] == 0 or kind_text.startswith(("Sume in procesare", "Sume restante")):
            continue
        merchant, note = _merchant(details)
        if "stornare plata in rate" in details:
            kind = "reversal"
        elif kind_text.startswith("Rate fara dobanda"):
            kind = "plan"
        elif e["amount"] < 0:
            kind = "payment" if "Incasare" in kind_text else "refund"
        else:
            kind = "purchase" if merchant else "fee"
        found = IBAN.search(squeezed) if e["amount"] < 0 else None
        st.lines.append(Line(date=iso(e["date"]), settled=iso(e["settled"]), kind=kind, amount_minor=e["amount"],
                             merchant=merchant, note=note, iban=found.group(0) if found else None,
                             text=kind_text if not merchant else merchant))

    # A purchase turned into instalments shows as the purchase, a reversal and the plan: the reversal and
    # the plan cancel, and the purchase stays where it was made.
    plans = [ln for ln in st.lines if ln.kind == "plan"]
    drop: set[int] = set()
    for rev in (ln for ln in st.lines if ln.kind == "reversal"):
        twin = next((p for p in plans if id(p) not in drop and p.amount_minor == -rev.amount_minor), None)
        if twin:
            drop |= {id(rev), id(twin)}
    st.lines = [ln for ln in st.lines if id(ln) not in drop]
    for ln in st.lines:
        if ln.kind == "reversal":
            ln.kind = "refund"
        elif ln.kind == "plan":
            ln.kind = "purchase"
            ln.note = ln.note or "instalments"

    # Instalment plans, listed below the transactions.
    for i, r in enumerate(rows):
        m = PLAN_ROW.match(r)
        if m:
            name = next((p.strip() for p in reversed(rows[:i]) if p.strip()), "")
            st.plans.append(Plan(merchant=name[:60], started=iso(m.group(2)), initial_minor=money(m.group(3)),
                                 installment_minor=money(m.group(4)), number=m.group(5), remaining_minor=money(m.group(6))))

    # The lines must add up to what the statement says it used and repaid.
    used, repaid = amount("Sume utilizate pe parcursul perioadei"), amount("Sume rambursate pe parcursul perioadei")
    got_used = sum(ln.amount_minor for ln in st.lines if ln.amount_minor > 0)
    got_repaid = -sum(ln.amount_minor for ln in st.lines if ln.amount_minor < 0)
    warn_unless(st, used is None or abs(used) == got_used,
                "The purchases read from this statement do not add up to its total of amounts used.")
    warn_unless(st, repaid is None or abs(repaid) == got_repaid,
                "The repayments read from this statement do not add up to its total of amounts repaid.")
    warn_unless(st, bool(st.lines) or bool(used), "No transactions were found in this statement.")
    return st


# ---------------------------------------------------------------- BRD current account

UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
# Debit amounts end at or before the "Credit" header's column (BRD prints some up to two columns past it), credit
# amounts about fifteen columns further right: anything this far past the header is a credit.
CREDIT_SLACK = 8
ACCOUNT_ENTRY = re.compile(rf"^\s{{0,3}}({DATE})\s{{2,}}(\S.*?)\s+({AMOUNT})\s+({DATE})\s*$")
ACCOUNT_ENTRY_START = re.compile(rf"^\s{{0,3}}{DATE}\s{{2,}}\S")


def parse_brd_account(text: str) -> AccountStatement:
    rows = text.splitlines()
    iban = re.search(r"IBAN\s+(RO\d{2}[A-Z]{4}[0-9A-Z]{16})", text)
    period = re.search(rf"De la / From\s+({DATE})\s+La / To\s+({DATE})", text)
    currency = re.search(r"Valuta / Currency\s+([A-Z]{3})", text)
    header = next((r for r in rows if "Data oper." in r and "Credit" in r), None)
    if not (iban and period and currency and header):
        raise StatementError("The statement's account, period or columns were not found.")
    credit_col = header.index("Credit") + CREDIT_SLACK
    st = AccountStatement(bank="BRD", iban=iban.group(1), currency=currency.group(1),
                          period_start=iso(period.group(1)), period_end=iso(period.group(2)))
    opening = re.search(rf"Sold initial / Start balance\s+({AMOUNT})", text)
    closing = re.search(rf"Sold final / End balance\s+({AMOUNT})", text)
    totals = re.search(rf"Total debit / Total credit\s+({AMOUNT})\s+({AMOUNT})", text)
    st.start_minor = money(opening.group(1)) if opening else None
    st.end_minor = money(closing.group(1)) if closing else None

    entries: list[dict] = []
    for r in rows:
        if "Total debit / Total credit" in r:
            break
        m = ACCOUNT_ENTRY.match(r)
        if m:
            cents = money(m.group(3))
            entries.append({"date": m.group(1), "desc": m.group(2).strip(), "value": m.group(4),
                            "amount": cents if m.end(3) >= credit_col else -cents, "details": []})
        elif entries and not ACCOUNT_ENTRY_START.match(r) and r.strip() and 10 <= len(r) - len(r.lstrip()) <= 16:
            detail = r.strip()
            if not UUID.match(detail):
                entries[-1]["details"].append(detail)

    for e in entries:
        details = e["details"]
        if e["amount"] > 0 and "alim. conventie" in e["desc"].lower():
            # Payroll credits name the account holder where the payer would be: keep the name, drop the IBAN,
            # as the bank's API does, so the credit is not mistaken for a move between the holder's accounts.
            details = ["" if IBAN.fullmatch(d.replace(" ", "")) else d for d in details]
        st.lines.append(AccountLine(date=iso(e["date"]), value_date=iso(e["value"]), amount_minor=e["amount"],
                                    text="|".join([e["desc"], *details])))

    credits = sum(ln.amount_minor for ln in st.lines if ln.amount_minor > 0)
    debits = -sum(ln.amount_minor for ln in st.lines if ln.amount_minor < 0)
    totals_ok = not totals or (money(totals.group(1)), money(totals.group(2))) == (debits, credits)
    warn_unless(st, totals_ok, "The payments read from this statement do not add up to its totals of debits and credits.")
    balances_ok = st.start_minor is None or st.end_minor is None or st.start_minor + credits - debits == st.end_minor
    warn_unless(st, not totals_ok or balances_ok,
                "The payments read from this statement do not take its opening balance to its closing balance.")
    warn_unless(st, bool(st.lines), "No transactions were found in this statement.")
    return st


register(Reader(id="brd-card", bank="BRD", kind="card", label="BRD credit card",
                detect=lambda text: "Cardul de Credit BRD" in text, parse=parse_brd_card))
register(Reader(id="brd-account", bank="BRD", kind="account", label="BRD current account",
                detect=lambda text: "Descriere operatiune" in text and "Extras de cont" in text
                and "Cardul de Credit BRD" not in text, parse=parse_brd_account))
