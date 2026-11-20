"""Leak scanner for the agent-facing layer."""

import re
import sqlite3
from dataclasses import dataclass

from .rules import Rules

PATTERNS = {
    "iban": re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b"),
    "card_number": re.compile(r"\b(?:\d[ -]?){13,19}\b"),
    "cnp": re.compile(r"\b[1-8]\d{12}\b"),
    "email": re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"),
    "phone": re.compile(r"(?:\+40|\b0)7\d{8}\b"),
}
AGENT_TABLES = ("transactions_clean", "recurring_series")


@dataclass
class Leak:
    table: str
    column: str
    kind: str


def _text_values(conn: sqlite3.Connection) -> list[tuple[str, str, str]]:
    out = []
    for table in AGENT_TABLES:
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})") if r[2].upper() == "TEXT"]
        for col in cols:
            for (value,) in conn.execute(f"SELECT DISTINCT {col} FROM {table} WHERE {col} IS NOT NULL"):
                out.append((table, col, value))
    return out


def scan(conn: sqlite3.Connection, rules: Rules) -> list[Leak]:
    """Returns every suspected leak. Reports where, never the leaked value."""
    leaks = []
    values = _text_values(conn)
    for table, col, value in values:
        for kind, pat in PATTERNS.items():
            if pat.search(value):
                leaks.append(Leak(table, col, kind))
    # Any raw counterparty name appearing verbatim, unless it is an allowlisted merchant name.
    allowed = {m.name.lower() for m in rules.merchants}
    names = {n.lower().strip() for (n,) in conn.execute(
        "SELECT DISTINCT counterparty_name FROM transactions_raw WHERE counterparty_name IS NOT NULL")}
    names = {n for n in names if len(n) >= 4}
    for table, col, value in values:
        v = value.lower()
        if v in allowed:
            continue
        if any(n in v for n in names):
            leaks.append(Leak(table, col, "counterparty_name"))
    return leaks
