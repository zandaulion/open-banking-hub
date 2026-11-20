# Security policy

Bank Hub holds a person's complete banking history and the key that lets it read their accounts.
Please report vulnerabilities privately, so that running installations can be fixed before details
become public.

## Reporting

- Preferred: GitHub **Security → Report a vulnerability** on this repository.
- Alternatively, contact the maintainer through the address on their GitHub profile.
- Do not open public issues for security problems, and do not test against installations you do not
  operate.

Include what you found, how to reproduce it and the impact you see. You will get an acknowledgement
within a few days, and a fix or mitigation plan as soon as one exists.

## Scope

- The web app and its API: device registration, invitations, cookies, the bank-linking callback.
- The privacy boundary: anything that lets an MCP client (an AI assistant) see names, IBANs, card
  numbers or payment descriptions.
- Data handling: sync, storage, rules files, backups and the off-site script.

Out of scope: Enable Banking, the banks, Tailscale, reverse proxies and the operating system themselves.

## What operators should know

- The app is meant for a private network only. Do not publish it on the internet.
- It can only read accounts (PSD2 AISP); it contains no payment code.
- Data at rest (SQLite, backups) is not encrypted by the application. Protect the data directory at the
  operating-system level; the off-site backup script encrypts with restic.
- The signing key in `secrets/` grants read access to every linked account while consents are valid.
  If it leaks, rotate the key in the Enable Banking Control Panel and revoke the consents in your banks'
  apps.
- Each registered phone keeps an offline copy of what the app shows, including payee names, in the
  browser's storage for the app (deleted after 14 days without reaching the server, and wiped when the
  server rejects the device). Anyone using the unlocked phone can open it. Revoke lost phones with
  `bankhub devices --revoke <id>`.
