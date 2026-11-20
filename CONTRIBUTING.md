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

## Pull requests

Keep them focused, describe what you verified (tests, a demo run, a browser check), and update the
affected documentation.
