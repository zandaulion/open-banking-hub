# Deploying Bank Hub

Bank Hub is two loopback listeners (the app, and an optional admin API) plus a few timers. All
state lives in the data directory (`$BANKHUB_DATA_DIR`, default `~/.local/share/bankhub`):

| Path | What |
|---|---|
| `.env` | settings (see [`.env.example`](../.env.example)) |
| `secrets/enable_banking.pem` | the key your Enable Banking application trusts |
| `bankhub.db` | accounts, transactions, the redacted ledger |
| `web.db` | invites and registered devices |
| `categories.yaml` | your own categorization rules |
| `backups/` | nightly snapshots |

Reading credit card statements (PDF) needs `pdftotext`: `apt install poppler-utils` (or
`dnf install poppler-utils`). The Docker image has it. Without it everything else works.

## Keep it private

The app shows your real transactions, with names, to every device you register. Reach it only
over a private network, never directly from the internet. Inside that network, each device still
needs a one-time invitation, and holds a random token in an HttpOnly cookie (only its hash is
stored). The pages use a strict Content-Security-Policy, and every write needs a custom header
that other websites cannot send.

### Tailscale Serve (recommended)

Give the app its own HTTPS port at the root:

```bash
tailscale serve --bg --https=8446 http://127.0.0.1:8041
```

```
PUBLIC_BASE_URL=https://<machine>.<tailnet>.ts.net:8446
BANKHUB_REDIRECT_URL=https://<machine>.<tailnet>.ts.net:8446/callback
```

Serve's addresses are reachable only from your tailnet. Add the `/callback` address as a redirect
URL in the Enable Banking Control Panel: your bank sends the browser back there after approval.
The phone is on your tailnet, so it can follow it; Enable Banking itself never needs to reach it.

### Caddy under a sub-path

If you already run Caddy on a private listener, mount the app under a path and strip it:

```caddy
redir /bank /bank/ 308
handle_path /bank/* {
	reverse_proxy 127.0.0.1:8041
}
```

```
PUBLIC_BASE_URL=https://<private host>/bank
```

The cookie is scoped to `/bank` automatically (from `PUBLIC_BASE_URL`). With SELinux, Caddy may
need the port labelled: `semanage port -a -t http_port_t -p tcp 8041`.

## Optional: AI on your phone

Claude's custom connectors are called from Anthropic's servers, not from your phone, so a private
network cannot reach them. `bankhub mcp-http` is a separate, small endpoint for this: it serves only
the read-only, redacted MCP tools (the same ones as `bankhub mcp`), never the app, and only to a client
you approved with a one-time code from the app.

How it is protected:

- **Off until you turn it on**, in Settings → AI assistant. Turning it off stops every token at once.
- **OAuth 2.1 with PKCE.** Clients register themselves, but only with Claude's own callback addresses
  (`BANKHUB_MCP_REDIRECTS` adds others). Approving needs a pairing code from the app: eight characters,
  valid once for ten minutes, and five wrong tries void the request.
- **Short-lived tokens**, stored only as hashes: one hour for access, thirty days for refresh, rotated
  on every use. Settings lists connected clients (Disconnect removes one) and the last tools called.
- **Redacted data only.** It opens `bankhub.db` read-only and reads the redacted tables: no names of
  people, IBANs, card numbers or payment descriptions. Check with `bankhub inspect-redaction --samples 0`.

Setting it up:

1. Pick a public hostname for the endpoint alone, for example `bank-mcp.example.com`, and put it in the
   data directory's `.env`:

   ```
   BANKHUB_MCP_URL=https://bank-mcp.example.com
   # BANKHUB_MCP_PORT=8241
   ```

2. Run it: copy [`deploy/bankhub-mcp.service`](../deploy/bankhub-mcp.service) to
   `~/.config/systemd/user/`, fix the paths, then `systemctl --user enable --now bankhub-mcp.service`.
   It listens on `127.0.0.1` only.
3. Publish **only this port** at that hostname over HTTPS: a Cloudflare Tunnel, or a reverse proxy
   with a certificate. Keep the app and the admin API private as before. If the proxy is on the same
   machine, the endpoint trusts `Cf-Connecting-Ip` only from loopback.
4. In Bank Hub: Settings → AI assistant → *Allow connections*.
5. In Claude (web or the app): Settings → Connectors → *Add custom connector*, with the address the
   card shows (`https://bank-mcp.example.com/mcp`). Press *Connect*; a Bank Hub page asks for a code.
   Get it from Settings → AI assistant → *Get a pairing code*, type it, and Claude is connected. The
   connector then works from the Claude app on your phone.

Optional: `BANKHUB_MCP_ALLOWED_NETS` (comma-separated networks) limits who can call `/mcp` and `/token`
to those networks; the approval page stays reachable for your browser. Only set it after checking in the
proxy's log where Claude's calls really come from.

## Optional: admin API for an external console

Invitations and devices are managed with `bankhub invite` and `bankhub devices`. If you run a
console that manages several apps, set `ADMIN_TOKEN` (64 hex characters) and the app starts a
second listener on `ADMIN_PORT` with:

- `GET/POST /api/admin/invites`, `POST /api/admin/invites/{id}/revoke`
- `GET /api/admin/devices`, `POST /api/admin/devices/{id}/revoke` `{revoked}`,
  `POST /api/admin/devices/{id}/label` `{label}`, `DELETE /api/admin/devices/{id}`

Every request needs `X-Admin-Token`. Have your private proxy add that header, and make sure no
public route can reach the admin port.

## Timers

| Unit | When | What |
|---|---|---|
| `bankhub-sync.timer` | 06:47 and 18:47 | new transactions and balances, then a rebuild |
| `bankhub-backup.timer` | 03:23 | local snapshot (`bankhub backup`) |
| `bankhub-offsite-backup.timer` | 03:53 | optional: encrypted copy with restic |
| `bankhub-offsite-maintain.timer` | Sundays | optional: thin out old copies, verify part of the repository |

Copy the units from `deploy/` to `~/.config/systemd/user/`, replace the `/path/to/...`
placeholders, then `systemctl --user daemon-reload` and `enable --now` the timers you want.
`loginctl enable-linger "$USER"` keeps them running while you are logged out.

## Off-site backups with restic

`deploy/offsite-backup.sh` uploads the newest snapshot to a restic repository over SFTP.

1. On the backup machine, allow a dedicated key to do SFTP only, in `~/.ssh/authorized_keys`:
   `restrict,command="internal-sftp -d /home/<user>" ssh-ed25519 AAAA... bankhub backup`.
2. On the Bank Hub machine, create a private settings directory (e.g. `~/.config/bankhub`) with
   that key, an SSH config entry for the backup host, a random restic password file, and
   `offsite.env`:

   ```
   BANKHUB_DATA_DIR=/home/<user>/.local/share/bankhub
   RESTIC_REPOSITORY=sftp:<ssh host alias>:Backups/bankhub
   RESTIC_PASSWORD_FILE=/home/<user>/.config/bankhub/restic-password
   RESTIC_SSH_CONFIG=/home/<user>/.config/bankhub/ssh_config
   RESTIC_HOST=bankhub
   ```

3. `deploy/offsite-backup.sh init`, then `backup`, then `restore-test` (restores to a temporary
   directory and checks both databases).

Store the restic password outside this machine: without it the copy cannot be opened.

## Upgrading

```bash
git pull && uv sync
systemctl --user restart bankhub-web.service
# or, with Docker Compose:
docker compose pull && docker compose up -d
```

Database migrations run automatically. After a change to a bank parser,
`bankhub renormalize` re-reads stored transactions without contacting the bank.
