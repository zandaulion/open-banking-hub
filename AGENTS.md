# Notes for AI agents

Bank Hub is a self-hosted, read-only view of one person's bank accounts: Python 3.11+ (`src/bankhub/`),
SQLite, the Enable Banking PSD2 API, a FastAPI + static PWA for the owner (`src/bankhub/web/`) and a
read-only MCP server over a redacted ledger.

## Deploying, upgrading or rolling back

Follow [.claude/skills/deploy/SKILL.md](.claude/skills/deploy/SKILL.md) step by step. It covers gathering
facts, Enable Banking registration, systemd and Docker installs, publishing on a private network,
the first device invitation, bank linking, backups and upgrades. Do not improvise a deployment from the
README alone, and never expose the app to the public internet.

## Changing code

- `uv sync && uv run pytest` before and after a change; CI runs the same, plus `node --check` on the PWA.
- All test data is synthetic. `tests/test_publish_hygiene.py` fails on real-looking IBANs, private
  hostnames, email addresses, keys and home paths: never weaken it to make a commit pass.
- The privacy boundary is `transactions_clean` (`ledger.CLEAN_COLUMNS`) and `bankhub.queries`: the MCP
  server reads nothing else. Never add names, IBANs or descriptions there.
- Bank transfers are matched on the counterparty name only, never on free-text descriptions.
- There is no payment code path, and there must never be one.
- Amounts are integer minor units in the raw layer; reports use RON at the BNR rate.
- Schema changes are new files in `src/bankhub/migrations/`; they run on open (the web app opens the
  database read-write once at start for this). Add a test that opens a database in the old shape.
- Statement readers (PDF, CSV) live in `src/bankhub/statements/`; a new bank follows the convention in
  `CONTRIBUTING.md` and is tested with `tests/reader_conformance.py`.
- After changing a bank reader in `normalize.py`, `bankhub renormalize` re-reads stored payloads.
- PWA text is built with `textContent` only (bank data is untrusted); styles go through CSSOM because of
  the CSP.

More detail: `README.md`, `docs/DESIGN.md`, `docs/DEPLOY.md`, `docs/BANKS.md`, `CONTRIBUTING.md`,
`SECURITY.md`.
