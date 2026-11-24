# Changelog

## Unreleased

- Charts open on the latest months again. Since the weekly summary card, Month waited for it before showing
  the page, and the chart scrolled to its end before it was on screen, so it opened on the oldest months.
- BRD sync: dates older than about 90 days are only asked for right after a new link. Outside that window
  BRD answers them with an empty list rather than an error, which had marked months of missing history as
  complete. Health now says which dates are still missing and that a new link is what brings them.
- Accounts and Settings are apart. Accounts shows what you have: balances, net worth, credit cards and the
  groups' balances, with one line saying whether every bank is up to date. Settings (⚙ on Accounts, or the
  last item in the laptop sidebar) has the banks with their health, Sync now and linking, adding statements
  and CSVs, groups, your categories, notifications with the weekly summary, export and the app version.
- When something needs a look (a bank that has not synced for two days, backups that stopped, a card
  statement over 40 days old), Month shows a banner that opens Settings.
- Coming back from linking a bank, and the alerts about consents, syncing and backups, open Settings.
- A chart tooltip no longer stays on screen after moving to another tab.
- The demo shows its made-up banks as synced.
- Weekly summary: a notification once a week (Monday 08:00 by default; choose the day and hour under
  Notifications) and a card at the top of Month. Last week's day-to-day spending against your usual week,
  with regular payments shown apart; the month so far against the same days of last month; categories well
  above usual; payments worth a look (perhaps charged twice, much bigger than usual, a new payee, a new regular
  payment); and what is due in the coming week, card payments included. It says when an account looks
  incomplete, and leaves out cards added from statements. Rules and arithmetic, no AI.
- BRD sync: within the current month it carries on a few days before the newest payment it already has,
  instead of asking for the month's first page again after being cut short by the bank's limit.
- The "credit card payment is due" alert switch was not saved; it is now.
- Statements in the standard formats many banks export: CAMT.053 (the European ISO 20022 XML statement,
  versions .02 to .08) and OFX/QFX (1 and 2). Payee, IBAN and description come from the file; a CAMT
  statement is checked against its opening and closing balances. If a file's account is not a linked IBAN,
  the app asks which account it is. XML with a DOCTYPE is refused.
- Net worth by month on Accounts: accounts minus what is owed on cards, worked back from today's balances
  and starting where every account's history is complete (it says from which month). Groups are left out.
- Your data: download every transaction as CSV (opens in a spreadsheet), or everything as JSON.

## 0.6.0 (2026-11-21)

- Laptop and desktop layout. From 900 px wide the tabs move to a sidebar, cards flow into columns (three
  on very wide screens) and dialogs are centred. Month puts the figures beside a chart of up to 24 months,
  with categories and payees side by side. Activity is a table (payee, description, category, account,
  amount), and a payment opens in a panel beside it, so you can work down the list. Recurring puts the
  totals beside the chart and the payments in a table (when, change, a sparkline, the last amount) beside
  the income. Accounts has three columns: what you have, other people's money and cards, and the
  connections, health and alerts. On Sort and My rules a payee or rule opens beside the list. Charts are
  drawn to the width of their card, so text keeps its size on a big screen. Between 900 and 1199 px the
  grids are half and half. Keys: ← and → change month, / searches Activity, Esc closes. The phone layout
  is unchanged.
- Laptop screenshots in the README and on the website.

## 0.5.0 (2026-11-21)

- The demo (`bankhub demo`) now includes a child's account in a group and a credit card added from three
  statements, and the screenshots are new.
- A card added from statements of a bank whose name has spaces gets a proper alias (`demo_card_bank_card`).
- CSV import: a column headed like a description or a payee is taken as one before the longest text.
- BRD sync: the current month comes first, then the days before it that earlier syncs did not reach, then
  older history; so an account that is weeks behind is up to date after one sync. A request limit reached
  after some payments arrived no longer reports the account as skipped. Pending payments are only cleared
  by an answer that reaches today.
- Month chart: the selected month is outlined, with no band behind it and no dimming of the others, so a
  month keeps the same colours whether or not it is selected. Income you did not spend is a hatched green
  "left over" segment (the mirror of the red "over income", and hatched so colour alone is not what tells
  them apart). A month with no income in the app shows everything spent as over income, like the cards above it.
- CSV import for any bank: Accounts → Add a statement → Import a CSV. Choose the account and the file;
  the delimiter, date format, number format and columns are guessed, and confirmed once (remembered for
  the account). Only payments the account does not have yet are added, like a PDF statement.
- Statement readers are pluggable: each bank's reader registers itself (`src/bankhub/statements/`), and
  CONTRIBUTING.md describes the convention (detect narrowly, signed minor units and ISO dates, check the
  lines against the statement's totals, errors that never quote the file). `tests/reader_conformance.py`
  checks it; the BRD readers follow it.
- A payment the bank itself labels (interest, tax, a fee, cash, payroll) is never matched by amount to a
  credit elsewhere as a move between your accounts: a 14.24 RON interest charge had been paired with a
  14.14 RON Revolut top-up the next day.
- Current account statements: a debit BRD prints two columns right of its usual place (some months, for some
  lines) was read as a credit, so a loan instalment showed as income. Credits are now told apart by how far
  right they sit, and the totals check catches any line that is still misplaced.
- Statements for BRD current accounts. Add a month's statement (PDF) and its payments appear at once, for
  history the sync has not fetched yet. They are checked against the statement's totals and balances,
  not counted twice if the sync already has them, and replaced by the bank's own records when the sync
  gets there. Payroll credits ("Alim. conventie": salary, bonus) now count as income, so Month shows income
  for BRD; loan instalments ("Rambursare credit") and insurance have their own categories.
- Credit cards from statements. A bank that does not share a credit card (BRD) can still be covered:
  on Accounts, add the monthly statement (PDF), several at once for older months. The app reads it
  itself and keeps only the payments, what is owed, the limit, the due date and the instalment plans,
  not the file. Purchases are categorised like any card payment, a repayment from your own account is a
  transfer, and a purchase turned into instalments is counted once. What is owed is shown apart from your
  total. Health warns when the latest statement is over 40 days old, and an alert comes three days
  before the payment is due (unless the minimum has already been paid). Needs `pdftotext`.
- Groups are recognised more strictly. A card payment never joins a group because its amount matched one
  on the group's account (a 5 RON ride the day a child got 5 RON), and a matching amount only teaches a
  payee name when most transfers to that name match, twice at least. Payments are then assigned by name.
- Each group lists the payee names it recognises, with how many payments each brought. Untick one that was
  matched by mistake, or add a name by hand: a group can be just a person with no linked account (a
  partner you send household money to), counted the way the group says.
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
