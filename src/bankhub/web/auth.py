"""Invites and registered devices, in their own database (web.db).

Implements the invite-console contract: invites are one-time codes that register one device;
the device then holds a random token in an HttpOnly cookie. Only token hashes are stored.
"""

import hashlib
import hmac
import secrets
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

INVITE_DAYS = 7
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"   # no 0/O, 1/I
SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    id         INTEGER PRIMARY KEY,
    label      TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    last_seen  TEXT,
    revoked    INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS invites (
    id         INTEGER PRIMARY KEY,
    label      TEXT NOT NULL,
    code       TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used_at    TEXT,
    revoked    INTEGER NOT NULL DEFAULT 0,
    device_id  INTEGER REFERENCES devices(id)
);
CREATE TABLE IF NOT EXISTS pending_links (
    state      TEXT PRIMARY KEY,
    bank       TEXT NOT NULL,
    device_id  INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
"""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_code() -> str:
    return "-".join("".join(secrets.choice(CODE_ALPHABET) for _ in range(4)) for _ in range(3))


def normalize_code(code: str) -> str:
    raw = "".join(ch for ch in code.upper() if ch.isalnum())
    return "-".join(raw[i:i + 4] for i in range(0, len(raw), 4))


class RedeemLimiter:
    """Global cap on failed redemptions: codes are ~60 bits, this stops online guessing outright."""

    def __init__(self, max_failures: int = 10, window_s: int = 600):
        self.max, self.window = max_failures, window_s
        self.failures: list[float] = []
        self.lock = threading.Lock()

    def blocked(self) -> bool:
        with self.lock:
            cutoff = time.monotonic() - self.window
            self.failures = [t for t in self.failures if t > cutoff]
            return len(self.failures) >= self.max

    def fail(self) -> None:
        with self.lock:
            self.failures.append(time.monotonic())


class AuthStore:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        new = not path.exists()
        with self._conn() as c:
            c.executescript(SCHEMA)
        if new:
            path.chmod(0o600)

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA busy_timeout = 5000")
        return c

    # --- invites ---------------------------------------------------------------

    def create_invite(self, label: str, base_url: str) -> dict:
        code = new_code()
        now = _now()
        with self._conn() as c:
            c.execute("INSERT INTO invites(label, code, created_at, expires_at) VALUES (?,?,?,?)",
                      (label or "Device", code, _iso(now), _iso(now + timedelta(days=INVITE_DAYS))))
            row = c.execute("SELECT * FROM invites WHERE code=?", (code,)).fetchone()
        return self._invite(row, base_url)

    def list_invites(self, base_url: str) -> list[dict]:
        with self._conn() as c:
            return [self._invite(r, base_url) for r in c.execute("SELECT * FROM invites ORDER BY id DESC")]

    def revoke_invite(self, invite_id: int) -> bool:
        with self._conn() as c:
            return c.execute("UPDATE invites SET revoked=1 WHERE id=?", (invite_id,)).rowcount > 0

    @staticmethod
    def _invite(r: sqlite3.Row, base_url: str) -> dict:
        # The code travels in the URL fragment, so it never reaches a server log.
        return {"id": r["id"], "label": r["label"], "code": r["code"],
                "url": f"{base_url.rstrip('/')}/#invite={r['code']}", "created_at": r["created_at"],
                "expires_at": r["expires_at"], "used_at": r["used_at"], "revoked": bool(r["revoked"]),
                "device_id": r["device_id"]}

    def redeem(self, code: str) -> tuple[str, dict] | None:
        """Returns (device token, device) or None. A code registers exactly one device."""
        code = normalize_code(code)
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            inv = c.execute("SELECT * FROM invites WHERE code=?", (code,)).fetchone()
            if (not inv or inv["revoked"] or inv["used_at"]
                    or datetime.fromisoformat(inv["expires_at"]) < _now()):
                c.execute("ROLLBACK")
                return None
            token = secrets.token_urlsafe(32)
            now = _iso(_now())
            dev_id = c.execute("INSERT INTO devices(label, token_hash, created_at, last_seen) VALUES (?,?,?,?)",
                               (inv["label"], hash_token(token), now, now)).lastrowid
            c.execute("UPDATE invites SET used_at=?, device_id=? WHERE id=?", (now, dev_id, inv["id"]))
            c.execute("COMMIT")
        return token, {"id": dev_id, "label": inv["label"]}

    # --- devices ---------------------------------------------------------------

    def device_for_token(self, token: str | None) -> dict | None:
        if not token:
            return None
        with self._conn() as c:
            r = c.execute("SELECT * FROM devices WHERE token_hash=?", (hash_token(token),)).fetchone()
            if not r or r["revoked"]:
                return None
            # Constant-time recheck of the matched hash (defence in depth; the lookup is by hash).
            if not hmac.compare_digest(r["token_hash"], hash_token(token)):
                return None
            now = _now()
            if not r["last_seen"] or now - datetime.fromisoformat(r["last_seen"]) > timedelta(minutes=5):
                c.execute("UPDATE devices SET last_seen=? WHERE id=?", (_iso(now), r["id"]))
            return {"id": r["id"], "label": r["label"]}

    def list_devices(self) -> list[dict]:
        with self._conn() as c:
            return [{"id": r["id"], "label": r["label"], "created_at": r["created_at"], "last_seen": r["last_seen"],
                     "revoked": bool(r["revoked"]), "has_push": False}
                    for r in c.execute("SELECT * FROM devices ORDER BY id DESC")]

    def set_device_revoked(self, device_id: int, revoked: bool) -> bool:
        with self._conn() as c:
            return c.execute("UPDATE devices SET revoked=? WHERE id=?", (int(revoked), device_id)).rowcount > 0

    def set_device_label(self, device_id: int, label: str) -> bool:
        with self._conn() as c:
            return c.execute("UPDATE devices SET label=? WHERE id=?", (label, device_id)).rowcount > 0

    def delete_device(self, device_id: int) -> bool:
        with self._conn() as c:
            c.execute("UPDATE invites SET device_id=NULL WHERE device_id=?", (device_id,))
            c.execute("DELETE FROM pending_links WHERE device_id=?", (device_id,))
            return c.execute("DELETE FROM devices WHERE id=?", (device_id,)).rowcount > 0

    # --- bank linking (consent callback) ----------------------------------------

    def add_pending_link(self, state: str, bank: str, device_id: int) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM pending_links WHERE created_at < ?", (_iso(_now() - timedelta(hours=1)),))
            c.execute("INSERT INTO pending_links(state, bank, device_id, created_at) VALUES (?,?,?,?)",
                      (state, bank, device_id, _iso(_now())))

    def take_pending_link(self, state: str) -> dict | None:
        with self._conn() as c:
            r = c.execute("SELECT * FROM pending_links WHERE state=? AND created_at >= ?",
                          (state, _iso(_now() - timedelta(hours=1)))).fetchone()
            c.execute("DELETE FROM pending_links WHERE state=?", (state,))
            return dict(r) if r else None
