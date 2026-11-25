"""Sign-in for the remote MCP endpoint (OAuth 2.1, as Claude's custom connectors expect).

The protocol (client registration, /authorize, /token, revocation, checking the bearer token on /mcp) is
the MCP SDK's. This module is the storage behind it, and the one step that is Bank Hub's own: to approve a
connection, the owner types a short code that only the owner's app shows (Settings → AI assistant). There
are no passwords; a code works once, for ten minutes, and a few wrong tries void the request.

Everything secret is stored hashed: authorization codes, access and refresh tokens, pairing codes. The
owner's app can turn the endpoint off (every token stops working at once) and disconnect a client.
"""

import hashlib
import json
import secrets
import sqlite3
import time
from pathlib import Path

from mcp.server.auth.provider import (AccessToken, AuthorizationCode, AuthorizationParams, AuthorizeError,
                                      RefreshToken, RegistrationError, TokenError, construct_redirect_uri)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

SCOPE = "read"
ACCESS_SECONDS = 3600
REFRESH_SECONDS = 30 * 24 * 3600
CODE_SECONDS = 300
PAIRING_SECONDS = 600
REQUEST_SECONDS = 900
MAX_TRIES = 5
ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"   # no 0/O, 1/I
# Claude's connector callbacks (claude.com is announced as the future one). Others can be added in settings.
DEFAULT_REDIRECTS = ("https://claude.ai/api/mcp/auth_callback", "https://claude.com/api/mcp/auth_callback")

