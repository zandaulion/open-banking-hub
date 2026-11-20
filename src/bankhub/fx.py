"""Official BNR (National Bank of Romania) exchange rates, RON per unit."""

import sqlite3
import xml.etree.ElementTree as ET
from bisect import bisect_right
from datetime import date

import httpx

YEAR_URL = "https://curs.bnr.ro/files/xml/years/nbrfxrates{year}.xml"
NS = {"b": "https://www.bnr.ro/xsd"}


def parse_bnr_xml(xml: str) -> list[tuple[str, str, float]]:
    """Returns (date, currency, RON per 1 unit) rows."""
    root = ET.fromstring(xml)
    rows = []
    for cube in root.iterfind(".//b:Cube", NS):
        day = cube.get("date")
        for rate in cube.iterfind("b:Rate", NS):
            multiplier = int(rate.get("multiplier") or 1)
            rows.append((day, rate.get("currency"), float(rate.text) / multiplier))
    return rows


def fetch_years(conn: sqlite3.Connection, years: list[int], client: httpx.Client | None = None) -> int:
    own = client is None
    client = client or httpx.Client(timeout=60, follow_redirects=True)
    try:
        count = 0
        for year in years:
            r = client.get(YEAR_URL.format(year=year))
            r.raise_for_status()
            rows = parse_bnr_xml(r.text)
            conn.executemany("INSERT OR REPLACE INTO fx_rates(date, currency, rate_ron) VALUES (?,?,?)", rows)
            count += len(rows)
        return count
    finally:
        if own:
            client.close()


def ensure_rates(conn: sqlite3.Connection) -> int:
    """Fetches the years that have foreign-currency transactions; refreshes the current year only
    when transactions are newer than the newest rate held (BNR publishes once per business day)."""
    years_needed = {int(r[0]) for r in conn.execute(
        "SELECT DISTINCT substr(booking_date, 1, 4) FROM transactions_raw WHERE currency != 'RON'")}
    if not years_needed:
        return 0
    have = {int(r[0]) for r in conn.execute("SELECT DISTINCT substr(date, 1, 4) FROM fx_rates")}
    this_year = date.today().year
    newest_rate = conn.execute("SELECT MAX(date) FROM fx_rates").fetchone()[0] or ""
    newest_tx = conn.execute("SELECT MAX(booking_date) FROM transactions_raw").fetchone()[0] or ""
    years = sorted(y for y in years_needed | ({this_year} if newest_tx > newest_rate else set())
                   if y not in have or (y == this_year and newest_tx > newest_rate))
    return fetch_years(conn, years) if years else 0


class RateTable:
    """In-memory lookup: the rate for a day, or the last one published before it."""

    def __init__(self, conn: sqlite3.Connection):
        self._dates: dict[str, list[str]] = {}
        self._rates: dict[str, list[float]] = {}
        for cur, day, rate in conn.execute("SELECT currency, date, rate_ron FROM fx_rates ORDER BY currency, date"):
            self._dates.setdefault(cur, []).append(day)
            self._rates.setdefault(cur, []).append(rate)

    def to_ron(self, amount: float, currency: str, day: str) -> float | None:
        if currency == "RON":
            return amount
        dates = self._dates.get(currency)
        if not dates:
            return None
        i = bisect_right(dates, day) - 1
        if i < 0:
            i = 0  # before the first published rate: use the earliest one
        return amount * self._rates[currency][i]
