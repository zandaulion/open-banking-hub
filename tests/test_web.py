from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bankhub import ledger, sync
from bankhub.config import Config
from bankhub.web.auth import AuthStore, normalize_code
from bankhub.web import rules_edit
from bankhub.web.data import suggest_pattern
from bankhub.web.server import WebConfig, create_admin_app, create_app

from conftest import session, store, tx

TOKEN = "a" * 64


@pytest.fixture
def cfg(tmp_path: Path, conn, rules):
    sync.register_session(conn, session("Revolut", [("rev-ron", "RON", "CACC")]))
    store(conn, "rev-ron", [tx("45.20", date.today().strftime("%Y-%m-02"), name="Lidl"),
                            tx("200", date.today().strftime("%Y-%m-03"), name="Jane Example",
                               iban="RO49AAAA1B31007593840000")])
    ledger.rebuild(conn, rules)
    core = Config(data_dir=tmp_path, app_id=None, key_path=tmp_path / "none.pem",
                  redirect_url="https://enablebanking.com/", country="RO")
    return WebConfig(core=core, admin_token=TOKEN, base_url="http://testserver", cookie_path="/",
                     port=0, admin_port=0)


def test_admin_requires_token_and_fails_closed(cfg):
    admin = TestClient(create_admin_app(cfg))
    assert admin.get("/api/admin/invites").status_code == 403
    assert admin.get("/api/admin/invites", headers={"X-Admin-Token": "b" * 64}).status_code == 403
    cfg.admin_token = None
    assert TestClient(create_admin_app(cfg)).get("/api/admin/invites",
                                                 headers={"X-Admin-Token": ""}).status_code == 403


def test_invite_registers_exactly_one_device(cfg):
    admin = TestClient(create_admin_app(cfg), headers={"X-Admin-Token": TOKEN})
    inv = admin.post("/api/admin/invites", json={"label": "Phone"}).json()
    assert inv["url"].endswith("/#invite=" + inv["code"])     # code in the fragment, not the path
    app = TestClient(create_app(cfg))
    assert app.get("/api/overview").status_code == 401
    r = app.post("/api/invites/redeem", json={"code": inv["code"].lower().replace("-", " ")})
    assert r.status_code == 200
    assert app.get("/api/overview").status_code == 200
    assert TestClient(create_app(cfg)).post("/api/invites/redeem", json={"code": inv["code"]}).status_code == 400
    devices = admin.get("/api/admin/devices").json()["devices"]
    assert [d["label"] for d in devices] == ["Phone"]
    admin.post(f"/api/admin/devices/{devices[0]['id']}/revoke", json={"revoked": True})
    assert app.get("/api/overview").status_code == 401


def test_revoked_invite_is_refused(cfg):
    admin = TestClient(create_admin_app(cfg), headers={"X-Admin-Token": TOKEN})
    inv = admin.post("/api/admin/invites", json={"label": "x"}).json()
    admin.post(f"/api/admin/invites/{inv['id']}/revoke")
    assert TestClient(create_app(cfg)).post("/api/invites/redeem", json={"code": inv["code"]}).status_code == 400
    assert admin.get("/api/admin/invites").json()["invites"][0]["revoked"] is True


def _device(cfg) -> TestClient:
    admin = TestClient(create_admin_app(cfg), headers={"X-Admin-Token": TOKEN})
    code = admin.post("/api/admin/invites", json={"label": "t"}).json()["code"]
    app = TestClient(create_app(cfg))
    app.post("/api/invites/redeem", json={"code": code})
    return app


def test_writes_need_the_custom_header(cfg):
    app = _device(cfg)
    body = {"pattern": "obscure shop", "category": "groceries"}
    assert app.post("/api/inbox/rule", json=body).status_code == 403
    assert app.post("/api/inbox/rule", json=body, headers={"X-Bankhub": "1"}).status_code == 200
    assert "obscure shop" in cfg.core.rules_path.read_text()


