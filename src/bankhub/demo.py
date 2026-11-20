"""A year of made-up banking data, shaped like Enable Banking payloads, for trying Bank Hub
without a bank (and for screenshots). Every name, amount and account here is invented.
"""

import random
import uuid
from datetime import date, timedelta

from . import sync
from .db import transaction, utcnow
from .normalize import to_minor

EMPLOYER = "Northwind Software SRL"
FRIEND = "Ana Example"
LANDLORD = "Mihai Example"


def _tx(amount: float, day: date, *, debit: bool = True, name: str | None = None, text: str | None = None,
        iban: str | None = None) -> dict:
    party = {"name": name}
    account = {"iban": iban, "other": None}
    nobody = {"name": None}
    return {
        "entry_reference": str(uuid.uuid4()),
        "transaction_amount": {"amount": f"{amount:.2f}", "currency": "RON"},
        "creditor": party if debit else nobody,
        "creditor_account": account if debit else {"iban": None, "other": None},
        "debtor": nobody if debit else party,
        "debtor_account": {"iban": None, "other": None} if debit else account,
        "credit_debit_indicator": "DBIT" if debit else "CRDT",
        "status": "BOOK",
        "booking_date": day.isoformat(),
        "value_date": day.isoformat(),
        "remittance_information": [text or name or ""],
        "bank_transaction_code": {},
    }


def _session(bank: str, session_id: str, accounts: list[tuple[str, str]]) -> dict:
    return {
        "session_id": session_id,
        "aspsp": {"name": bank, "country": "RO"},
        "access": {"valid_until": (date.today() + timedelta(days=150)).isoformat() + "T00:00:00Z"},
        "accounts": [{"uid": uid, "currency": "RON", "cash_account_type": typ, "identification_hash": f"demo-{uid}",
                      "account_id": {"iban": iban}} for uid, typ, iban in accounts],
    }


