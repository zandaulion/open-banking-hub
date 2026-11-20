---
name: deploy
description: Install, upgrade or verify a Bank Hub deployment on a Linux machine (systemd user services or Docker), publish it on a private network, register the owner's phone and link banks through Enable Banking. Use when asked to deploy, install, host, upgrade or move Bank Hub.
---

# Deploying Bank Hub

You are deploying an app that holds one person's complete banking history and the key that lets it read
their accounts. Treat the data directory, the signing key and any token as secrets, prefer the
repository's commands over hand-made equivalents, and stop to ask the human wherever this guide says so.

Read this whole file before running anything.

## How it is put together

- `bankhub web` serves the owner's app on `127.0.0.1:${WEB_PORT:-8041}`. With `ADMIN_TOKEN` set it also
  serves an admin API on `127.0.0.1:${ADMIN_PORT:-8141}`; leave it unset unless the human has a console.
- `bankhub sync` (twice a day) fetches transactions; `bankhub backup` (nightly) takes a snapshot.
- Everything personal is in the data directory, `$BANKHUB_DATA_DIR` (default `~/.local/share/bankhub`;
  `/data` in Docker): `.env`, `secrets/enable_banking.pem`, `bankhub.db`, `web.db`, `categories.yaml`,
  `backups/`. Never copy it into the repository, a ticket, a chat or logs.
- Migrations run automatically when the app starts.

## 1. Gather facts (ask the human)

1. The machine: an always-on Linux host? Docker available, or systemd user services?
2. The private network: Tailscale (recommended), or an existing private reverse proxy? If neither, stop:
   the app must not be published on the public internet.
3. The address the app will have, for example `https://<machine>.<tailnet>.ts.net:8446`.
4. Which banks, and in which country (`BANKHUB_COUNTRY`, default `RO`).

## 2. Install

systemd:

```bash
git clone https://github.com/zandaulion/open-banking-hub.git && cd open-banking-hub && uv sync
uv run bankhub init
```

Docker:

```bash
docker compose pull                     # the published image, amd64 or arm64
docker compose run --rm app bankhub init
```

`init` prints a public key. Hand it to the human for the next step.

## 3. Enable Banking (the human does this)

The human registers a **production** application in the Enable Banking Control Panel with that public
key, adds the redirect URLs `https://enablebanking.com/` and `<app address>/callback`, then uses
**Link accounts** for every bank. Point them at `docs/BANKS.md` (BRD in particular needs a specific
order). Ask for the application ID. It is not a secret on its own, but it never goes into the repository.

```bash
uv run bankhub init --app-id <id> --base-url <app address>        # or: docker compose run --rm app bankhub init ...
```

## 4. Run

systemd: copy `deploy/bankhub-web.service`, `bankhub-sync.*` and `bankhub-backup.*` to
`~/.config/systemd/user/`, replace every `/path/to/...`, `systemctl --user daemon-reload`,
`enable --now` the service and both timers, and `loginctl enable-linger "$USER"`.

Docker: `docker compose up -d`. The scheduler service runs sync and backup.

## 5. Publish privately

Tailscale: `tailscale serve --bg --https=8446 http://127.0.0.1:8041`. With Caddy, see `docs/DEPLOY.md`.
Do not add a public DNS name, a Cloudflare tunnel or a port forward.

## 6. Verify

- `curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8041/` returns 200.
- `curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8041/api/overview` returns 401 (no device yet).
- If `ADMIN_TOKEN` is unset, nothing listens on the admin port.

## 7. Register the phone and link banks

`uv run bankhub invite --label phone` (or `docker compose exec app bankhub invite --label phone`) prints
a link. The human opens it on their phone, on the private network, activates the device and adds the
app to the home screen. Then **Accounts → Link or renew a bank** for each bank: approval happens in the
bank's own app. The first import fetches the longest history available.

## 8. Backups

The nightly snapshot is local. For an off-site copy, follow the restic section of `docs/DEPLOY.md`. Ask
the human to store the restic password outside the machine.

## Upgrading

`git pull && uv sync && systemctl --user restart bankhub-web.service`, or
`docker compose pull && docker compose up -d`. Take a `bankhub backup` first. Migrations run on start;
an older release cannot be assumed to read a newer database, so roll back by restoring the snapshot.

## Never

- Put the app on the public internet, or proxy the admin port anywhere.
- Commit, paste or log the data directory, `.env`, the key, tokens or any transaction.
- Run `bankhub demo` against a data directory with real accounts. It refuses unless forced; don't force it.