def test_owner_ui_shows_names_but_security_headers_are_set(cfg):
    app = _device(cfg)
    r = app.get("/api/transactions")
    assert "Jane Example" in r.text            # the owner sees their own data
    assert r.headers["cache-control"] == "no-store"
    assert "frame-ancestors 'none'" in app.get("/").headers["content-security-policy"]


def test_callback_rejects_unknown_state(cfg):
    app = _device(cfg)
    r = app.get("/callback?state=forged&code=x", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].endswith("link=invalid")


def test_shell_only_serves_known_files(cfg):
    app = TestClient(create_app(cfg))
    assert app.get("/app.js").status_code == 200
    assert app.get("/auth.py").status_code == 404
    assert app.get("/sw.js").text.count("__BUILD_VERSION__") == 0


def test_suggested_patterns_are_substrings():
    for raw in ["Ep*example.ro", "Ntp*bookshop", "Corner Steel C2"]:
        assert suggest_pattern(raw) in raw.lower()


def test_user_rule_file_is_private(tmp_path):
    p = tmp_path / "categories.yaml"
    rules_edit.add(p, patterns=["corner shop"], category="groceries", subcategory=None, name=None)
    assert oct(p.stat().st_mode & 0o777) == "0o600"
    assert normalize_code("abcd efgh-ijkl") == "ABCD-EFGH-IJKL"


def _ids(app):
    return {t["payee"]: t for t in app.get("/api/transactions").json()["transactions"]}


H = {"X-Bankhub": "1"}


def test_recategorize_payee_edits_one_rule_without_duplicates(cfg):
    app = _device(cfg)
    lidl = _ids(app)["Lidl"]
    r = app.post(f"/api/transactions/{lidl['id']}/category", json={"scope": "payee", "category": "household"}, headers=H)
    assert r.status_code == 200
    assert _ids(app)["Lidl"]["category"] == "household"
    # A second change for the same payee updates the same rule.
    app.post(f"/api/transactions/{lidl['id']}/category", json={"scope": "payee", "category": "groceries",
                                                               "subcategory": "discount"}, headers=H)
    rules = app.get("/api/rules").json()["rules"]
    assert len(rules) == 1 and rules[0]["name"] == "Lidl" and rules[0]["subcategory"] == "discount"
    assert rules[0]["matches"] == 1


def test_person_rule_stays_unnamed(cfg):
    app = _device(cfg)
    jane = _ids(app)["Jane Example"]
    app.post(f"/api/transactions/{jane['id']}/category", json={"scope": "payee", "category": "rent"}, headers=H)
    rule = app.get("/api/rules").json()["rules"][0]
    assert rule["named"] is False and rule["name"] is None
    import sqlite3
    c = sqlite3.connect(cfg.core.db_path)
    assert c.execute("SELECT merchant, category FROM transactions_clean WHERE id=?", (jane["id"],)).fetchone() \
        == ("unnamed_payee", "rent")


def test_one_payment_override_and_undo(cfg):
    app = _device(cfg)
    lidl = _ids(app)["Lidl"]
    app.post(f"/api/transactions/{lidl['id']}/category", json={"scope": "one", "category": "gifts"}, headers=H)
    t = _ids(app)["Lidl"]
    assert t["category"] == "gifts" and t["overridden"] == 1
    assert app.get("/api/rules").json()["rules"] == []
    app.delete(f"/api/transactions/{lidl['id']}/category", headers=H)
    assert _ids(app)["Lidl"]["category"] == "groceries"


