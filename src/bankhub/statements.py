"""Statements (PDF) from the bank: credit cards the bank does not share over open banking, and current
accounts whose history the bank's API only hands out slowly.

A card statement is read into lines, a summary (owed, limit, due date) and instalment plans, and stored on an
account whose `source` is 'statement'. A current account statement adds the payments the sync has not
fetched yet to the linked account, and the sync later replaces them with the bank's own records. Only what is
parsed is kept: the PDF itself holds the holder's name and address and is never stored.
"""

import hashlib
import json
import re
import shutil
import sqlite3
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from . import sync
from .db import transaction, utcnow

MAX_PDF_BYTES = 5_000_000


class StatementError(ValueError):
    """A problem the owner can act on. Messages never include statement content."""


@dataclass
class Line:
    date: str                  # when the purchase was made
    settled: str
    kind: str                  # purchase | payment | refund | fee | reversal | plan
    amount_minor: int          # as on the statement: positive = a charge, negative = money paid in
    merchant: str | None = None
    note: str | None = None
    iban: str | None = None    # who paid the card, on a repayment
    text: str = ""


@dataclass
class Plan:
    merchant: str
    started: str
    initial_minor: int
    installment_minor: int
    number: str
    remaining_minor: int


@dataclass
class Statement:
    bank: str
    iban: str                  # the card's payment account
    period_start: str
    period_end: str
    owed_minor: int | None = None
    limit_minor: int | None = None
    available_minor: int | None = None
    total_due_minor: int | None = None
    min_due_minor: int | None = None
    due_date: str | None = None
    installments_due_minor: int | None = None
    installments_future_minor: int | None = None
    lines: list[Line] = field(default_factory=list)
    plans: list[Plan] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class AccountLine:
    date: str                  # operation date
    value_date: str
    amount_minor: int          # signed: money out is negative
    text: str                  # "type|channel|reference|party|account|bank|details", as the bank's API writes it


@dataclass
class AccountStatement:
    bank: str
    iban: str
    currency: str
    period_start: str
    period_end: str
    start_minor: int | None = None
    end_minor: int | None = None
    lines: list[AccountLine] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def pdf_text(data: bytes) -> str:
    """The text of a PDF with its columns kept (poppler's pdftotext)."""
    if not data.startswith(b"%PDF-"):
        raise StatementError("That file is not a PDF.")
    if len(data) > MAX_PDF_BYTES:
        raise StatementError("That PDF is larger than 5 MB.")
    exe = shutil.which("pdftotext")
    if not exe:
        raise StatementError("Reading PDFs needs pdftotext (poppler-utils) on the server.")
    with tempfile.TemporaryDirectory() as tmp:       # private to this process
        path = Path(tmp) / "statement.pdf"
        path.write_bytes(data)
        try:
            out = subprocess.run([exe, "-layout", "-q", str(path), "-"], capture_output=True, timeout=30, check=True)
        except (subprocess.SubprocessError, OSError):
            raise StatementError("The PDF could not be read (is it password protected?).")
    return out.stdout.decode("utf-8", "replace")


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


def _money(text: str) -> int:
    return int(text.replace(".", "").replace(",", ""))


