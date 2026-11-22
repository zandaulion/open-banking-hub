import base64
from pathlib import Path
import json
from datetime import date, timedelta

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from bankhub import ledger, sync
from bankhub.web.alerts import Alerts, validate_subscription
from bankhub.web.auth import AuthStore

from conftest import session, store, tx


def _sub():
    key = ec.generate_private_key(ec.SECP256R1()).public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    b64 = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=").decode()
    return {"endpoint": "https://fcm.googleapis.com/fcm/send/abc", "keys": {"p256dh": b64(key), "auth": b64(b"0" * 16)}}


@pytest.fixture
def setup(tmp_path, conn, rules, monkeypatch):
    sent = []
    import pywebpush

    def fake_webpush(subscription_info, data, **kw):
        sent.append(json.loads(data))
    monkeypatch.setattr(pywebpush, "webpush", fake_webpush)
    auth = AuthStore(tmp_path / "web.db")
    inv = auth.create_invite("phone", "http://x")
    _, dev = auth.redeem(inv["code"])
    al = Alerts(tmp_path / "web.db", tmp_path)
    sync.register_session(conn, session("Revolut", [("rev-ron", "RON", "CACC")]))
    return al, dev, sent


def test_large_payment_alert_is_sent_once_and_private_by_default(setup, conn, rules):
    al, dev, sent = setup
    al.subscribe(dev["id"], _sub())                     # alerts start now
    today = date.today().isoformat()
    store(conn, "rev-ron", [tx("2500", today, name="eMAG"), tx("40", today, name="Lidl")])
    ledger.rebuild(conn, rules)
    first = al.run(conn)
    assert [a.title for a in first] == ["Large payment"]
    assert sent[-1]["body"] == "A payment above your alert amount went out."     # no amount, no payee
    assert al.run(conn) == []                           # not sent twice
    al.save_settings({"details": True, "large_payment_ron": 30})
    al.run(conn)
    assert "40 RON to Lidl" in sent[-1]["body"]


def test_old_transactions_do_not_alert(setup, conn, rules):
    al, dev, sent = setup
    store(conn, "rev-ron", [tx("9000", date.today().isoformat(), name="eMAG")])   # before subscribing
    ledger.rebuild(conn, rules)
    al.subscribe(dev["id"], _sub())
    assert [a for a in al.run(conn) if a.key.startswith("pay:")] == []


def test_balance_and_consent_alerts(setup, conn):
    al, dev, sent = setup
    al.subscribe(dev["id"], _sub())
    acc = conn.execute("SELECT id FROM accounts").fetchone()[0]
    conn.execute("INSERT INTO balance_snapshots(account_id, fetched_at, balance_type, amount_minor, currency)"
                 " VALUES (?, '2026-01-01', 'ITAV', 30000, 'RON')", (acc,))
    conn.execute("UPDATE consents SET valid_until=?", ((date.today() + timedelta(days=5)).isoformat() + "T00:00:00Z",))
    al.save_settings({"balance_below_ron": 500})
    titles = {a.title for a in al.run(conn)}
    assert {"Balance is low", "Renew Revolut"} <= titles


def test_nothing_is_logged_without_a_subscribed_phone(setup, conn, rules):
    al, dev, sent = setup
    conn.execute("UPDATE consents SET valid_until=?", ((date.today() + timedelta(days=2)).isoformat() + "T00:00:00Z",))
    assert al.run(conn) and not sent
    al.subscribe(dev["id"], _sub())
    assert al.run(conn) and sent                        # still delivered once a phone subscribes


def test_only_real_push_services_are_accepted():
    good = _sub()
    assert validate_subscription(good)["endpoint"].startswith("https://fcm.googleapis.com/")
    with pytest.raises(ValueError):
        validate_subscription({**good, "endpoint": "https://attacker.example/push"})
    with pytest.raises(ValueError):
        validate_subscription({**good, "keys": {"p256dh": "x", "auth": "y"}})


def test_a_gone_subscription_is_removed(setup, conn, monkeypatch):
    al, dev, sent = setup
    al.subscribe(dev["id"], _sub())
    import pywebpush

    class Resp:
        status_code = 410

    def gone(*a, **k):
        raise pywebpush.WebPushException("gone", response=Resp())
    monkeypatch.setattr(pywebpush, "webpush", gone)
    assert al.test(dev["id"]) == 0
    assert al.status(dev["id"])["subscribed"] is False