def test_rule_edit_rename_delete_and_stale_etag(cfg):
    app = _device(cfg)
    app.post("/api/inbox/rule", json={"pattern": "jane example", "category": "rent"}, headers=H)
    rule = app.get("/api/rules").json()["rules"][0]
    body = {"etag": rule["etag"], "patterns": ["jane example"], "category": "housing", "subcategory": "rent"}
    assert app.put(f"/api/rules/{rule['id']}", json=body, headers=H).status_code == 200
    assert app.put(f"/api/rules/{rule['id']}", json=body, headers=H).status_code == 409   # stale etag
    fresh = app.get("/api/rules").json()["rules"][0]
    assert (fresh["category"], fresh["subcategory"]) == ("housing", "rent")
    assert app.delete(f"/api/rules/{fresh['id']}?etag={fresh['etag']}", headers=H).status_code == 200
    assert app.get("/api/rules").json()["rules"] == []
    assert app.put("/api/rules/merchants:0", json=body, headers=H).status_code == 409
    assert app.post("/api/inbox/rule", json={"pattern": "x", "category": "Bad Cat"}, headers=H).status_code == 400


def test_amount_search():
    from bankhub.web.data import amount_filter
    assert amount_filter("38.68") == ("abs(abs(c.amount) - ?) < 0.005", [38.68])
    assert amount_filter("38,68 lei")[1] == [38.68]
    assert amount_filter("38") == ("abs(c.amount) >= ? AND abs(c.amount) < ?", [38.0, 39.0])
    assert amount_filter(">500") == ("abs(c.amount) > ?", [500.0])
    assert amount_filter("200-100")[1] == [100.0, 200.0]
    assert amount_filter("Lidl") is None and amount_filter("5 to go") is None


def test_search_by_name_and_amount(cfg):
    app = _device(cfg)
    names = lambda q: [t["payee"] for t in app.get("/api/transactions", params={"q": q}).json()["transactions"]]
    assert names("lidl") == ["Lidl"]
    assert names("45.20") == ["Lidl"]
    assert names(">100") == ["Jane Example"]


def test_trend_is_fixed_while_the_selected_month_moves(cfg):
    app = _device(cfg)
    this = date.today().strftime("%Y-%m")
    a = app.get("/api/overview", params={"month": this}).json()["trend"]
    prev_month = (date.today().replace(day=1) - __import__("datetime").timedelta(days=1)).strftime("%Y-%m")
    b = app.get("/api/overview", params={"month": prev_month}).json()["trend"]
    assert [t["month"] for t in a] == [t["month"] for t in b]
    assert a[-1]["month"] == this and a[-1]["partial"] is True


def test_rename_account_shows_in_app_not_to_agents(cfg):
    app = _device(cfg)
    import sqlite3 as _sq
    _c = _sq.connect(cfg.core.db_path)
    _c.execute("INSERT INTO balance_snapshots(account_id, fetched_at, balance_type, amount_minor, currency)"
               " SELECT id, '2026-01-01', 'ITAV', 1000, 'RON' FROM accounts")
    _c.commit()
    assert app.post("/api/accounts/revolut_ron/name", json={"name": "Everyday"}, headers=H).status_code == 200
    accounts = app.get("/api/accounts").json()["balances"]["accounts"]
    assert any(a["name"] == "Everyday" and a["alias"] == "revolut_ron" for a in accounts)
    assert _ids(app)["Lidl"]["account_name"] == "Everyday"
    import sqlite3
    clean = sqlite3.connect(cfg.core.db_path).execute("SELECT DISTINCT account_alias FROM transactions_clean").fetchall()
    assert clean == [("revolut_ron",)]
    assert app.post("/api/accounts/nope/name", json={"name": "x"}, headers=H).status_code == 404


def test_app_start_applies_pending_migrations(tmp_path):
    """The app reads through read-only connections; starting it must bring the schema up to date."""
    import sqlite3 as _sq
    from bankhub.db import _migrations
    db = tmp_path / "bankhub.db"
    old = _sq.connect(db)
    for number, sql in _migrations():
        if number <= 5:
            old.executescript(sql)
    old.execute("PRAGMA user_version = 5")
    old.commit()
    old.close()
    core = Config(data_dir=tmp_path, app_id=None, key_path=tmp_path / "k.pem",
                  redirect_url="https://enablebanking.com/", country="RO")
    create_app(WebConfig(core=core, admin_token=TOKEN, base_url="http://testserver", cookie_path="/", port=0, admin_port=0))
    new = _sq.connect(db)
    cols = [r[1] for r in new.execute("PRAGMA table_info(accounts)")]
    assert {"display_name", "group_id", "source", "backfill_until"} <= set(cols)
    tables = {r[0] for r in new.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"account_groups", "card_statements", "card_plans", "statement_formats"} <= tables