SCHEMA = """
CREATE TABLE IF NOT EXISTS mcp_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS mcp_clients (
    client_id  TEXT PRIMARY KEY,
    info       TEXT NOT NULL,
    created_at REAL NOT NULL,
    last_used  REAL
);
CREATE TABLE IF NOT EXISTS mcp_pairing (code_hash TEXT PRIMARY KEY, expires_at REAL NOT NULL, used INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS mcp_requests (
    id         TEXT PRIMARY KEY,
    client_id  TEXT NOT NULL,
    params     TEXT NOT NULL,
    expires_at REAL NOT NULL,
    tries      INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS mcp_codes (code_hash TEXT PRIMARY KEY, data TEXT NOT NULL, expires_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS mcp_tokens (
    token_hash TEXT PRIMARY KEY,
    kind       TEXT NOT NULL,              -- access | refresh
    client_id  TEXT NOT NULL,
    family     TEXT NOT NULL,              -- one sign-in: its access and refresh tokens, rotated together
    expires_at REAL NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS mcp_log (
    id        INTEGER PRIMARY KEY,
    at        REAL NOT NULL,
    client_id TEXT,
    tool      TEXT NOT NULL
);
"""


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class Store:
    """The connector's state, in the app's own `web.db`. Used by the endpoint and by the owner's app."""

    def __init__(self, path: Path, extra_redirects: tuple[str, ...] = ()):
        self.path = path
        self.redirects = set(DEFAULT_REDIRECTS) | set(extra_redirects)
        with self._conn() as c:
            c.executescript(SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA busy_timeout = 5000")
        return c

    # --- the owner's switches -------------------------------------------------------------

    def enabled(self) -> bool:
        with self._conn() as c:
            r = c.execute("SELECT value FROM mcp_settings WHERE key='enabled'").fetchone()
        return bool(r and json.loads(r[0]))

    def set_enabled(self, on: bool) -> None:
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO mcp_settings(key, value) VALUES ('enabled', ?)", (json.dumps(bool(on)),))

    def new_pairing_code(self) -> dict:
        code = "".join(secrets.choice(ALPHABET) for _ in range(8))
        expires = time.time() + PAIRING_SECONDS
        with self._conn() as c:
            c.execute("DELETE FROM mcp_pairing WHERE expires_at < ? OR used = 1", (time.time(),))
            c.execute("INSERT INTO mcp_pairing(code_hash, expires_at) VALUES (?, ?)", (_hash(code), expires))
        return {"code": f"{code[:4]}-{code[4:]}", "expires_at": expires}

    def clients(self) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                """SELECT c.client_id, c.info, c.created_at, c.last_used,
                          (SELECT count(*) FROM mcp_tokens t WHERE t.client_id = c.client_id AND t.kind = 'refresh'
                             AND t.expires_at > ?) AS signed_in
                   FROM mcp_clients c ORDER BY coalesce(c.last_used, c.created_at) DESC""", (time.time(),)).fetchall()
        return [{"client_id": r["client_id"], "name": json.loads(r["info"]).get("client_name") or "Unnamed client",
                 "created_at": r["created_at"], "last_used": r["last_used"], "connected": r["signed_in"] > 0} for r in rows]

    def disconnect(self, client_id: str) -> bool:
        with self._conn() as c:
            c.execute("DELETE FROM mcp_tokens WHERE client_id = ?", (client_id,))
            return c.execute("DELETE FROM mcp_clients WHERE client_id = ?", (client_id,)).rowcount > 0

    def log(self, client_id: str | None, tool: str) -> None:
        with self._conn() as c:
            c.execute("INSERT INTO mcp_log(at, client_id, tool) VALUES (?, ?, ?)", (time.time(), client_id, tool))
            c.execute("UPDATE mcp_clients SET last_used = ? WHERE client_id = ?", (time.time(), client_id))
            c.execute("DELETE FROM mcp_log WHERE id <= (SELECT max(id) - 1000 FROM mcp_log)")

    def recent_calls(self, limit: int = 30) -> list[dict]:
        with self._conn() as c:
            return [dict(r) for r in c.execute(
                """SELECT l.at, l.tool, coalesce(json_extract(c.info, '$.client_name'), 'Unknown') AS client
                   FROM mcp_log l LEFT JOIN mcp_clients c ON c.client_id = l.client_id
                   ORDER BY l.id DESC LIMIT ?""", (limit,))]

    # --- the approval page ----------------------------------------------------------------

    def pending(self, request_id: str) -> dict | None:
        with self._conn() as c:
            r = c.execute("SELECT * FROM mcp_requests WHERE id = ? AND expires_at > ?", (request_id, time.time())).fetchone()
        return dict(r) if r else None

    def approve(self, request_id: str, pairing_code: str) -> str:
        """Checks the code the owner typed. Returns the URL to send the browser back to Claude with, or
        raises ValueError with a message for the page."""
        if not self.enabled():
            raise ValueError("The AI assistant connection is turned off in Bank Hub.")
        req = self.pending(request_id)
        if not req or req["tries"] >= MAX_TRIES:
            raise ValueError("This request has expired. Start again from Claude.")
        code = "".join(ch for ch in pairing_code.upper() if ch in ALPHABET)
        with self._conn() as c:
            ok = c.execute("UPDATE mcp_pairing SET used = 1 WHERE code_hash = ? AND used = 0 AND expires_at > ?",
                           (_hash(code), time.time())).rowcount == 1
            if not ok:
                c.execute("UPDATE mcp_requests SET tries = tries + 1 WHERE id = ?", (request_id,))
                left = MAX_TRIES - req["tries"] - 1
                raise ValueError(f"That code is not right, or has expired. {left} tr{'ies' if left != 1 else 'y'} left."
                                 if left > 0 else "Too many wrong codes. Start again from Claude.")
            c.execute("DELETE FROM mcp_requests WHERE id = ?", (request_id,))
            params = json.loads(req["params"])
            auth_code = secrets.token_urlsafe(32)
            data = {"code": auth_code, "client_id": req["client_id"], "scopes": params["scopes"] or [SCOPE],
                    "code_challenge": params["code_challenge"], "redirect_uri": params["redirect_uri"],
                    "redirect_uri_provided_explicitly": params["redirect_uri_provided_explicitly"],
                    "resource": params.get("resource"), "expires_at": time.time() + CODE_SECONDS, "subject": "owner"}
            c.execute("INSERT INTO mcp_codes(code_hash, data, expires_at) VALUES (?, ?, ?)",
                      (_hash(auth_code), json.dumps(data), data["expires_at"]))
        return construct_redirect_uri(params["redirect_uri"], code=auth_code, state=params.get("state"))


