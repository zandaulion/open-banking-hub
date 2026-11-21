# Changelog

## Unreleased

- BRD history now arrives in full. BRD returns at most 20 transactions per request, the oldest 20 of the
  range asked for, so each request starts where the last one stopped and is saved straight away. A few
  requests per sync fill in two years over several days, and a rate limit only pauses the work. Before,
  an account linked with older code could get stuck at its first 20 transactions.
- Unattended syncs never ask for more than the last 90 days. A quiet account's last payment can be months
  old, and ING refused the older start (error 422).
- Health shows how far the older history has got, and the sync log records the bank's reason when it
  refuses a request.
- Activity can show one account: tap an account on Accounts, then **See its payments**. The account
  shows as a chip that combines with search, month and category; own transfers stay in the list.

## 0.4.0 (2026-11-21)

- Account groups, for money that isn't yours: a child's or a parent's accounts, a business, an account
  you manage for someone. Create one on Accounts, put accounts in it, and choose how it counts:
  - **Someone I support:** what you send counts in the group's own category (e.g. Kids · allowance,
    Family · parents); what comes back is other money in, or reduces that category;
  - **Kept separate:** money between you is a transfer (not counted), or spending if you prefer.

  The group's own payments stay visible in Activity but out of your totals, and its balance is shown
  apart. Month lists what went to each group. Transfers are found by shared reference, IBAN, matching
  amounts (a child's account often has no IBAN) and the names learned from those. Group names stay in
  the app; AI assistants see only the category.
- Products under one company name are told apart: built-in rules for Google Cloud, Google Play,
  Google One and YouTube Premium (before the general "Google"), and Google's 0.00 card checks as fees.
- "All payments to this payee" keeps the product in its match text (`Google*cloud Ab12` → `google*cloud`),
  shows it editable with a live count and examples, and a narrower match makes a new rule that is checked
  first instead of changing the broad one.
- Top-ups through Google Pay or Apple Pay count as transfers between your own accounts.
- Offline fixes (found on a plane):
  - the offline copy is filled on mobile data too (it was Wi-Fi only, so usually only the screen you
    had open was saved), starting from any screen; about 0.4 MB, at most every six hours;
  - a network that hangs instead of failing (plane Wi-Fi, a VPN with no route) no longer keeps the app
    from opening: the app files fall back to the copy after 2.5 s, data after 4 s, and once that
    happens everything comes from the copy straight away for a minute; a cold start takes ~3 s;
  - API answers are gzip-compressed (a page of payments: 24 KB → 2 KB).

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