def test_suggestions_keep_the_product():
    cases = {"Google*cloud Vcpv4x": "google*cloud", "Google Cloud V3nlhx": "google cloud",
             "Google *google Play Ap": "google *google play", "Google Google*one 01m": "google google*one",
             "Ep*example.ro": "example.ro", "Netflix*ab12": "netflix"}
    for raw, want in cases.items():
        assert suggest_pattern(raw) == want and want in raw.lower()
    assert rules_edit.rule_name("google *google play") == "Google Play"


def test_a_narrower_match_makes_its_own_rule_ahead_of_a_broad_one(cfg, conn, rules):
    store(conn, "rev-ron", [tx("120", date.today().strftime("%Y-%m-04"), name="Google*cloud Ab12cd"),
                            tx("30", date.today().strftime("%Y-%m-05"), name="Google Play Apps")])
    ledger.rebuild(conn, rules)
    app = _device(cfg)
    rules_edit.add(cfg.core.rules_path, patterns=["google"], category="subscriptions", subcategory=None, name="Google")
    cloud = next(t for t in app.get("/api/transactions").json()["transactions"] if "cloud" in (t["description"] or "").lower())
    info = app.get(f"/api/transactions/{cloud['id']}/payee_rule").json()
    assert info["pattern"] == "google*cloud" and info["existing"]["patterns"] == ["google"]
    assert info["preview"]["count"] == 1
    assert app.get("/api/rules/preview", params={"pattern": "google"}).json()["count"] == 2
    r = app.post(f"/api/transactions/{cloud['id']}/category", headers=H,
                 json={"scope": "payee", "category": "dev", "pattern": "google*cloud"})
    assert r.status_code == 200 and r.json()["rule"]["name"] == "Google Cloud"
    cats = {t["description"]: t["category"] for t in app.get("/api/transactions").json()["transactions"]}
    assert cats["Google*cloud Ab12cd"] == "dev" and cats["Google Play Apps"] == "subscriptions"
    names = [r["name"] for r in app.get("/api/rules").json()["rules"]]
    assert "Google Cloud" in names and "Google" in names              # the broad rule is untouched


def test_groups_create_edit_and_remove(cfg):
    app = _device(cfg)
    body = {"name": "Alex", "behaviour": "supported", "category": "kids", "subcategory": "allowance",
            "accounts": ["revolut_ron"]}
    r = app.post("/api/groups", json=body, headers=H)
    assert r.status_code == 200
    gid = r.json()["id"]
    d = app.get("/api/accounts").json()
    assert d["groups"][0]["name"] == "Alex" and d["groups"][0]["accounts"] == ["revolut_ron"]
    assert {t["kind"] for t in app.get("/api/transactions").json()["transactions"]} == {"dependent"}
    assert app.post("/api/groups", json=body, headers=H).status_code == 400            # same name
    assert app.post("/api/groups", json={**body, "name": "B", "behaviour": "shared"}, headers=H).status_code == 400
    assert app.post(f"/api/groups/{gid}", json={**body, "category": "Not Valid"}, headers=H).status_code == 400
    assert app.delete(f"/api/groups/{gid}").status_code == 403                          # needs the custom header
    assert app.delete(f"/api/groups/{gid}", headers=H).status_code == 200
    assert app.get("/api/accounts").json()["groups"] == []
    assert "dependent" not in {t["kind"] for t in app.get("/api/transactions").json()["transactions"]}