class Provider:
    """The SDK's OAuthAuthorizationServerProvider, backed by `Store`."""

    def __init__(self, store: Store, connect_url: str, resource: str | None = None):
        # Tokens are only ever issued for this one endpoint, so each carries its address as the resource.
        self.store, self.connect_url, self.resource = store, connect_url, resource

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        with self.store._conn() as c:
            r = c.execute("SELECT info FROM mcp_clients WHERE client_id = ?", (client_id,)).fetchone()
        return OAuthClientInformationFull.model_validate_json(r[0]) if r else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        # Only Claude's own callbacks (and any the owner added): a stranger's client could not receive a code.
        uris = {str(u) for u in (client_info.redirect_uris or [])}
        if not uris or not uris <= self.store.redirects:
            raise RegistrationError("invalid_redirect_uri", "Only Claude's connector callback is accepted.")
        with self.store._conn() as c:
            c.execute("DELETE FROM mcp_clients WHERE last_used IS NULL AND created_at < ?", (time.time() - 86400,))
            c.execute("INSERT OR REPLACE INTO mcp_clients(client_id, info, created_at) VALUES (?, ?, ?)",
                      (client_info.client_id, client_info.model_dump_json(), time.time()))

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        if not self.store.enabled():
            raise AuthorizeError("access_denied", "The AI assistant connection is turned off in Bank Hub.")
        request_id = secrets.token_urlsafe(24)
        with self.store._conn() as c:
            c.execute("DELETE FROM mcp_requests WHERE expires_at < ?", (time.time(),))
            c.execute("INSERT INTO mcp_requests(id, client_id, params, expires_at) VALUES (?, ?, ?, ?)",
                      (request_id, client.client_id, params.model_dump_json(), time.time() + REQUEST_SECONDS))
        return f"{self.connect_url}?request={request_id}"

    async def load_authorization_code(self, client: OAuthClientInformationFull, authorization_code: str):
        with self.store._conn() as c:
            r = c.execute("SELECT data FROM mcp_codes WHERE code_hash = ? AND expires_at > ?",
                          (_hash(authorization_code), time.time())).fetchone()
        if not r:
            return None
        code = AuthorizationCode.model_validate_json(r[0])
        return code if code.client_id == client.client_id else None

    def _issue(self, c, client_id: str, scopes: list[str], family: str, resource: str | None) -> OAuthToken:
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        now = time.time()
        c.executemany("INSERT INTO mcp_tokens(token_hash, kind, client_id, family, expires_at, created_at) VALUES (?,?,?,?,?,?)",
                      [(_hash(access), "access", client_id, family, now + ACCESS_SECONDS, now),
                       (_hash(refresh), "refresh", client_id, family, now + REFRESH_SECONDS, now)])
        c.execute("DELETE FROM mcp_tokens WHERE expires_at < ?", (now,))
        return OAuthToken(access_token=access, token_type="Bearer", expires_in=ACCESS_SECONDS,
                          refresh_token=refresh, scope=" ".join(scopes))

    async def exchange_authorization_code(self, client: OAuthClientInformationFull, authorization_code) -> OAuthToken:
        with self.store._conn() as c:
            if c.execute("DELETE FROM mcp_codes WHERE code_hash = ?", (_hash(authorization_code.code),)).rowcount != 1:
                raise TokenError("invalid_grant", "The authorization code was already used.")
            return self._issue(c, client.client_id, authorization_code.scopes, secrets.token_hex(8), authorization_code.resource)

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str):
        with self.store._conn() as c:
            r = c.execute("SELECT * FROM mcp_tokens WHERE token_hash = ? AND kind = 'refresh' AND expires_at > ?",
                          (_hash(refresh_token), time.time())).fetchone()
        if not r or r["client_id"] != client.client_id or not self.store.enabled():
            return None
        return RefreshToken(token=refresh_token, client_id=r["client_id"], scopes=[SCOPE], expires_at=int(r["expires_at"]),
                            subject="owner")

    async def exchange_refresh_token(self, client: OAuthClientInformationFull, refresh_token, scopes: list[str]) -> OAuthToken:
        with self.store._conn() as c:
            row = c.execute("SELECT family FROM mcp_tokens WHERE token_hash = ?", (_hash(refresh_token.token),)).fetchone()
            if not row:
                raise TokenError("invalid_grant", "The refresh token is no longer valid.")
            c.execute("DELETE FROM mcp_tokens WHERE family = ?", (row["family"],))     # rotate: old ones stop working
            return self._issue(c, client.client_id, [SCOPE], row["family"], refresh_token.resource)

    async def load_access_token(self, token: str) -> AccessToken | None:
        if not self.store.enabled():
            return None
        with self.store._conn() as c:
            r = c.execute("SELECT * FROM mcp_tokens WHERE token_hash = ? AND kind = 'access' AND expires_at > ?",
                          (_hash(token), time.time())).fetchone()
        if not r:
            return None
        return AccessToken(token=token, client_id=r["client_id"], scopes=[SCOPE], expires_at=int(r["expires_at"]),
                           resource=self.resource, subject="owner")

    async def revoke_token(self, token) -> None:
        with self.store._conn() as c:
            row = c.execute("SELECT family FROM mcp_tokens WHERE token_hash = ?", (_hash(token.token),)).fetchone()
            if row:
                c.execute("DELETE FROM mcp_tokens WHERE family = ?", (row["family"],))
