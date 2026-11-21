"""CSV statements from any bank: the owner says which column is which, once per account.

No bank-specific code: `sniff` looks at the file and guesses the delimiter, the date format and the columns;
`statement_from_csv` reads the rows with the mapping the owner confirmed. The result is an `AccountStatement`
like a PDF reader's, stored the same way (only what the account does not have yet is added).
"""

import csv
import io
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation

from .base import AccountLine, AccountStatement, StatementError

MAX_CSV_CHARS = 3_000_000
DELIMITERS = [",", ";", "\t", "|"]
DATE_FORMATS = ["%d/%m/%Y", "%d.%m.%Y", "%Y-%m-%d", "%d-%m-%Y", "%m/%d/%Y", "%d/%m/%y", "%d.%m.%y", "%Y/%m/%d"]
IBAN = re.compile(r"^[A-Z]{2}\d{2}[A-Z0-9]{11,30}$")
FIELDS = {"delimiter", "skip", "date", "value_date", "date_format", "description", "counterparty", "iban",
          "amount", "debit", "credit", "decimal", "invert"}
HEADER_WORDS = {
    "date": ("date", "data", "datum", "booking", "tranzactie"),
    "description": ("description", "descriere", "details", "detalii", "memo", "reference", "referinta", "narrative"),
    "counterparty": ("payee", "beneficiar", "counterparty", "name", "merchant", "partener", "platitor"),
    "iban": ("iban", "account"),
    "debit": ("debit", "out", "withdrawal", "plati", "retras"),
    "credit": ("credit", "in", "deposit", "incasari", "depus"),
    "amount": ("amount", "suma", "valoare", "value", "sum"),
}


def read_rows(text: str, delimiter: str) -> list[list[str]]:
    if len(text) > MAX_CSV_CHARS:
        raise StatementError("That file is too large.")
    return [row for row in csv.reader(io.StringIO(text.lstrip("﻿")), delimiter=delimiter) if any(c.strip() for c in row)]


def parse_number(text: str, decimal: str) -> int | None:
    """'1.234,56' (decimal ',') or '(12.50)' or '12,50-' -> minor units; None when it is not a number."""
    s = (text or "").strip().replace(" ", "").replace(" ", "")
    if not s:
        return None
    negative = s.startswith("(") and s.endswith(")") or s.endswith("-") or s.startswith("-")
    s = re.sub(r"[^\d.,]", "", s)
    if not s:
        return None
    s = s.replace(".", "").replace(",", ".") if decimal == "," else s.replace(",", "")
    try:
        cents = int((Decimal(s) * 100).to_integral_value())
    except InvalidOperation:
        return None
    return -cents if negative else cents


