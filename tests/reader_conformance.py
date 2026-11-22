"""The convention every statement reader must follow (see "Adding a statement reader" in CONTRIBUTING.md).

    check_reader("my-bank-account", sample_text, broken_text)

`sample_text` is an invented statement in the bank's real layout; `broken_text` is the same statement with one
amount changed so it no longer adds up to its own totals.
"""

import re

from bankhub import statements
from bankhub.statements import AccountStatement, Statement, StatementError

ISO = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def check_reader(reader_id: str, sample: str, broken: str | None) -> None:
    """`broken=None` only for a format that carries no totals at all (OFX has a closing balance, no opening)."""
    reader = next(r for r in statements.readers() if r.id == reader_id)
    # 1. It recognises its own statements, and only those.
    assert reader.detect(sample)
    assert [r.id for r in statements.readers() if r.detect(sample)] == [reader_id], "another reader also claims it"
    assert not reader.detect("Some other document\nwith lines of text") and not reader.detect("")
    # 2. It returns the common types, with ISO dates and integer minor units, and is deterministic.
    st = statements.parse_statement(sample)
    assert isinstance(st, AccountStatement if reader.kind == "account" else Statement)
    assert st.lines and statements.parse_statement(sample) == st
    assert re.match(ISO, st.period_start) and re.match(ISO, st.period_end)
    for ln in st.lines:
        assert isinstance(ln.amount_minor, int) and ln.amount_minor != 0 and re.match(ISO, ln.date)
    # 3. It checks the lines against the statement's own totals: clean when they add up, a warning when not.
    assert st.warnings == [], st.warnings
    if broken is not None:
        assert statements.parse_statement(broken).warnings, "a statement that does not add up must warn"
    # 4. Its errors say what is wrong without repeating the statement.
    head = "\n".join(sample.splitlines()[:6])
    try:
        reader.parse(head)
    except StatementError as e:
        words = str(e)
        assert not re.search(r"\d{4}", words) and not any(len(ln.strip()) > 20 and ln.strip() in words for ln in sample.splitlines())
