"""Statements (PDF or CSV) from the bank: credit cards the bank does not share over open banking, and current
accounts whose history the bank's API only hands out slowly.

A card statement is read into lines, a summary (owed, limit, due date) and instalment plans, and stored on an
account whose `source` is 'statement'. A current account statement adds the payments the sync has not
fetched yet to the linked account, and the sync later replaces them with the bank's own records. Only what is
parsed is kept: the PDF itself holds the holder's name and address and is never stored.

Layout: `base` (types, registry), one module per bank (`brd`), `csv_import` (any bank, by column mapping) and
`store`. A new bank's reader registers itself in its module and is imported below.
"""

from .base import (MAX_PDF_BYTES, AccountLine, AccountStatement, Line, Plan, Reader, Statement, StatementError,
                   parse_statement, pdf_text, readers, register, warn_unless)
from .brd import parse_brd_account, parse_brd_card   # registers the BRD readers
from .store import store, store_account_statement, store_statement