def parse_date(text: str, fmt: str) -> str | None:
    s = (text or "").strip()
    for candidate in (s, s.split(" ")[0], s.split("T")[0]):
        try:
            return datetime.strptime(candidate, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _split_guess(text: str) -> str:
    """The delimiter that splits the first lines into the same, largest number of fields."""
    lines = [ln for ln in text.lstrip("﻿").splitlines() if ln.strip()][:20]
    best, best_score = ",", 0
    for d in DELIMITERS:
        counts = [len(next(csv.reader([ln], delimiter=d))) for ln in lines]
        modal = max(set(counts), key=counts.count) if counts else 0
        score = counts.count(modal) * (modal > 1) * modal
        if score > best_score:
            best, best_score = d, score
    return best


def sniff(text: str, delimiter: str | None = None) -> dict:
    """What the file looks like, and a best guess at the mapping for the owner to confirm."""
    delimiter = delimiter if delimiter in DELIMITERS else _split_guess(text)
    rows = read_rows(text, delimiter)
    if len(rows) < 2:
        raise StatementError("That file has no rows to read.")
    width = max(len(r) for r in rows[:50])
    sample = rows[:60]
    col = lambda i, rs: [r[i] if i < len(r) else "" for r in rs]

    # The date column and format: the pair that parses the most rows.
    best = (0, 0, DATE_FORMATS[0])
    for i in range(width):
        for fmt in DATE_FORMATS:
            hits = sum(1 for v in col(i, sample) if parse_date(v, fmt))
            if hits > best[0]:
                best = (hits, i, fmt)
    hits, date_col, fmt = best
    skip = next((n for n, r in enumerate(sample) if date_col < len(r) and parse_date(r[date_col], fmt)), 0)
    data = [r for r in sample[skip:] if date_col < len(r) and parse_date(r[date_col], fmt)]
    headers = [h.strip().lower() for h in rows[skip - 1]] if skip else []

    # Numbers: a comma before two digits at the end means a decimal comma.
    values = [v for i in range(width) if i != date_col for v in col(i, data)]
    comma = sum(1 for v in values if re.search(r"\d,\d\d$", v.strip()))
    point = sum(1 for v in values if re.search(r"\d\.\d\d$", v.strip()))
    decimal = "," if comma > point else "."
    def is_numeric(i: int) -> bool:
        filled = [v for v in col(i, data) if (v or "").strip()]
        return bool(filled) and sum(1 for v in filled if parse_number(v, decimal) is not None) >= 0.9 * len(filled)

    numeric = [i for i in range(width) if i != date_col and data and is_numeric(i)]
    # Two numeric columns that are never both filled are debit and credit; otherwise one signed amount.
    guess: dict = {"delimiter": delimiter, "skip": skip, "date": date_col, "date_format": fmt, "decimal": decimal,
                   "value_date": None, "description": None, "counterparty": None, "iban": None,
                   "amount": None, "debit": None, "credit": None, "invert": False}
    split = [i for i in numeric if sum(1 for v in col(i, data) if (v or "").strip()) < 0.9 * len(data)]
    if len(split) >= 2 and not any(all((r[i] if i < len(r) else "").strip() for i in split[:2]) for r in data):
        guess["debit"], guess["credit"] = split[0], split[1]
        named = {k: next((i for i in split if any(w in headers[i] for w in HEADER_WORDS[k])), None) for k in ("debit", "credit")} \
            if headers else {}
        if named.get("debit") is not None and named.get("credit") is not None:
            guess["debit"], guess["credit"] = named["debit"], named["credit"]
    elif numeric:
        by_name = next((i for i in numeric if headers and any(w in headers[i] for w in HEADER_WORDS["amount"])), None)
        guess["amount"] = by_name if by_name is not None else numeric[0]

    # Text columns: an IBAN column, then the longest text for the description, a shorter one for the counterparty.
    used = {date_col, *(i for i in numeric)}
    iban_col = next((i for i in range(width) if i not in used
                     and sum(1 for v in col(i, data) if IBAN.match(v.replace(" ", ""))) >= 0.5 * max(len(data), 1)), None)
    guess["iban"] = iban_col
    text_cols = [i for i in range(width) if i not in used and i != iban_col]
    # A second date-like column (the value date).
    for i in text_cols:
        if sum(1 for v in col(i, data) if parse_date(v, fmt)) >= 0.7 * len(data) and guess["value_date"] is None:
            guess["value_date"] = i
    text_cols = [i for i in text_cols if i != guess["value_date"]]
    text_cols.sort(key=lambda i: -sum(len(v) for v in col(i, data)))
    # A column headed like a description or a payee is taken as one; otherwise the longest text describes.
    named = lambda kind, cols: next((i for i in cols if i < len(headers) and any(w in headers[i] for w in HEADER_WORDS[kind])), None)
    guess["description"] = named("description", text_cols)
    guess["counterparty"] = named("counterparty", [i for i in text_cols if i != guess["description"]])
    if guess["description"] is None:
        guess["description"] = next((i for i in text_cols if i != guess["counterparty"]), None)
    return {"delimiter": delimiter, "columns": width, "total_rows": len(rows), "rows": [r[:14] for r in rows[:8]], "guess": guess}


def clean_mapping(raw: dict) -> dict:
    """A mapping the owner (or the browser) sent, checked. Raises StatementError for what cannot be used."""
    m = {k: raw.get(k) for k in FIELDS}
    if m["delimiter"] not in DELIMITERS or m["decimal"] not in (".", ",") or not isinstance(m["skip"], int) or m["skip"] < 0:
        raise StatementError("The file format settings are not valid.")
    try:
        datetime.now().strftime(m["date_format"])
    except (TypeError, ValueError):
        raise StatementError("The date format is not valid.")
    for k in ("date", "value_date", "description", "counterparty", "iban", "amount", "debit", "credit"):
        if m[k] is not None and not (isinstance(m[k], int) and 0 <= m[k] < 100):
            raise StatementError("A column number is not valid.")
    if m["date"] is None:
        raise StatementError("Choose the date column.")
    if m["amount"] is None and (m["debit"] is None or m["credit"] is None):
        raise StatementError("Choose the amount column, or both the debit and the credit column.")
    m["invert"] = bool(m["invert"])
    return m


def statement_from_csv(text: str, mapping: dict, *, bank: str, alias: str, currency: str) -> AccountStatement:
    m = clean_mapping(mapping)
    rows = read_rows(text, m["delimiter"])[m["skip"]:]
    cell = lambda r, i: (r[i].strip() if i is not None and i < len(r) else "")
    lines, skipped = [], 0
    for r in rows:
        day = parse_date(cell(r, m["date"]), m["date_format"])
        if m["amount"] is not None:
            amount = parse_number(cell(r, m["amount"]), m["decimal"])
        else:
            debit, credit = parse_number(cell(r, m["debit"]), m["decimal"]), parse_number(cell(r, m["credit"]), m["decimal"])
            amount = (credit or 0) - abs(debit or 0) if (debit is not None or credit is not None) else None
        if not day or amount is None or amount == 0:
            skipped += 1
            continue
        if m["invert"]:
            amount = -amount
        iban = cell(r, m["iban"]).replace(" ", "").upper() or None
        lines.append(AccountLine(date=day, value_date=parse_date(cell(r, m["value_date"]), m["date_format"]) or day,
                                 amount_minor=amount, text=cell(r, m["description"])[:300],
                                 counterparty=cell(r, m["counterparty"])[:120] or None,
                                 iban=iban if iban and IBAN.match(iban) else None))
    if not lines or skipped > len(lines):
        raise StatementError("Most rows could not be read: check the date format, the delimiter and the columns.")
    days = sorted(ln.date for ln in lines)
    st = AccountStatement(bank=bank, alias=alias, currency=currency, period_start=days[0], period_end=days[-1], lines=lines)
    if skipped:
        st.warnings.append(f"{skipped} row{'s' if skipped != 1 else ''} could not be read and {'were' if skipped != 1 else 'was'} left out.")
    return st