def _iso(text: str) -> str:
    return datetime.strptime(text, "%d/%m/%Y").date().isoformat()


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
    amount = lambda label, sign=1: (lambda v: None if v is None else sign * _money(v))(_find(text, label, AMOUNT))
    owed = amount("Soldul liniei de credit")
    due = _find(text, "Data scadenta", DATE)
    st = Statement(
        bank="BRD", iban=iban.group(1).replace(" ", ""), period_start=_iso(period.group(1)), period_end=_iso(period.group(2)),
        owed_minor=None if owed is None else -owed, limit_minor=amount("Limita de credit"),
        available_minor=amount("Sold disponibil"), total_due_minor=amount("Total de plata (fara rate scadente in viitor)"),
        min_due_minor=amount("Suma minima de plata"), due_date=_iso(due) if due else None,
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
            entries.append({"date": m.group(1), "settled": m.group(2), "amount": _money(m.group(4)),
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
        st.lines.append(Line(date=_iso(e["date"]), settled=_iso(e["settled"]), kind=kind, amount_minor=e["amount"],
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
            st.plans.append(Plan(merchant=name[:60], started=_iso(m.group(2)), initial_minor=_money(m.group(3)),
                                 installment_minor=_money(m.group(4)), number=m.group(5), remaining_minor=_money(m.group(6))))

    # The lines must add up to what the statement says it used and repaid.
    used, repaid = amount("Sume utilizate pe parcursul perioadei"), amount("Sume rambursate pe parcursul perioadei")
    got_used = sum(ln.amount_minor for ln in st.lines if ln.amount_minor > 0)
    got_repaid = -sum(ln.amount_minor for ln in st.lines if ln.amount_minor < 0)
    if used is not None and abs(used) != got_used:
        st.warnings.append("The purchases read from this statement do not add up to its total of amounts used.")
    if repaid is not None and abs(repaid) != got_repaid:
        st.warnings.append("The repayments read from this statement do not add up to its total of amounts repaid.")
    if not st.lines and not used:
        st.warnings.append("No transactions were found in this statement.")
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
                          period_start=_iso(period.group(1)), period_end=_iso(period.group(2)))
    opening = re.search(rf"Sold initial / Start balance\s+({AMOUNT})", text)
    closing = re.search(rf"Sold final / End balance\s+({AMOUNT})", text)
    totals = re.search(rf"Total debit / Total credit\s+({AMOUNT})\s+({AMOUNT})", text)
    st.start_minor = _money(opening.group(1)) if opening else None
    st.end_minor = _money(closing.group(1)) if closing else None

    entries: list[dict] = []
    for r in rows:
        if "Total debit / Total credit" in r:
            break
        m = ACCOUNT_ENTRY.match(r)
        if m:
            cents = _money(m.group(3))
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
        st.lines.append(AccountLine(date=_iso(e["date"]), value_date=_iso(e["value"]), amount_minor=e["amount"],
                                    text="|".join([e["desc"], *details])))

    credits = sum(ln.amount_minor for ln in st.lines if ln.amount_minor > 0)
    debits = -sum(ln.amount_minor for ln in st.lines if ln.amount_minor < 0)
    if totals and (_money(totals.group(1)), _money(totals.group(2))) != (debits, credits):
        st.warnings.append("The payments read from this statement do not add up to its totals of debits and credits.")
    elif st.start_minor is not None and st.end_minor is not None and st.start_minor + credits - debits != st.end_minor:
        st.warnings.append("The payments read from this statement do not take its opening balance to its closing balance.")
    if not st.lines:
        st.warnings.append("No transactions were found in this statement.")
    return st


def parse_statement(text: str) -> Statement | AccountStatement:
    """Reads whichever kind of BRD statement this is."""
    if "Cardul de Credit BRD" in text:
        return parse_brd_card(text)
    if "Descriere operatiune" in text and "Extras de cont" in text:
        return parse_brd_account(text)
    raise StatementError("This does not look like a BRD statement (a current account or a credit card).")


# ---------------------------------------------------------------- storing

def _card_account(conn: sqlite3.Connection, st: Statement) -> int:
    ident = hashlib.sha256(f"{st.bank}:{st.iban}".encode()).hexdigest()[:32]
    row = conn.execute("SELECT id FROM accounts WHERE identity_hash=?", (ident,)).fetchone()
    if row:
        return row["id"]
    session = f"statement:{ident}"
    conn.execute("INSERT INTO consents(session_id, institution, country, valid_until, created_at, status)"
                 " VALUES (?,?,?,?,?,'statement')", (session, st.bank, "RO", "9999-12-31T00:00:00Z", utcnow()))
    taken = {r[0] for r in conn.execute("SELECT alias FROM accounts")}
    alias, n = f"{st.bank.lower()}_card", 2
    while alias in taken:
        alias, n = f"{st.bank.lower()}_card_{n}", n + 1
    return conn.execute(
        "INSERT INTO accounts(uid, session_id, institution, alias, currency, cash_account_type, identity_hash, own_ibans,"
        " backfilled_at, display_name, source) VALUES (?,?,?,?,?,?,?,?,?,?,'statement')",
        (session, session, st.bank, alias, "RON", "CARD", ident, json.dumps([st.iban]), utcnow(),
         f"{st.bank} credit card")).lastrowid


def _eb(ln: Line, ref: str) -> dict:
    """A statement line in the shape the bank API uses, so it is stored and read like any other payment."""
    debit = ln.amount_minor > 0
    cents = abs(ln.amount_minor)
    nobody = {"name": None}
    none = {"iban": None, "other": None}
    return {
        "entry_reference": ref,
        "transaction_amount": {"amount": f"{cents // 100}.{cents % 100:02d}", "currency": "RON"},
        "credit_debit_indicator": "DBIT" if debit else "CRDT", "status": "BOOK",
        "booking_date": ln.date, "value_date": ln.settled,
        "creditor": {"name": ln.merchant} if debit else nobody, "creditor_account": none,
        "debtor": nobody if debit else {"name": ln.merchant},
        "debtor_account": {"iban": ln.iban, "other": None} if ln.iban else none,
        "remittance_information": [ln.text or ln.kind], "bank_transaction_code": {},
    }


def store_statement(conn: sqlite3.Connection, st: Statement) -> dict:
    """Adds a statement. Adding the same one again changes nothing."""
    with transaction(conn):
        acc = _card_account(conn, st)
        seen: dict[tuple, int] = {}
        txs = []
        for ln in st.lines:
            # Identical lines on one day (two 5 RON rides) are told apart by their order in the statement.
            basis = (ln.date, ln.amount_minor, ln.merchant, ln.text)
            seen[basis] = seen.get(basis, 0) + 1
            digest = hashlib.sha256(json.dumps([st.iban, *basis, seen[basis]]).encode()).hexdigest()[:24]
            txs.append(_eb(ln, f"stmt-{digest}"))
        new = sync._store_transactions(conn, acc, txs, prune_pending=False)
        old = conn.execute("SELECT id FROM card_statements WHERE account_id=? AND period_end=?",
                           (acc, st.period_end)).fetchone()
        if old:
            conn.execute("DELETE FROM card_plans WHERE statement_id=?", (old["id"],))
            conn.execute("DELETE FROM card_statements WHERE id=?", (old["id"],))
        sid = conn.execute(
            """INSERT INTO card_statements(account_id, period_start, period_end, owed_minor, limit_minor, available_minor,
                   total_due_minor, min_due_minor, due_date, installments_due_minor, installments_future_minor, imported_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (acc, st.period_start, st.period_end, st.owed_minor, st.limit_minor, st.available_minor, st.total_due_minor,
             st.min_due_minor, st.due_date, st.installments_due_minor, st.installments_future_minor, utcnow())).lastrowid
        conn.executemany(
            "INSERT INTO card_plans(statement_id, merchant, started, initial_minor, installment_minor, number, remaining_minor)"
            " VALUES (?,?,?,?,?,?,?)",
            [(sid, p.merchant, p.started, p.initial_minor, p.installment_minor, p.number, p.remaining_minor) for p in st.plans])
        if st.owed_minor is not None:
            # What is owed is a negative balance; the latest statement is the one shown.
            stamp = f"{st.period_end}T23:59:59+00:00"
            conn.execute("DELETE FROM balance_snapshots WHERE account_id=? AND fetched_at=? AND balance_type='STMT'", (acc, stamp))
            conn.execute("INSERT INTO balance_snapshots(account_id, fetched_at, balance_type, amount_minor, currency)"
                         " VALUES (?,?,?,?,?)", (acc, stamp, "STMT", -st.owed_minor, "RON"))
    return {"period_start": st.period_start, "period_end": st.period_end, "lines": len(st.lines), "new": new,
            "already_had": len(st.lines) - new, "warnings": st.warnings}


def store_account_statement(conn: sqlite3.Connection, st: AccountStatement) -> dict:
    """Adds the payments of a current account statement that the account does not have yet. A payment the sync
    already fetched (same amount, within two days) is left alone; the sync later replaces what is added here."""
    acc = next((r for r in conn.execute("SELECT id, own_ibans, coalesce(display_name, alias) AS name FROM accounts"
                                        " WHERE source = 'bank' AND active = 1")
                if st.iban in json.loads(r["own_ibans"] or "[]")), None)
    if not acc:
        raise StatementError("This statement is for an account that is not linked. Link the bank first.")
    with transaction(conn):
        lo = (date.fromisoformat(st.period_start) - timedelta(days=2)).isoformat()
        hi = (date.fromisoformat(st.period_end) + timedelta(days=2)).isoformat()
        pool = [dict(r) for r in conn.execute(
            "SELECT id, booking_date, amount_minor FROM transactions_raw WHERE account_id=? AND booking_date BETWEEN ? AND ?",
            (acc["id"], lo, hi))]
        taken: set[int] = set()

        def match(line: AccountLine, slack: int) -> bool:
            day = date.fromisoformat(line.date)
            for r in pool:
                if r["id"] not in taken and r["amount_minor"] == line.amount_minor \
                        and abs((date.fromisoformat(r["booking_date"]) - day).days) <= slack:
                    taken.add(r["id"])
                    return True
            return False

        # Same day first, so that two equal payments on neighbouring days pair up correctly.
        rest = [ln for ln in st.lines if not match(ln, 0)]
        missing = [ln for ln in rest if not match(ln, 2)]
        seen: dict[tuple, int] = {}
        txs = []
        for ln in missing:
            basis = (ln.date, ln.amount_minor, ln.text)
            seen[basis] = seen.get(basis, 0) + 1
            digest = hashlib.sha256(json.dumps([st.iban, *basis, seen[basis]]).encode()).hexdigest()[:24]
            cents = abs(ln.amount_minor)
            txs.append({"entry_reference": f"stmt-{digest}", "status": "BOOK", "booking_date": ln.date,
                        "value_date": ln.value_date, "credit_debit_indicator": "DBIT" if ln.amount_minor < 0 else "CRDT",
                        "transaction_amount": {"amount": f"{cents // 100}.{cents % 100:02d}", "currency": st.currency},
                        "remittance_information": [ln.text], "bank_transaction_code": {}})
        new = sync._store_transactions(conn, acc["id"], txs, prune_pending=False)
    return {"kind": "account", "account": acc["name"], "period_start": st.period_start, "period_end": st.period_end,
            "lines": len(st.lines), "new": new, "already_had": len(st.lines) - new, "warnings": st.warnings}


def store(conn: sqlite3.Connection, st: Statement | AccountStatement) -> dict:
    if isinstance(st, AccountStatement):
        return store_account_statement(conn, st)
    result = store_statement(conn, st)
    name = conn.execute("SELECT coalesce(display_name, alias) FROM accounts WHERE source='statement'"
                        " AND identity_hash=?", (hashlib.sha256(f"{st.bank}:{st.iban}".encode()).hexdigest()[:32],)).fetchone()
    return {"kind": "card", "account": name[0] if name else None, **result}
