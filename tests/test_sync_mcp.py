import json
import sqlite3

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from bankhub import ledger, sync
from bankhub.db import connect_readonly
from bankhub.enablebanking import EnableBanking

from conftest import store, session, tx


@pytest.fixture
def key_path(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    p = tmp_path / "key.pem"
    p.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                    serialization.NoEncryption()))
    return p


def fake_bank(pages: list[list[dict]], calls: list):
    """Serves balances and paginated transactions like Enable Banking."""
    def handler(request: httpx.Request):
        calls.append(request)
        assert request.headers["authorization"].startswith("Bearer ")
        if request.url.path.endswith("/balances"):
            return httpx.Response(200, json={"balances": [
                {"balance_amount": {"amount": "10.00", "currency": "RON"}, "balance_type": "ITAV"}]})
        idx = int(request.url.params.get("continuation_key") or 0)
        body = {"transactions": pages[idx]}
        if idx + 1 < len(pages):
            body["continuation_key"] = str(idx + 1)
        return httpx.Response(200, json=body)
    return httpx.MockTransport(handler)


def test_backfill_paginates_and_is_idempotent(conn, key_path):
    sync.register_session(conn, session("Revolut", [("rev-ron", "RON", "CACC")]))
    pages = [[tx("1", "2026-10-01"), tx("2", "2026-10-02")], [tx("3", "2026-10-03")]]
    calls: list = []
    eb = EnableBanking("app", key_path, transport=fake_bank(pages, calls))
    acc = conn.execute("SELECT * FROM accounts").fetchone()
    r = sync.sync_account(conn, eb, acc, backfill=True)
    assert (r.fetched, r.new) == (3, 3)
    assert calls[1].url.params["strategy"] == "longest"
    r = sync.sync_account(conn, eb, acc, backfill=True)
    assert r.new == 0
    assert conn.execute("SELECT COUNT(*) FROM transactions_raw").fetchone()[0] == 3


def test_pending_is_replaced_when_it_settles(conn, key_path):
    sync.register_session(conn, session("Revolut", [("rev-ron", "RON", "CACC")]))
    acc = conn.execute("SELECT * FROM accounts").fetchone()
    first = [[tx("9.99", "2026-10-05", status="PDNG", ref="pending-ref")]]
    sync.sync_account(conn, EnableBanking("app", key_path, transport=fake_bank(first, [])), acc, backfill=True)
    settled = [[tx("10.49", "2026-10-06", ref="booked-ref")]]
    sync.sync_account(conn, EnableBanking("app", key_path, transport=fake_bank(settled, [])), acc,
                      backfill=False, force=True)
    rows = conn.execute("SELECT status, amount_minor FROM transactions_raw").fetchall()
    assert [tuple(r) for r in rows] == [("BOOK", -1049)]


def test_daily_budget_blocks_unattended_syncs(conn, key_path):
    sync.register_session(conn, session("Revolut", [("rev-ron", "RON", "CACC")]))
    acc = conn.execute("SELECT * FROM accounts").fetchone()
    eb = EnableBanking("app", key_path, transport=fake_bank([[]], []))
    results = [sync.sync_account(conn, eb, acc, backfill=False) for _ in range(sync.DAILY_SYNC_LIMIT + 1)]
    assert all(r.skipped is None for r in results[:-1])
    assert results[-1].skipped


def test_reconsent_keeps_alias_and_history(conn):
    sync.register_session(conn, session("Revolut", [("uid-old", "RON", "CACC")]))
    again = session("Revolut", [("uid-new", "RON", "CACC")], session_id="sess-9")
    again["accounts"][0]["identification_hash"] = "hash-uid-old"
    sync.register_session(conn, again)
    rows = conn.execute("SELECT uid, alias FROM accounts").fetchall()
    assert [tuple(r) for r in rows] == [("uid-new", "revolut_ron")]


def test_readonly_connection_cannot_write(conn, tmp_path):
    ro = connect_readonly(tmp_path / "bankhub.db")
    with pytest.raises(sqlite3.OperationalError):
        ro.execute("DELETE FROM transactions_clean")


