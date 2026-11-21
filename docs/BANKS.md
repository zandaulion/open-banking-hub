# Banks: what to expect

Everything goes through Enable Banking, but each bank sends different data and has its own
rules. These are the banks Bank Hub was built against, and what it does about each.

## Enable Banking, in general

- **Restricted mode returns linked accounts only.** If a consent succeeds but brings **0
  accounts**, the bank's accounts are not linked to your application in the Control Panel. Link
  them there, then link the bank again from the app.
- **Approve in a real browser.** If an approval link opens inside another app's built-in browser,
  you may not get back to Bank Hub. Open it in Chrome or Safari.
- **Old history is often only available right after approval.** Bank Hub fetches the longest
  history a bank allows immediately after you approve. Later syncs only fetch recent days.
- **Consents last up to 180 days.** Accounts shows the days left and warns two weeks ahead.
  Renewing keeps the same accounts and all history.
- **About four unattended requests per account per day.** Syncs are scheduled twice a day, and
  Bank Hub refuses to go over its own budget. "Sync now" counts too.

## Revolut

- **History:** about two years on first approval.
- **One account per currency pocket and savings vault.** Moves between them show up twice with
  the same reference and are matched as transfers between your own accounts.
- **Moves to other pockets** (`To RON …`, `Exchanged to EUR`, savings) and **card top-ups from
  another bank** (`Top-Up by *1234`) count as your own transfers.
- **No category codes.** Categories come from the payee name and description.

## ING Romania

- **History:** about two years on first approval.
- **The payee fields are always empty.** ING writes everything into the description as
  `Label, value, Label, value`. Bank Hub reads the shop from *Transaction at* (card payments),
  the other party from *Beneficiary* / *Ordering party*, and their IBAN from *To account* /
  *From account*.
- **It sends a useful transaction type** ("POS purchase", "Credit Repayment", "Life insurance
  premium (credits)", "Deposit creation" …). Bank Hub uses it for loans, insurance, fees,
  interest and cash, and treats term deposits as moves between your own accounts.

## BRD

- **One active consent per application.** Linking accounts in the Control Panel and approving a
  consent from Bank Hub cancel each other out ("Unable to link accounts"). The order that works:
  1. **Link accounts** in the Control Panel and approve in MyBRD.
  2. In MyBRD, **delete** that consent.
  3. Link BRD from Bank Hub and approve the new consent.
  The accounts stay linked in the Control Panel after step 2.
- **Decoupled approval.** The Enable Banking page shows a consent number and waits while you
  approve it in MyBRD. It moves on by itself; if it seems stuck, reload it. Don't press Cancel.
- **At most 20 transactions per request, and no "next page".** A full answer holds the *oldest*
  20 of the range asked for, so Bank Hub asks again from the last day it reached, and saves each
  answer straight away. A request spans a year at most.
- **A low daily request limit.** A few requests per account per sync, so two years of history take
  several days. If BRD's limit is hit, that sync skips BRD and continues next time. Unattended
  access reaches back 90 days; right after a new consent the bank answers for older dates too, so
  a fresh link fetches older history first.
- **The credit card is not offered over open banking** (the Control Panel and the consent list
  show only current accounts). Add its monthly statement instead: see below.
- **The description is `type|channel|reference|party|account or card|…`.** Bank Hub uses the
  first part as the transaction type. For card payments it takes the shop after the masked card
  number. Some foreign card payments carry only the cardholder's own name, which is never taken
  as the payee.
- **Card payments to Revolut** (`REVOLUT**1234* VILNIUS`) count as transfers between your own
  accounts.

## Credit cards from statements

For a card a bank does not share, add the monthly statement (PDF) on **Accounts → Credit cards**
(several files at once work, for older months). Bank Hub reads the PDF with `pdftotext`
(poppler-utils; the Docker image has it) and keeps only what it reads: the payments, what is owed,
the limit, the due date and the instalment plans. The PDF itself, with the cardholder's name and
address, is not stored. Adding a statement twice changes nothing.

- **BRD credit card:** purchases get categories like any card payment. A repayment from one of your
  accounts is a transfer between your own accounts (the statement names the paying IBAN). A purchase turned into
  interest-free instalments appears on the statement as the purchase, a reversal and the plan; the
  reversal and plan cancel and the purchase stays where it was made. Monthly instalments are not
  separate lines, so nothing counts twice.
- **Checked against the statement:** the purchases read must add up to its "amounts used" and the
  repayments to its "amounts repaid"; if they don't, the app says so after adding it.
- **Owed is not part of your total.** A card is shown with what is owed, the limit, the due date and a
  warning when the statement is over 40 days old. An alert fires three days before the due date,
  unless a payment of at least the minimum to the card has already left your accounts.

## Adding another bank

Usually nothing is needed. If its data looks wrong in the app:

- **Payee missing or wrong:** add a reader in `src/bankhub/normalize.py`, like `_ing` and
  `_brd`, then run `bankhub renormalize`.
- **Transaction types worth using:** add `bank_code_rules` in `categories.yaml`.
- **Moves between your own accounts not recognised:** add `own_account_patterns` for the bank.
- **History cut short:** if the bank truncates lists without a continuation key, add it to
  `WINDOWED` in `src/bankhub/sync.py`.
