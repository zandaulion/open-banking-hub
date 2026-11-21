# Contributing

Thanks for helping. Bank Hub is small on purpose: one person's banks, on their own machine, read-only.

## Getting started

```bash
uv sync
uv run pytest
BANKHUB_DATA_DIR=/tmp/bankhub-demo uv run bankhub demo      # a year of made-up data
BANKHUB_DATA_DIR=/tmp/bankhub-demo uv run bankhub invite --label dev
BANKHUB_DATA_DIR=/tmp/bankhub-demo uv run bankhub web       # open the printed link
```

## Rules of the road

- **Never use real banking data** in tests, fixtures, screenshots, issues or pull requests. Use
  `bankhub demo` or the synthetic helpers in `tests/conftest.py`. CI rejects real-looking IBANs,
  private hostnames, email addresses and keys.
- **Read-only, always.** No payment initiation, ever.
- **Respect the privacy boundary.** Anything an AI assistant can reach goes through `transactions_clean`
  and `bankhub/queries.py`. A new column there needs a good reason and a test.
- **Bank-specific code stays small and documented.** A new bank reader in `normalize.py` gets a test with
  a synthetic payload of the bank's shape, and a section in `docs/BANKS.md`.
- **Schema changes are migrations**, plus a test that upgrades a database in the old shape.
- **The PWA has no build step and no CDN resources.** It builds text with `textContent`, because bank
  data is untrusted.

## Categories

Additions to `src/bankhub/data/categories.yaml` are welcome: chains and online services in your
country, and bank transaction types. Keep personal places (your bakery, your landlord) in your own
`<data dir>/categories.yaml`.

## Adding a statement reader

Most banks need no reader: **Accounts → Add a statement → Import a CSV** works for any bank by choosing
which column is which, with no code. Write a reader when a bank's statements are only available as PDF, or
for something its API leaves out, such as a credit card (BRD is the example: `src/bankhub/statements/brd.py`).

A reader is a module in `src/bankhub/statements/` that registers itself and is imported in `__init__.py`:

```python
register(Reader(id="mybank-account", bank="MyBank", kind="account",        # or "card"
                label="MyBank current account",                          # shown to the owner
                detect=lambda text: "MyBank statement" in text,           # cheap, text only
                parse=parse_mybank_account))                             # -> AccountStatement (or Statement for a card)
```

`text` is the PDF's text from `pdftotext -layout`, so columns keep their positions. The convention every
reader follows:

1. **Detect narrowly.** `detect` looks at the text only, and no other reader may claim the same statement.
2. **Return the common types.** ISO dates, integer minor units, and the sign convention: on an account
   statement money out is negative; on a card statement a charge is positive.
3. **Check the lines against the statement.** Add up what you read and compare it with the statement's own
   totals (and its opening and closing balance, when it has them) using `warn_unless`. A mismatch is a
   *warning* the owner sees, never an exception and never silently ignored: a misread line is the worst
   failure, because the numbers still look plausible.
4. **Say what is wrong, not what is in the file.** Raise `StatementError` with a message that names the
   missing piece and quotes nothing from the statement (no names, amounts, IBANs or account numbers).
5. **Fit what the API gives.** For a bank whose API text `normalize.py` already reads (BRD), write the lines in
   that same shape, so a statement line and the API's record of the same payment are read identically.
   Otherwise fill `AccountLine.counterparty` and `.iban`.
6. **Keep only what is needed.** The lines and the summary figures; never the PDF, the holder's name or the address.

Test it with an **invented** statement in the bank's real layout (see `tests/statement_fixture.py`) and
one call to the shared checks:

```python
from reader_conformance import check_reader
check_reader("mybank-account", sample_text, text_with_one_amount_changed)
```

That verifies rules 1 to 4. Add tests for what is particular to the bank (a page break in the middle of a
payment, columns that shift, a payroll line that names the holder). Describe the reader in `docs/BANKS.md`.

## Pull requests

Keep them focused, describe what you verified (tests, a demo run, a browser check), and update the
affected documentation.