def test_mcp_tools_answer_from_clean_layer(conn, rules, tmp_path):
    import asyncio
    from bankhub.mcp_server import build_server

    sync.register_session(conn, session("Revolut", [("rev-ron", "RON", "CACC")]))
    from conftest import store
    store(conn, "rev-ron", [tx("45.20", "2026-10-03", name="Lidl"),
                            tx("200", "2026-10-06", name="Jane Example", iban="RO49AAAA1B31007593840000")])
    ledger.rebuild(conn, rules)
    server = build_server(tmp_path / "bankhub.db")
    tools = {t.name for t in asyncio.run(server.list_tools())}
    assert tools == {"get_sync_status", "get_balances", "query_transactions", "get_spending_summary",
                     "get_recurring_payments"}
    result = asyncio.run(server.call_tool("query_transactions", {"date_from": "2026-10-01"}))
    text = json.dumps([getattr(c, "text", "") for c in result.content])
    assert "Lidl" in text and "individual" in text
    assert "Jane" not in text and "RO49" not in text


def test_windowed_bank_fetches_history_over_several_syncs(conn, key_path):
    """A bank that silently returns at most 20 transactions per request, no continuation key (like BRD,
    which keeps the oldest 20). It refuses ranges over a year. Every sync makes a few requests and
    keeps what it got, so the whole history arrives over several syncs."""
    from datetime import date as d, timedelta as td
    today = d.today()
    all_txs = [tx("10", (today - td(days=i * 3)).isoformat(), ref=f"r{i}") for i in range(240)]   # 720 days
    asked = []

    def handler(request: httpx.Request):
        if request.url.path.endswith("/balances"):
            return httpx.Response(200, json={"balances": []})
        a, b = request.url.params["date_from"], request.url.params["date_to"]
        assert (d.fromisoformat(b) - d.fromisoformat(a)).days <= 365
        asked.append(a)
        inside = sorted((t for t in all_txs if a <= t["booking_date"] <= b), key=lambda t: t["booking_date"])
        return httpx.Response(200, json={"transactions": inside[:20]})

    sync.register_session(conn, session("BRD", [("brd-ron", "RON", "CACC")]))
    acc = lambda: conn.execute("SELECT * FROM accounts").fetchone()
    count = lambda: conn.execute("SELECT count(*) FROM transactions_raw").fetchone()[0]
    eb = EnableBanking("app", key_path, transport=httpx.MockTransport(handler))
    sync.sync_account(conn, eb, acc(), backfill=True)
    assert len(asked) == sync.WINDOWED_REQUESTS_PER_RUN and count() > 0
    # A fresh link goes for old dates first (the bank only answers for them just after the consent).
    assert asked[0] == (today - td(days=sync.BACKFILL_DAYS)).isoformat()
    asked.clear()
    sync.sync_account(conn, eb, acc(), backfill=False, force=True)
    assert asked[0] >= (today - td(days=sync.RECENT_DAYS)).isoformat()    # afterwards, recent days first
    # Ordinary syncs never ask for dates the bank only answers right after a consent (it would send nothing).
    asked.clear()
    for _ in range(5):
        sync.sync_account(conn, eb, acc(), backfill=False, force=True)
    assert min(asked) >= (today - td(days=sync.RECENT_DAYS)).isoformat() and not sync.backfill_done(acc())
    # Each new link fetches more of the older history, until it is all there.
    for _ in range(30):
        if sync.backfill_done(acc()):
            break
        sync.sync_account(conn, eb, acc(), backfill=True)
    assert count() == 240 and sync.backfill_done(acc())


def test_unattended_sync_asks_for_90_days_at_most(conn, key_path):
    """A quiet account's last payment can be months old; banks refuse an older start without the customer."""
    from datetime import date as d, timedelta as td
    sync.register_session(conn, session("ING", [("ing-ron", "RON", "CACC")]))
    store(conn, "ing-ron", [tx("10", "2025-01-31")])
    starts = []

    def handler(request: httpx.Request):
        if not request.url.path.endswith("/balances"):
            starts.append(request.url.params.get("date_from"))
        return httpx.Response(200, json={"balances": [], "transactions": []})
    acc = conn.execute("SELECT * FROM accounts").fetchone()
    sync.sync_account(conn, EnableBanking("app", key_path, transport=httpx.MockTransport(handler)), acc,
                      backfill=False, force=True)
    assert starts == [(d.today() - td(days=sync.RECENT_DAYS)).isoformat()]