def generate(months: int = 14, seed: int = 7) -> dict[str, list[dict]]:
    """Transactions per account uid: salary at one bank, daily life on a card account, savings."""
    rng = random.Random(seed)
    today = date.today()
    start = (today.replace(day=1) - timedelta(days=31 * (months - 1))).replace(day=1)
    salary, daily, savings = [], [], []

    def days_in(month_start: date):
        d = month_start
        while d.month == month_start.month and d <= today:
            yield d
            d += timedelta(days=1)

    m = start
    while m <= today:
        month_days = list(days_in(m))
        def on(day_of_month):
            return next((d for d in month_days if d.day == day_of_month), None)
        # Income and moving money around.
        if (d := on(10)):
            pay = 9400 + rng.choice([0, 0, 0, 250, 600])
            salary.append(_tx(pay, d, debit=False, name=EMPLOYER, iban="RO00DEMO0000000000000099"))
            salary.append(_tx(3500, d + timedelta(days=1), name="Me", iban="RO00DEMO0000000000000002", text="Top-up"))
            daily.append(_tx(3500, d + timedelta(days=1), debit=False, text="Top-Up by *4821"))
            salary.append(_tx(2600, d + timedelta(days=2), name=LANDLORD, iban="RO00DEMO0000000000000042",
                              text="Rent"))
            daily.append(_tx(500, d + timedelta(days=3), text="To RON Savings"))
            savings.append(_tx(500, d + timedelta(days=3), debit=False, text="To RON Savings"))
        # Bills and subscriptions.
        for day_, name, amount in [(1, "Digi", 62.5), (3, "Netflix.com", 59.2), (5, "Spotify", 29.99),
                                   (14, "Vodafone", 47.65), (21, "YouTube", 62.0)]:
            if (d := on(day_)):
                bump = 1.1 if name == "Netflix.com" and m >= today.replace(day=1) - timedelta(days=95) else 1.0
                daily.append(_tx(round(amount * bump, 2), d, name=name))
        if (d := on(28)):
            winter = m.month in (11, 12, 1, 2, 3)
            salary.append(_tx(round(rng.uniform(260, 420) if winter else rng.uniform(140, 220), 2), d, name="PPC Energie"))
            salary.append(_tx(round(rng.uniform(280, 380), 2), d, name="E-bloc", text="Ep*e-bloc.ro"))
        # Everyday life.
        for d in month_days:
            if rng.random() < 0.45:
                shop = rng.choice(["Lidl", "Kaufland", "Mega Image", "Carrefour"])
                daily.append(_tx(round(rng.uniform(25, 310), 2), d, name=shop))
            if rng.random() < 0.22:
                daily.append(_tx(round(rng.uniform(35, 120), 2), d, name=rng.choice(["Glovo", "Tazz", "Wolt"])))
            if rng.random() < 0.3:
                daily.append(_tx(round(rng.uniform(12, 48), 2), d, name="Bolt"))
            if rng.random() < 0.18:
                daily.append(_tx(5.0, d, name="Metrorex"))
            if rng.random() < 0.08:
                daily.append(_tx(round(rng.uniform(60, 650), 2), d, name="eMAG"))
            if rng.random() < 0.06:
                daily.append(_tx(round(rng.uniform(15, 90), 2), d, name=rng.choice(["Catena", "Dr. Max"])))
            if rng.random() < 0.05:
                daily.append(_tx(round(rng.uniform(18, 75), 2), d, name=rng.choice(
                    ["Corner Bakery SRL", "Green Leaf Bistro", "Atelier Nine SRL", "Kronos Trading SRL",
                     "Sigma Print SRL", "Nordlicht Studio", "Blue Door Gallery"])))
            if rng.random() < 0.03:
                daily.append(_tx(round(rng.uniform(30, 150), 2), d, name=FRIEND, iban="RO00DEMO0000000000000007",
                                 text="dinner"))
            if rng.random() < 0.02:
                daily.append(_tx(round(rng.uniform(20, 80), 2), d, debit=False, name=FRIEND,
                                 iban="RO00DEMO0000000000000007", text="my share"))
        if m.month == 7 and (d := on(12)):
            daily.append(_tx(1890.0, d, name="Booking.com"))
            daily.append(_tx(1460.0, d + timedelta(days=1), name="Wizz Air"))
        if m.month == 3 and (d := on(18)):
            salary.append(_tx(68500.0, d, name="Example Motors SRL", iban="RO00DEMO0000000000000013",
                              text="Car purchase"))
        if (d := on(19)) and rng.random() < 0.5:
            daily.append(_tx(round(rng.uniform(40, 200), 2), d, debit=False, name="eMAG", text="Refund from eMAG"))
        m = (m.replace(day=28) + timedelta(days=4)).replace(day=1)
    return {"demo-salary": salary, "demo-daily": daily, "demo-savings": savings}


def load(conn, months: int = 14) -> int:
    """Registers two made-up banks and stores the generated transactions and balances."""
    sync.register_session(conn, _session("Demo Bank", "demo-1", [("demo-salary", "CACC", "RO00DEMO0000000000000001")]))
    sync.register_session(conn, _session("Demo Card Bank", "demo-2", [("demo-daily", "CACC", "RO00DEMO0000000000000002"),
                                                                        ("demo-savings", "SVGS", None)]))
    data = generate(months)
    balances = {"demo-salary": 6830.15, "demo-daily": 1244.60, "demo-savings": 4500.00}
    names = {"demo-salary": "Salary", "demo-daily": "Everyday", "demo-savings": "Savings"}
    total = 0
    now = utcnow()
    for uid, txs in data.items():
        acc = conn.execute("SELECT id, currency FROM accounts WHERE uid=?", (uid,)).fetchone()
        with transaction(conn):
            total += sync._store_transactions(conn, acc[0], txs)
            conn.execute("UPDATE accounts SET backfilled_at=?, display_name=? WHERE id=?", (now, names[uid], acc[0]))
            conn.execute("INSERT INTO balance_snapshots(account_id, fetched_at, balance_type, amount_minor, currency)"
                         " VALUES (?,?,?,?,?)", (acc[0], now, "ITAV", to_minor(balances[uid]), "RON"))
    return total
