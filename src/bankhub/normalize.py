"""Turn an Enable Banking transaction payload into canonical raw-layer fields."""

import hashlib
import json
import re
from dataclasses import dataclass
from decimal import Decimal


@dataclass
class NormalizedTx:
    dedup_key: str
    entry_reference: str | None
    status: str
    booking_date: str | None
    value_date: str | None
    amount_minor: int
    currency: str
    counterparty_name: str | None
    counterparty_iban: str | None
    counterparty_has_account: bool
    remittance: str | None
    payload: str
    bank_code: str | None = None


def to_minor(amount: str | float) -> int:
    return int((Decimal(str(amount)) * 100).quantize(Decimal(1)))


def _party(tx: dict, role: str) -> tuple[str | None, str | None, bool]:
    party = tx.get(role) or {}
    account = tx.get(f"{role}_account") or {}
    iban = account.get("iban")
    return party.get("name"), iban, bool(iban or account.get("other"))


# ING Romania writes the payee into the description as "Label, value, Label, value, ...".
ING_LABELS = {"Details", "Reference number", "To account", "From account", "Ordering party", "Beneficiary", "Bank",
              "Card number", "Transaction at", "Authorization date", "Authorization number", "Amount",
              "Settlement amount", "Exchange rate"}
IBAN = re.compile(r"^[A-Z]{2}\d{2}[A-Z0-9]{11,30}$")


def parse_labelled(text: str, labels: set[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    key = None
    for part in (p.strip() for p in text.split(",")):
        if part in labels:
            key = part
            out.setdefault(key, "")
        elif key is not None:
            out[key] = f"{out[key]}, {part}" if out[key] else part
    return out


def _ing(tx: dict, is_debit: bool, text: str) -> tuple[str | None, str | None, bool, str | None]:
    """(counterparty name, counterparty IBAN, has account, description) for an ING transaction."""
    f = parse_labelled(text, ING_LABELS)
    if "Transaction at" in f:
        # Card payment: "Shop Name Branch  RO  City" -> merchant before the country code.
        place = f["Transaction at"]
        name = re.split(r"\s{2,}[A-Z]{2}\s{2,}", place)[0].strip()
        return name or None, None, False, place
    name = f.get("Beneficiary") if is_debit else f.get("Ordering party")
    account = (f.get("To account") if is_debit else f.get("From account")) or ""
    iban = account if IBAN.match(account.replace(" ", "")) else None
    return (name or None), iban, bool(account), (f.get("Details") or None)


def _brd(tx: dict, is_debit: bool) -> tuple[str | None, str | None, bool, str | None, str | None] | None:
    """BRD writes "type|channel|reference|party|account or card|bank or merchant|details...".
    Returns (name, IBAN, has account, description, transaction type), or None for other layouts."""
    parts = [p.strip() for p in "|".join(tx.get("remittance_information") or []).split("|")]
    if len(parts) < 4:
        return None
    kind, channel = parts[0], parts[1]
    if channel == "Card/Terminale":
        # Card payment: the shop follows the masked card number. Some foreign payments carry only the
        # cardholder's name, which must never be taken for the payee.
        merchant = None
        for i, p in enumerate(parts):
            if p.startswith("Card nr") and i + 1 < len(parts) and parts[i + 1]:
                merchant = parts[i + 1]
                break
        return merchant, None, False, kind, kind
    party = parts[3] or None
    account = parts[4].lstrip("/").replace(" ", "") if len(parts) > 4 else ""
    iban = account if IBAN.match(account) else None
    details = " · ".join(p for p in parts[6:] if p and p != "Transfer") if iban else \
        " · ".join(p for p in parts[4:] if p)
    description = f"{kind} · {details}" if details else kind
    return party, iban, bool(iban), description, kind


def normalize(tx: dict, institution: str | None = None) -> NormalizedTx:
    amount = tx["transaction_amount"]
    is_debit = tx.get("credit_debit_indicator") == "DBIT"
    minor = abs(to_minor(amount["amount"]))
    remittance = " | ".join(r for r in (tx.get("remittance_information") or []) if r) or None
    code = (tx.get("bank_transaction_code") or {})
    bank_code = code.get("description") or code.get("code") or None
    brd = _brd(tx, is_debit) if institution == "BRD" else None
    if institution == "ING":
        name, iban, has_account, remittance = _ing(tx, is_debit, ", ".join(tx.get("remittance_information") or []))
    elif brd:
        name, iban, has_account, remittance, bank_code = brd   # BRD's code field holds only a BIC
    else:
        # The counterparty is whoever is on the other side of the money flow.
        name, iban, has_account = _party(tx, "creditor" if is_debit else "debtor")
    booking = tx.get("booking_date") or tx.get("value_date") or tx.get("transaction_date")
    ref = tx.get("entry_reference") or tx.get("transaction_id")
    if ref:
        key = f"ref:{ref}"
    else:
        basis = json.dumps([booking, amount, tx.get("credit_debit_indicator"),
                            " | ".join(tx.get("remittance_information") or []),
                            ((tx.get("creditor") or {}) if is_debit else (tx.get("debtor") or {})).get("name")],
                           sort_keys=True)
        key = "hash:" + hashlib.sha256(basis.encode()).hexdigest()[:32]
    return NormalizedTx(
        dedup_key=key,
        entry_reference=ref,
        status=tx.get("status") or "BOOK",
        booking_date=booking,
        value_date=tx.get("value_date"),
        amount_minor=-minor if is_debit else minor,
        currency=amount["currency"],
        counterparty_name=name,
        counterparty_iban=iban,
        counterparty_has_account=has_account,
        remittance=remittance,
        payload=json.dumps(tx, ensure_ascii=False, sort_keys=True),
        bank_code=bank_code,
    )