def test_activity_filters_by_account(cfg):
    app = _device(cfg)
    rows = app.get("/api/transactions", params={"account": "revolut_ron"}).json()
    assert rows["total"] == 2 and {t["account_alias"] for t in rows["transactions"]} == {"revolut_ron"}
    assert app.get("/api/transactions", params={"account": "nope"}).json()["total"] == 0


def test_group_payee_names_round_trip(cfg):
    app = _device(cfg)
    body = {"name": "Partner", "behaviour": "supported", "category": "family", "subcategory": "household",
            "accounts": [], "payees": ["Jane  Example", ""], "ignored": []}
    assert app.post("/api/groups", json=body, headers=H).status_code == 200
    g = app.get("/api/accounts").json()["groups"][0]
    assert g["payees"] == ["Jane Example"]
    assert [f["name"] for f in g["found"]] == ["Jane Example"]            # her transfer now counts for the group
    assert _ids(app)["Jane Example"]["category"] == "family"


def test_statement_upload_shows_a_card_and_refuses_junk(cfg, monkeypatch):
    from bankhub import statements
    from statement_fixture import statement
    monkeypatch.setattr(statements, "pdf_text", lambda data: statement("RO00TEST0000000000000001")
                        if data.startswith(b"%PDF-") else (_ for _ in ()).throw(statements.StatementError("That file is not a PDF.")))
    app = _device(cfg)
    junk = app.post("/api/statements", content=b"hello", headers={**H, "Content-Type": "application/pdf"})
    assert junk.status_code == 400 and "not a PDF" in junk.json()["detail"]
    r = app.post("/api/statements", content=b"%PDF-1.4 fake", headers={**H, "Content-Type": "application/pdf"})
    assert r.status_code == 200 and r.json()["new"] == 7 and r.json()["warnings"] == []
    assert app.post("/api/statements", content=b"%PDF-1.4 fake", headers={"Content-Type": "application/pdf"}).status_code == 403
    d = app.get("/api/accounts").json()
    card = d["cards"][0]
    assert (card["owed_ron"], card["limit_ron"], card["due_date"], len(card["plans"])) == (9849.84, 10000.0, "2026-11-19", 2)
    assert d["balances"]["card_owed_ron"] == 9849.84
    assert d["health"]["statements"][0]["period_end"] == "2026-10-25"


def test_csv_preview_and_import_remember_the_mapping(cfg):
    app = _device(cfg)
    csv = "Date,Description,Debit,Credit\n2026-10-01,Coffee,3.50,\n2026-10-02,Salary,,2000.00\n2026-10-03,Rent,800.00,\n"
    pre = app.post("/api/statements/csv/preview", json={"account": "revolut_ron", "csv": csv}, headers=H).json()
    assert pre["guess"]["debit"] == 2 and pre["guess"]["credit"] == 3 and pre["saved"] is None
    assert app.post("/api/statements/csv/preview", json={"account": "nope", "csv": csv}, headers=H).status_code == 404
    r = app.post("/api/statements/csv", json={"account": "revolut_ron", "csv": csv, "mapping": pre["guess"]}, headers=H)
    assert r.status_code == 200 and r.json()["new"] == 3 and r.json()["account"]
    again = app.post("/api/statements/csv", json={"account": "revolut_ron", "csv": csv, "mapping": pre["guess"]}, headers=H).json()
    assert again["new"] == 0 and again["already_had"] == 3
    assert app.post("/api/statements/csv/preview", json={"account": "revolut_ron", "csv": csv}, headers=H).json()["saved"]["debit"] == 2
    bad = app.post("/api/statements/csv", json={"account": "revolut_ron", "csv": csv, "mapping": {**pre["guess"], "date": None}}, headers=H)
    assert bad.status_code == 400
    assert app.post("/api/statements/csv", json={"account": "revolut_ron", "csv": csv, "mapping": pre["guess"]}).status_code == 403
