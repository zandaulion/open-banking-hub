import base64
import hashlib
import json
import secrets
from urllib.parse import parse_qs, urlsplit

import pytest
from starlette.testclient import TestClient

from bankhub import ledger, mcp_auth, sync
from bankhub.mcp_remote import create_app

from conftest import session, store, tx

URL = "http://localhost"
CALLBACK = "https://claude.ai/api/mcp/auth_callback"
MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


@pytest.fixture
def remote(tmp_path, conn, rules):
    sync.register_session(conn, session("Revolut", [("rev-ron", "RON", "CACC")]))
    store(conn, "rev-ron", [tx("45.20", "2026-10-03", name="Lidl"), tx("200", "2026-10-06", name="Jane Example",
                                                                          iban="RO49AAAA1B31007593840000")])
    ledger.rebuild(conn, rules)
    app = create_app(tmp_path / "bankhub.db", tmp_path / "web.db", URL)
    st = mcp_auth.Store(tmp_path / "web.db")
    with TestClient(app, base_url=URL) as client:
        yield client, st


def _pkce():
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def _connect(client, st):
    """The whole sign-in, as Claude does it, with the owner typing the code from the app."""
    reg = client.post("/register", json={"redirect_uris": [CALLBACK], "client_name": "Claude",
                                         "token_endpoint_auth_method": "none", "grant_types": ["authorization_code", "refresh_token"]})
    assert reg.status_code == 201, reg.text
    cid = reg.json()["client_id"]
    verifier, challenge = _pkce()
    r = client.get("/authorize", params={"response_type": "code", "client_id": cid, "redirect_uri": CALLBACK, "state": "s1",
                                         "code_challenge": challenge, "code_challenge_method": "S256", "scope": "read"},
                   follow_redirects=False)
    assert r.status_code == 302 and "/connect?request=" in r.headers["location"]
    rid = parse_qs(urlsplit(r.headers["location"]).query)["request"][0]
    assert "Connect Claude" in client.get(f"/connect?request={rid}").text
    assert client.post("/connect", data={"request": rid, "code": "WRONG-CODE"}).status_code == 400
    code = st.new_pairing_code()["code"]
    back = client.post("/connect", data={"request": rid, "code": code.lower()}, follow_redirects=False)
    assert back.status_code == 302 and back.headers["location"].startswith(CALLBACK)
    q = parse_qs(urlsplit(back.headers["location"]).query)
    assert q["state"] == ["s1"]
    tok = client.post("/token", data={"grant_type": "authorization_code", "code": q["code"][0], "redirect_uri": CALLBACK,
                                      "client_id": cid, "code_verifier": verifier})
    assert tok.status_code == 200, tok.text
    # A code works once.
    assert client.post("/token", data={"grant_type": "authorization_code", "code": q["code"][0], "redirect_uri": CALLBACK,
                                       "client_id": cid, "code_verifier": verifier}).status_code == 400
    return cid, tok.json()


def _call(client, token, method="tools/list", params=None):
    body = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}}
    h = {**MCP_HEADERS, "Authorization": f"Bearer {token}"}
    r = client.post("/mcp", json=body, headers=h)
    if r.status_code != 200:
        return r
    sid = r.headers.get("mcp-session-id")
    if sid:
        h["mcp-session-id"] = sid
    client.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=h)
    return client.post("/mcp", json={"jsonrpc": "2.0", "id": 2, "method": method, "params": params or {}}, headers=h)


def test_nothing_works_until_the_owner_turns_it_on(remote):
    client, st = remote
    reg = client.post("/register", json={"redirect_uris": [CALLBACK], "token_endpoint_auth_method": "none"}).json()
    _, challenge = _pkce()
    r = client.get("/authorize", params={"response_type": "code", "client_id": reg["client_id"], "redirect_uri": CALLBACK,
                                         "code_challenge": challenge, "code_challenge_method": "S256", "state": "x"},
                   follow_redirects=False)
    assert "error=access_denied" in r.headers.get("location", "")
    assert client.post("/mcp", json={}, headers=MCP_HEADERS).status_code == 401


def test_only_claudes_callback_can_register(remote):
    client, _ = remote
    assert client.post("/register", json={"redirect_uris": ["https://evil.example/cb"],
                                          "token_endpoint_auth_method": "none"}).status_code == 400


def test_sign_in_call_refresh_and_turn_off(remote):
    client, st = remote
    st.set_enabled(True)
    cid, tok = _connect(client, st)
    r = _call(client, tok["access_token"])
    assert r.status_code == 200
    assert "get_week_summary" in r.text and "get_net_worth" in r.text
    out = _call(client, tok["access_token"], "tools/call", {"name": "query_transactions", "arguments": {"date_from": "2026-10-01"}})
    assert "Lidl" in out.text and "Jane" not in out.text and "RO49" not in out.text     # redacted
    assert [c["tool"] for c in st.recent_calls()] == ["query_transactions"]
    assert st.clients()[0]["name"] == "Claude" and st.clients()[0]["connected"]
    # The bearer is required, and must be valid.
    assert client.post("/mcp", json={}, headers=MCP_HEADERS).status_code == 401
    assert "resource_metadata" in client.post("/mcp", json={}, headers=MCP_HEADERS).headers.get("www-authenticate", "")
    # Refreshing rotates both tokens: the old ones stop working.
    new = client.post("/token", data={"grant_type": "refresh_token", "refresh_token": tok["refresh_token"], "client_id": cid})
    assert new.status_code == 200
    assert _call(client, tok["access_token"]).status_code == 401
    assert client.post("/token", data={"grant_type": "refresh_token", "refresh_token": tok["refresh_token"],
                                       "client_id": cid}).status_code == 400
    assert _call(client, new.json()["access_token"]).status_code == 200
    # Turning it off stops every token at once; disconnecting removes the client.
    st.set_enabled(False)
    assert _call(client, new.json()["access_token"]).status_code == 401
    st.set_enabled(True)
    assert st.disconnect(cid) and _call(client, new.json()["access_token"]).status_code == 401


def test_pairing_codes_run_out_after_a_few_wrong_tries(remote):
    client, st = remote
    st.set_enabled(True)
    reg = client.post("/register", json={"redirect_uris": [CALLBACK], "token_endpoint_auth_method": "none"}).json()
    _, challenge = _pkce()
    loc = client.get("/authorize", params={"response_type": "code", "client_id": reg["client_id"], "redirect_uri": CALLBACK,
                                           "code_challenge": challenge, "code_challenge_method": "S256", "state": "x"},
                     follow_redirects=False).headers["location"]
    rid = parse_qs(urlsplit(loc).query)["request"][0]
    for _ in range(mcp_auth.MAX_TRIES):
        client.post("/connect", data={"request": rid, "code": "AAAA-AAAA"})
    good = st.new_pairing_code()["code"]
    assert client.post("/connect", data={"request": rid, "code": good}).status_code == 400   # the request is void


def test_the_network_limit_guards_the_api_but_not_the_approval_page(tmp_path, conn):
    app = create_app(tmp_path / "bankhub.db", tmp_path / "web.db", URL, allowed_nets="160.79.104.0/21")
    with TestClient(app, base_url=URL) as client:
        assert client.post("/mcp", json={}, headers=MCP_HEADERS).status_code == 403
        assert client.post("/token", data={}).status_code == 403
        assert client.get("/connect?request=x").status_code == 400          # reachable (and the link is unknown)
