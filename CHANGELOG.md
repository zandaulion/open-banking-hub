# Changelog

## 0.1.0 (2026-11-20)

First public release.

- Sync from any bank supported by Enable Banking (PSD2, read-only), with full-history import on consent,
  a daily request budget, pending-payment replacement and windowed fetching for banks that truncate.
- Bank readers for Revolut, ING Romania and BRD.
- Redacted ledger: allowlisted columns, merchants named only when a rule matches, a leak scanner.
- Detection of transfers between your own accounts (same reference, own IBANs, own-account patterns,
  amount matching across banks).
- Categories from bank transaction types, merchants and keyword rules; one-offs; refunds net against
  their category; income detected from regular payers.
- Recurring payments with bill-over-bill month-over-month change.
- The owner's PWA: Month, Recurring, Activity (search by name or amount), Sort, My rules, Accounts with
  in-app bank linking; invitations per device.
- Read-only MCP server with five tools.
- `bankhub init`, `invite`, `devices`, `demo`; systemd units, Docker Compose, nightly snapshots and an
  optional restic off-site copy.
