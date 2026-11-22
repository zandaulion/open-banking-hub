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
- **A low daily request limit** ("TPP maximum number of hits was reached"). A few requests per account per
  sync, spent in this order: the current month, then the days before it that earlier syncs did not reach,
  then older history (two years take several days; statements fill it in at once). When the limit is hit,
  what arrived is kept and the rest waits for the next sync. Unattended access reaches back 90 days; right
  after a new consent the bank answers for older dates too, so a fresh link fetches older history first.
- **The credit card is not offered over open banking** (the Control Panel and the consent list
  show only current accounts). Add its monthly statement instead: see below.
- **The description is `type|channel|reference|party|account or card|…`.** Bank Hub uses the
  first part as the transaction type. For card payments it takes the shop after the masked card
  number. Some foreign card payments carry only the cardholder's own name, which is never taken
  as the payee.
- **Card payments to Revolut** (`REVOLUT**1234* VILNIUS`) count as transfers between your own
  accounts.

## Statements

Add a monthly statement on **Accounts → Add a statement**. Nothing about the file is kept: only the
payments it holds, and for a card what is owed and when. Adding the same statement twice changes nothing.

- **A CSV from any bank** (**Import a CSV**): choose the account and the file, and confirm which column
  is the date, the amount (one signed column, or separate debit and credit columns), the description and,
  if there is one, the counterparty and its IBAN. The delimiter, header rows, date format and number
  format are guessed and can be changed. The choice is remembered for the account, so the next file needs
  no setup. Rows that cannot be read are counted and said so; a file where most rows fail is refused.
- **A CAMT.053 or OFX/QFX file** (several at once work): the standard statement exports many banks offer
  (CAMT.053 is the European ISO 20022 statement; OFX is common elsewhere). Payee, IBAN and description come
  from the file's own fields. A CAMT statement's lines must take its opening balance to its closing one;
  OFX has no opening balance, so it cannot be checked that way. If the file's account number is not an IBAN
  of a linked account, the app asks which account it belongs to.
- **A PDF** (several at once work): the kind is recognised from the file. The readers that exist are listed
  below; adding one for another bank is described in [CONTRIBUTING.md](../CONTRIBUTING.md). Bank Hub reads
  PDFs with `pdftotext` (poppler-utils; the Docker image has it).

For a current account, only payments the account does not have yet are added. A payment the sync already
fetched (same amount, within two days) is left alone, and when the sync reaches a period a statement filled
in, the bank's own records replace the statement's lines, so nothing is counted twice. The account's
history, categories and transfers then work as for any synced account.

- **BRD current account (PDF):** the statement's IBAN must belong to a linked account. The lines must add
  up to its totals and its opening and closing balance, or the app says so. Payroll credits ("Alim.
  conventie") are income.

### Credit cards

For a card a bank does not share, the statements are the only source: the card shows under **Accounts →
Credit cards**. Bank Hub keeps the payments, what is owed, the limit, the due date and the instalment
plans, not the PDF with the cardholder's name and address.

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
