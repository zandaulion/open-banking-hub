"""Synthetic fixtures shaped like real Enable Banking (Revolut) payloads. No real data."""

import itertools
from pathlib import Path

import pytest

from bankhub import sync
from bankhub.db import connect, transaction, utcnow
from bankhub.rules import load_rules

_ref = itertools.count(1)


def tx(amount: str, day: str, *, debit: bool = True, name: str | None = None, text: str | None = None,
       iban: str | None = None, other: dict | None = None, ref: str | None = None, status: str = "BOOK",
       currency: str = "RON") -> dict:
    """One transaction in Enable Banking's shape. Amounts are positive; direction is in the indicator."""
    party = {"name": name, "postal_address": None, "organisation_id": None, "private_id": None,
             "contact_details": None}
    account = {"iban": iban, "other": other} if (iban or other) else {"iban": None, "other": None}
    empty = {"name": None, "postal_address": None, "organisation_id": None, "private_id": None,
             "contact_details": None}
    return {
        "entry_reference": ref or f"00000000-0000-4000-8000-{next(_ref):012d}",
        "transaction_id": None,
        "merchant_category_code": None,
        "transaction_amount": {"amount": amount, "currency": currency},
        "creditor": party if debit else empty,
        "creditor_account": account if debit else {"iban": None, "other": None},
        "debtor": empty if debit else party,
        "debtor_account": {"iban": None, "other": None} if debit else account,
        "bank_transaction_code": {"code": None, "sub_code": None, "description": None},
        "credit_debit_indicator": "DBIT" if debit else "CRDT",
        "status": status,
        "booking_date": day,
        "value_date": day,
        "transaction_date": None,
        "remittance_information": [text or name or ""],
        "exchange_rate": None,
        "note": None,
        "balance_after_transaction": None,
        "reference_number": None,
        "reference_number_schema": None,
    }


def session(institution: str, accounts: list[tuple[str, str, str]], session_id: str = "sess-1") -> dict:
    """accounts: (uid, currency, cash_account_type)."""
    return {
        "session_id": session_id,
        "aspsp": {"name": institution, "country": "RO"},
        "psu_type": "personal",
        "access": {"valid_until": "2099-01-01T00:00:00Z"},
        "accounts": [
            {"uid": uid, "currency": cur, "cash_account_type": typ, "identification_hash": f"hash-{uid}",
             "account_id": {"iban": f"RO00TEST{abs(hash(uid)) % 10**16:016d}"}, "all_account_ids": [],
             "name": "Synthetic", "usage": "PRIV"}
            for uid, cur, typ in accounts
        ],
    }


@pytest.fixture
def conn(tmp_path: Path):
    c = connect(tmp_path / "bankhub.db")
    # Fixed FX so tests do not need the network.
    c.executemany("INSERT INTO fx_rates(date, currency, rate_ron) VALUES (?,?,?)",
                  [("2024-01-01", "EUR", 5.0), ("2024-01-01", "USD", 4.5)])
    yield c
    c.close()


@pytest.fixture
def rules():
    return load_rules(None)


def store(conn, account_uid: str, txs: list[dict]) -> None:
    acc = conn.execute("SELECT id FROM accounts WHERE uid=?", (account_uid,)).fetchone()
    with transaction(conn):
        sync._store_transactions(conn, acc["id"], txs)
