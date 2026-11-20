"""SQLite access and migrations."""

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _migrations() -> list[tuple[int, str]]:
    files = resources.files("bankhub") / "migrations"
    out = []
    for f in files.iterdir():
        if f.name.endswith(".sql"):
            out.append((int(f.name.split("_", 1)[0]), f.read_text()))
    return sorted(out)


def connect(path: Path) -> sqlite3.Connection:
    """Read-write connection; applies pending migrations."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    new = not path.exists()
    conn = sqlite3.connect(path, isolation_level=None)  # explicit transactions only
    if new:
        path.chmod(0o600)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 15000")   # the timer, the CLI and the web app share the file
    conn.execute("PRAGMA journal_mode = WAL")
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    for number, sql in _migrations():
        if number > version:
            conn.executescript(f"BEGIN; {sql}; PRAGMA user_version = {number}; COMMIT;")
    return conn


def connect_readonly(path: Path) -> sqlite3.Connection:
    """Read-only connection: the database cannot be modified through it."""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 15000")
    conn.execute("PRAGMA query_only = ON")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection):
    try:
        conn.execute("BEGIN")
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
