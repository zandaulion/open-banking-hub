"""What every statement reader shares: the result types, the registry of readers and a few helpers.

A reader turns the text of one kind of statement into an `AccountStatement` (a current account) or a
`Statement` (a credit card). See "Adding a statement reader" in CONTRIBUTING.md for the convention every
reader follows: detect cheaply, return signed minor-unit amounts and ISO dates, check the lines against the
statement's own totals, and never put statement content in an error message.
"""

import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Callable

MAX_PDF_BYTES = 5_000_000


class StatementError(ValueError):
    """A problem the owner can act on. Messages never include statement content."""


@dataclass
class Line:
    """One line of a credit card statement."""
    date: str                  # when the purchase was made (ISO)
    settled: str               # when it was settled (ISO)
    kind: str                  # purchase | payment | refund | fee
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
    """A credit card statement: the lines, what is owed and when it is due."""
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
    """One payment of a current account statement."""
    date: str                  # operation date (ISO)
    value_date: str            # (ISO)
    amount_minor: int          # signed: money out is negative
    text: str                  # the description; BRD writes "type|channel|reference|party|account|bank|details"
    counterparty: str | None = None
    iban: str | None = None    # the counterparty's account


@dataclass
class AccountStatement:
    """A current account statement. `iban` or `alias` says which linked account it belongs to."""
    bank: str
    currency: str
    period_start: str
    period_end: str
    iban: str | None = None
    alias: str | None = None
    start_minor: int | None = None
    end_minor: int | None = None
    lines: list[AccountLine] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Reader:
    id: str                              # "brd-account"
    bank: str
    kind: str                            # "account" | "card"
    label: str                           # "BRD current account", shown to the owner
    detect: Callable[[str], bool]        # cheap: does this text look like this statement?
    parse: Callable[[str], "Statement | AccountStatement"]


_READERS: dict[str, Reader] = {}


def register(reader: Reader) -> Reader:
    _READERS[reader.id] = reader
    return reader


def readers() -> list[Reader]:
    return list(_READERS.values())


def parse_statement(text: str) -> "Statement | AccountStatement":
    """Reads whichever supported statement this is."""
    found = [r for r in _READERS.values() if r.detect(text)]
    if len(found) == 1:
        return found[0].parse(text)
    if found:
        raise StatementError("This statement matches more than one reader: " + ", ".join(r.label for r in found) + ".")
    raise StatementError("This does not look like a statement Bank Hub can read (" +
                         ", ".join(r.label for r in _READERS.values()) + "). For another bank, import a CSV.")


def warn_unless(statement: "Statement | AccountStatement", ok: bool, message: str) -> None:
    """The reconciliation every reader does: the lines read must add up to the statement's own totals."""
    if not ok:
        statement.warnings.append(message)


def money(text: str) -> int:
    """'-1.234,56' -> -123456 (Romanian style); readers for other formats parse their own numbers."""
    return int(text.replace(".", "").replace(",", ""))


def iso(text: str, fmt: str = "%d/%m/%Y") -> str:
    return datetime.strptime(text, fmt).date().isoformat()


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