def test_rate_limit_skips_one_bank_without_failing_the_sync(conn, key_path):
    def handler(request):
        return httpx.Response(429, json={"error": "ASPSP_RATE_LIMIT_EXCEEDED"})
    sync.register_session(conn, session("BRD", [("brd-ron", "RON", "CACC")]))
    result = sync.sync_all(conn, EnableBanking("app", key_path, transport=httpx.MockTransport(handler)))
    assert result.accounts[0].skipped and "limit" in result.accounts[0].skipped


def test_backup_snapshot_is_consistent_and_rotated(conn, tmp_path):
    from bankhub import backup
    sync.register_session(conn, session("Revolut", [("rev-ron", "RON", "CACC")]))
    (tmp_path / "categories.yaml").write_text("merchants: []\n")
    snaps = []
    for _ in range(3):
        snaps.append(backup.snapshot(tmp_path, keep=2))
        import time; time.sleep(1.1)
    kept = sorted(p.name for p in (tmp_path / "backups").iterdir())
    assert kept == [snaps[1].name, snaps[2].name]
    copy = sqlite3.connect(snaps[-1] / "bankhub.db")
    assert copy.execute("SELECT count(*) FROM accounts").fetchone()[0] == 1
    assert oct((snaps[-1] / "bankhub.db").stat().st_mode & 0o777) == "0o600"
    assert backup.latest(tmp_path) == snaps[-1]


def test_windowed_bank_fetches_the_current_month_before_an_older_gap(conn, key_path):
    """A BRD account weeks behind: this month's payments come first, the gap before it after."""
    from datetime import date as d, timedelta as td
    today = d.today()
    all_txs = [tx("10", (today - td(days=i)).isoformat(), ref=f"g{i}") for i in range(0, 80)]   # one a day
    asked = []

    def handler(request: httpx.Request):
        if request.url.path.endswith("/balances"):
            return httpx.Response(200, json={"balances": []})
        a, b = request.url.params["date_from"], request.url.params["date_to"]
        asked.append(a)
        inside = sorted((t for t in all_txs if a <= t["booking_date"] <= b), key=lambda t: t["booking_date"])
        return httpx.Response(200, json={"transactions": inside[:20]})

    sync.register_session(conn, session("BRD", [("brd-ron", "RON", "CACC")]))
    gap_from = (today - td(days=60)).isoformat()
    conn.execute("UPDATE accounts SET backfilled_at='2026-01-01', history_from=?, recent_cursor=?, backfill_from=?, backfill_until=?",
                 (gap_from, gap_from, "2000-01-01", gap_from))
    acc = lambda: conn.execute("SELECT * FROM accounts").fetchone()
    eb = EnableBanking("app", key_path, transport=httpx.MockTransport(handler))
    sync.sync_account(conn, eb, acc(), backfill=False, force=True)
    month_start = today.replace(day=1) - td(days=sync.OVERLAP_DAYS)
    assert asked[0] == month_start.isoformat()                                   # the current month first
    have = {r[0] for r in conn.execute("SELECT booking_date FROM transactions_raw")}
    assert today.isoformat() in have or len([x for x in all_txs if x["booking_date"] >= month_start.isoformat()]) > 40
    for _ in range(10):
        sync.sync_account(conn, eb, acc(), backfill=False, force=True)
    assert acc()["recent_cursor"] is None and conn.execute("SELECT count(*) FROM transactions_raw").fetchone()[0] == 61


def test_a_rate_limit_after_some_progress_is_not_a_skipped_sync(conn, key_path):
    from datetime import date as d, timedelta as td
    today = d.today()
    calls = {"n": 0}

    def handler(request: httpx.Request):
        if request.url.path.endswith("/balances"):
            return httpx.Response(200, json={"balances": []})
        calls["n"] += 1
        if calls["n"] > 1:
            return httpx.Response(429, json={"error": "ASPSP_RATE_LIMIT_EXCEEDED"})
        return httpx.Response(200, json={"transactions": [tx("5", today.isoformat(), ref="now")]})

    sync.register_session(conn, session("BRD", [("brd-ron", "RON", "CACC")]))
    gap = (today - td(days=60)).isoformat()
    conn.execute("UPDATE accounts SET backfilled_at='2026-01-01', history_from=?, recent_cursor=?, backfill_from=?, backfill_until=?",
                 (gap, gap, "2000-01-01", gap))
    result = sync.sync_all(conn, EnableBanking("app", key_path, transport=httpx.MockTransport(handler)))
    assert result.accounts[0].skipped is None and result.accounts[0].new == 1
