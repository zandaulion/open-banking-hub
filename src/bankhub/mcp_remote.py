"""The MCP server over HTTPS, for Claude on the phone and on the web (custom connector).

Same read-only, redacted tools as `bankhub mcp`, served at /mcp behind OAuth 2.1 (see mcp_auth). It runs as
its own process on loopback; a reverse proxy publishes it on its own hostname, separate from the owner's app,
which stays private. The owner turns it on, connects Claude with a one-time code and sees every call in
Settings → AI assistant.

Settings (in the data directory's .env):
  BANKHUB_MCP_URL       the public base URL, e.g. https://bank-mcp.example.com (required)
  BANKHUB_MCP_PORT      loopback port, default 8241
  BANKHUB_MCP_ALLOWED_NETS  optional: only these client networks may call /mcp and /token (comma separated)
  BANKHUB_MCP_REDIRECTS optional: callback URLs to accept besides Claude's own
"""

import html
import ipaddress
import os
from urllib.parse import urlsplit

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from . import mcp_auth
from .config import load_config
from .mcp_server import build_server

PAGE_HEADERS = {"Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'",
                "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer", "Cache-Control": "no-store",
                "X-Frame-Options": "DENY"}


def _page(body: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Connect Claude to Bank Hub</title>
<style>body{{font:16px/1.5 system-ui,sans-serif;max-width:420px;margin:40px auto;padding:0 18px;color:#111}}
input{{font:22px ui-monospace,monospace;letter-spacing:.15em;text-transform:uppercase;width:100%;box-sizing:border-box;padding:10px;border:1px solid #bbb;border-radius:10px}}
button{{margin-top:14px;font:inherit;padding:10px 18px;border:0;border-radius:99px;background:#1d4f91;color:#fff}}
.muted{{color:#555;font-size:14px}} .err{{color:#b3261e}}</style></head><body>{body}</body></html>""",
                        status_code=status, headers=PAGE_HEADERS)


def create_app(db_path=None, web_db=None, public_url: str | None = None, allowed_nets: str | None = None,
               extra_redirects: tuple[str, ...] = ()):
    core = load_config() if (db_path is None or web_db is None) else None
    db_path = db_path or core.db_path
    web_db = web_db or core.data_dir / "web.db"
    public_url = (public_url or os.environ.get("BANKHUB_MCP_URL", "")).rstrip("/")
    if not public_url:
        raise SystemExit("Set BANKHUB_MCP_URL to the public address of the MCP endpoint first (see docs/DEPLOY.md).")
    store = mcp_auth.Store(web_db, extra_redirects)
    provider = mcp_auth.Provider(store, f"{public_url}/connect", f"{public_url}/mcp")
    auth = AuthSettings(issuer_url=public_url, resource_server_url=f"{public_url}/mcp", required_scopes=[mcp_auth.SCOPE], validate_token_resource=True,
                        client_registration_options=ClientRegistrationOptions(enabled=True, valid_scopes=[mcp_auth.SCOPE],
                                                                              default_scopes=[mcp_auth.SCOPE]),
                        revocation_options=RevocationOptions(enabled=True))

    def on_call(tool: str) -> None:
        token = get_access_token()
        store.log(token.client_id if token else None, tool)

    server = build_server(db_path, auth_server_provider=provider, auth=auth, on_call=on_call)

    @server.custom_route("/connect", methods=["GET", "POST"])
    async def connect(request: Request) -> Response:
        if request.method == "GET":
            rid = request.query_params.get("request", "")
            if not store.pending(rid):
                return _page("<h1>Link expired</h1><p>Start again from Claude: Settings → Connectors.</p>", 400)
            return _page(f"""<h1>Connect Claude to Bank Hub</h1>
<p>Claude asks to read your banking data: <strong>read-only</strong> and <strong>redacted</strong> (no names,
IBANs or payment descriptions).</p>
<p>Open Bank Hub → Settings → <strong>AI assistant</strong> → <strong>Get a pairing code</strong>, and type the code it shows.</p>
<form method="post"><input type="hidden" name="request" value="{html.escape(rid)}">
<input name="code" autocomplete="one-time-code" autocapitalize="characters" placeholder="XXXX-XXXX" required maxlength="12">
<button type="submit">Allow read-only access</button></form>
<p class="muted">Not you? Close this page: nothing is shared without the code.</p>""")
        form = await request.form()
        rid = str(form.get("request", ""))
        try:
            target = store.approve(rid, str(form.get("code", "")))
        except ValueError as e:
            again = f"""<form method="post"><input type="hidden" name="request" value="{html.escape(rid)}">
<input name="code" autocapitalize="characters" placeholder="XXXX-XXXX" required maxlength="12">
<button type="submit">Try again</button></form>""" if store.pending(rid) else ""
            return _page(f"<h1>Not connected</h1><p class='err'>{html.escape(str(e))}</p>{again}", 400)
        return RedirectResponse(target, status_code=302, headers={"Cache-Control": "no-store"})

    host = urlsplit(public_url).netloc
    port = os.environ.get("BANKHUB_MCP_PORT", "8241")
    app = server.streamable_http_app(transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True, allowed_hosts=[host, f"127.0.0.1:{port}", f"localhost:{port}"],
        allowed_origins=[public_url, "https://claude.ai", "https://claude.com"]))
    nets = [ipaddress.ip_network(n.strip()) for n in (allowed_nets or os.environ.get("BANKHUB_MCP_ALLOWED_NETS", "")).split(",") if n.strip()]
    return _only_from(app, nets) if nets else app


def _only_from(app, nets):
    """Lets only these networks call /mcp and /token. The visitor's address is taken from the tunnel's
    Cf-Connecting-Ip header, and only when the request comes from loopback (the proxy); the approval page stays
    open to any address, since the owner opens it on the phone."""
    async def guarded(scope, receive, send):
        if scope["type"] == "http" and (scope["path"].startswith("/mcp") or scope["path"] in ("/token", "/register")):
            headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
            peer = (scope.get("client") or ("", 0))[0]
            ip = headers.get("cf-connecting-ip") if peer in ("127.0.0.1", "::1") else peer
            try:
                ok = ip and any(ipaddress.ip_address(ip) in n for n in nets)
            except ValueError:
                ok = False
            if not ok:
                await Response("Forbidden", status_code=403)(scope, receive, send)
                return
        await app(scope, receive, send)
    return guarded


def serve() -> None:
    import uvicorn
    from .config import DEFAULT_DATA_DIR, _read_env_file
    from pathlib import Path
    data_dir = Path(os.environ.get("BANKHUB_DATA_DIR") or DEFAULT_DATA_DIR)
    for k, v in _read_env_file(data_dir / ".env").items():      # the same .env as the rest of Bank Hub
        os.environ.setdefault(k, v)
    extra = tuple(u.strip() for u in os.environ.get("BANKHUB_MCP_REDIRECTS", "").split(",") if u.strip())
    uvicorn.run(create_app(extra_redirects=extra), host="127.0.0.1", port=int(os.environ.get("BANKHUB_MCP_PORT", "8241")),
                proxy_headers=False, access_log=False, log_level="warning")
