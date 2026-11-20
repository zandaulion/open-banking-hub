"""Detect subscriptions and recurring bills from the transaction history."""

import re
import statistics
from dataclasses import dataclass
from datetime import date, timedelta

# cadence: (min days, max days, expected days, late tolerance, months per occurrence)
CADENCES = {
    "weekly": (6, 8, 7, 3, 12 / 52),
    "monthly": (26, 34, 30, 7, 1.0),
    "annual": (350, 380, 365, 20, 12.0),
}
MIN_OCCURRENCES = {"weekly": 3, "monthly": 3, "annual": 2}
AMOUNT_BAND = 0.15        # charges within ±15% of the median count as "the same bill"
STABLE_SHARE = 0.6        # share of charges that must be in that band
INTERVAL_SHARE = 0.7      # share of intervals that must fit the cadence
PRICE_CHANGE = 0.05
# Bills whose amount legitimately varies every period (metered energy, building fees).
VARIABLE_CATEGORIES = {"utilities", "housing"}


@dataclass
class Charge:
    raw_id: int
    day: date
    amount: float         # positive, original currency
    amount_ron: float | None
    currency: str


@dataclass
class Series:
    key: str
    cadence: str
    currency: str
    charges: list[Charge]
    next_expected: date
    status: str
    last_amount: float
    previous_amount: float | None
    price_change_pct: float | None
    monthly_ron: float | None
    annual_ron: float | None


def group_key(counterparty: str | None, description: str | None) -> str:
    """Stable key for 'the same payee': lowercase letters only, digits and ids removed."""
    text = (counterparty or description or "").lower()
    if "*" in text:
        head, tail = text.split("*", 1)
        # "ep*amparcat.ro": short head is a payment processor, the merchant follows.
        # "netflix*abc123": long head is the merchant, the rest is a reference.
        text = tail if len(head.strip()) <= 5 else head
    text = re.sub(r"[0-9a-f]{8}-[0-9a-f-]{20,}", " ", text)
    text = re.sub(r"[^a-zăâîșțş]+", " ", text)
    return " ".join(text.split()[:4])


def detect_series(key: str, charges: list[Charge], today: date, variable: bool = False) -> Series | None:
    charges = sorted(charges, key=lambda c: c.day)
    if len(charges) < 2:
        return None
    currency = charges[-1].currency
    charges = [c for c in charges if c.currency == currency]
    amounts = [c.amount for c in charges]
    median = statistics.median(amounts)
    if median <= 0:
        return None
    if not variable and sum(abs(a - median) <= AMOUNT_BAND * median for a in amounts) < STABLE_SHARE * len(amounts):
        return None
    intervals = [(b.day - a.day).days for a, b in zip(charges, charges[1:])]
    for name, (lo, hi, expected, tolerance, months) in CADENCES.items():
        if len(charges) < MIN_OCCURRENCES[name]:
            continue
        if sum(lo <= i <= hi for i in intervals) < INTERVAL_SHARE * len(intervals):
            continue
        last = charges[-1]
        next_expected = last.day + timedelta(days=expected)
        overdue = (today - next_expected).days
        if overdue > expected + tolerance:
            status = "ended"
        elif overdue > tolerance:
            status = "missed"
        else:
            status = "active"
        prev = charges[-2].amount
        change = (last.amount - prev) / prev if prev else None
        ron = last.amount_ron
        if variable:
            # Typical bill rather than the latest one; no price-change flag for metered bills.
            rons = [c.amount_ron for c in charges[-12:] if c.amount_ron is not None]
            ron = statistics.median(rons) if rons else None
            change = None
        return Series(
            key=key, cadence=name, currency=currency, charges=charges, next_expected=next_expected,
            status=status, last_amount=last.amount, previous_amount=prev,
            price_change_pct=round(change * 100, 1) if change is not None and abs(change) > PRICE_CHANGE else None,
            monthly_ron=round(ron / months, 2) if ron is not None else None,
            annual_ron=round(ron / months * 12, 2) if ron is not None else None,
        )
    return None