def test_card_payment_alert_waits_for_the_due_date_and_stops_once_paid(conn):
    from bankhub import statements
    from bankhub.web.alerts import DEFAULTS, evaluate
    from statement_fixture import CARD_IBAN, statement
    sync.register_session(conn, session("BRD", [("main", "RON", "CACC")]))
    statements.store_statement(conn, statements.parse_brd_card(statement("RO00TEST0000000000000001")))
    due = date(2026, 11, 19)
    keys = lambda today: [a.key for a in evaluate(conn, dict(DEFAULTS), Path("/nonexistent"), today) if a.key.startswith("card:")]
    assert keys(due - timedelta(days=5)) == []
    assert keys(due - timedelta(days=3)) == [f"card:brd_card:{due}"]
    assert keys(due) == [f"card:brd_card:{due}"] and keys(due + timedelta(days=1)) == []
    store(conn, "main", [tx("400", "2026-11-17", name="Bank", iban=CARD_IBAN)])          # the minimum (350.28) left
    assert keys(due - timedelta(days=1)) == []


def _history(conn, rules):
    """Eight weeks of about 300 RON of groceries, then a week with a lot more, a double charge and a new payee."""
    from datetime import date as d
    weeks = [d(2026, 11, 16) - timedelta(days=7 * i) for i in range(1, 9)]
    rows = [tx("100", (w + timedelta(days=k)).isoformat(), name="Lidl") for w in weeks for k in (0, 2, 4)]
    rows += [tx("40", (w + timedelta(days=1)).isoformat(), name="Example Bakery") for w in weeks]
    rows += [tx("100", "2026-11-16", name="Lidl"), tx("450", "2026-11-18", name="Lidl"), tx("300", "2026-11-19", name="Lidl"),
             tx("64.99", "2026-11-20", name="Example Shop"), tx("64.99", "2026-11-20", name="Example Shop"),
             tx("900", "2026-11-21", name="Example Furniture SRL")]
    store(conn, "rev-ron", rows)
    ledger.rebuild(conn, rules, today=d(2026, 11, 23))


def test_weekly_summary_compares_with_the_usual_week(setup, conn, rules):
    from bankhub.web import weekly
    _history(conn, rules)
    s = weekly.summary(conn, date(2026, 11, 23))                      # a Monday: covers 16 to 22 November
    # The bakery every week is a regular payment, so the usual day-to-day week is the 300 of groceries.
    assert (s["start"], s["end"], s["usual_ron"]) == ("2026-11-16", "2026-11-22", 300.0)
    assert s["spent_ron"] == round(100 + 450 + 300 + 64.99 * 2 + 900, 2)
    kinds = {(x["kind"], x["payee"]) for x in s["worth_a_look"]}
    assert ("twice", "Example Shop") in kinds and ("new", "Example Furniture SRL") in kinds and ("bigger", "Lidl") in kinds
    assert any(c["category"] == "groceries" for c in s["categories_up"])
    detailed, private = weekly.notification(s)
    assert "above usual" in detailed and private == "Your weekly summary is ready."
    assert "Lidl" not in detailed                                    # payees stay inside the app


def test_weekly_summary_is_sent_once_at_the_chosen_time(setup, conn, rules):
    from datetime import datetime
    al, dev, sent = setup
    _history(conn, rules)
    assert al.weekly(conn, now=datetime(2026, 11, 23, 9, 0)) is None          # no phone subscribed yet
    al.subscribe(dev["id"], _sub())
    assert al.weekly(conn, now=datetime(2026, 11, 23, 9, 0))["end"] == "2026-11-22"
    assert sent[-1]["body"] == "Your weekly summary is ready." and sent[-1]["url"] == "./#month?week=1"
    assert al.weekly(conn, now=datetime(2026, 11, 23, 10, 0)) is None         # once
    assert al.weekly(conn, now=datetime(2026, 11, 30, 7, 59)) is None         # before 08:00
    assert al.weekly(conn, now=datetime(2026, 12, 1, 9, 0)) is None           # missed by more than 20 hours
    al.save_settings({"weekly_day": 4, "weekly_hour": 18})                    # Friday 18:00
    assert al.weekly(conn, now=datetime(2026, 11, 27, 18, 5))["start"] == "2026-11-20"
    al.save_settings({"weekly": False})
    assert al.weekly(conn, now=datetime(2026, 12, 4, 18, 5)) is None


def test_weekly_settings_are_kept_in_range(setup):
    al, _, _ = setup
    s = al.save_settings({"weekly_day": 9, "weekly_hour": -3})
    assert (s["weekly_day"], s["weekly_hour"]) == (6, 0)
    assert al.save_settings({"weekly_day": 2.7})["weekly_day"] == 2


def test_a_week_with_a_silent_busy_account_says_it_may_be_incomplete(setup, conn, rules):
    from bankhub.web import weekly
    _history(conn, rules)                       # payments every week, the last on the 21st: complete
    assert weekly.summary(conn, date(2026, 11, 23))["incomplete"] == []
    s = weekly.summary(conn, date(2026, 11, 30))                      # nothing at all from the 22nd on
    assert [x["account"] for x in s["incomplete"]] == ["revolut_ron"]
    assert "may not be complete" in weekly.notification(s)[0]
