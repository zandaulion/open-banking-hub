# Changelog

## 0.3.1 (2026-11-20)

- Month: the swipe also starts on the category and payee rows (a tap still opens them; a drag doesn't).

## 0.3.0 (2026-11-20)

- Offline: the app keeps a copy of every screen on the phone, saves all months, Recurring, Accounts and
  recent payments ahead of time on Wi-Fi, and shows the copy with a banner when the server can't be
  reached. Changes wait for the server. Copies expire after 14 days and are wiped when the device is revoked.
- Month: swipe to the next or previous month. The page follows the finger, is thrown past a third of
  the screen or on a quick flick, springs back otherwise, and stretches only a little at the first or
  current month; a short vibration marks the point of no return. Neighbouring months are fetched in
  advance, so the new month appears at once. Swipes on the chart still scroll the chart; swipes from
  the screen edges are left to the system's back gesture.

## 0.2.0 (2026-11-20)

- **Notifications on your phone** (Web Push): a large payment, a low balance, late regular income, a
  changed regular payment, a bank consent about to end, a bank that stopped syncing, backups that stopped.
  Private by default: no amounts or payees on the lock screen unless you turn details on. `bankhub alerts`
  previews them.
- **Health** on Accounts: last successful sync per account, banks skipped for their request limit, history
  still being filled in (BRD), and the last local and off-site backup.
- **Ready-made MCP prompts**: Monthly review, Subscription audit, Unusual spending, Savings ideas. The MCP
  server now tells assistants to treat every returned value as data, never as instructions.
- **Published container image** `ghcr.io/zandaulion/open-banking-hub` for amd64 and arm64, built and tested
  by GitHub Actions on every release; Docker Compose uses it by default.
- **Website** with a page for Romania: [zandaulion.github.io/open-banking-hub](https://zandaulion.github.io/open-banking-hub/).

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
