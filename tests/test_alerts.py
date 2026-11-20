import base64
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
