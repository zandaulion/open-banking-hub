"""Local, validated snapshots of everything needed to rebuild Bank Hub.

A snapshot is a directory under <data dir>/backups/<UTC timestamp>/ holding consistent copies of the
databases (SQLite online backup, safe while the app and the timer are writing) and the private files
(rules, settings, signing key). Each database copy is checked with PRAGMA integrity_check.
"""

import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DATABASES = ("bankhub.db", "web.db")
FILES = (".env", "categories.yaml", "secrets/enable_banking.pem")
KEEP = 14


def snapshot(data_dir: Path, keep: int = KEEP) -> Path:
    root = data_dir / "backups"
    root.mkdir(mode=0o700, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    tmp = root / f".{stamp}.partial"
    tmp.mkdir(mode=0o700)
    try:
        for name in DATABASES:
            src = data_dir / name
            if not src.exists():
                continue
            dst = tmp / name
            s = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
            d = sqlite3.connect(dst)
            try:
                s.backup(d)
                # One self-contained file: no -wal/-shm companions in the snapshot.
                d.execute("PRAGMA journal_mode = DELETE")
                result = d.execute("PRAGMA integrity_check").fetchone()[0]
            finally:
                d.close()
                s.close()
            if result != "ok":
                raise RuntimeError(f"{name}: integrity check failed ({result})")
            dst.chmod(0o600)
        for name in FILES:
            src = data_dir / name
            if src.exists():
                dst = tmp / name
                dst.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                shutil.copy2(src, dst)
                dst.chmod(0o600)
        final = root / stamp
        tmp.rename(final)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    # Keep the newest `keep` snapshots; only ever remove directories this function created.
    snaps = sorted(p for p in root.iterdir() if p.is_dir() and len(p.name) == 16 and p.name.endswith("Z"))
    for old in snaps[:-keep]:
        shutil.rmtree(old)
    return final


def latest(data_dir: Path) -> Path | None:
    root = data_dir / "backups"
    snaps = sorted(p for p in root.iterdir() if p.is_dir() and len(p.name) == 16 and p.name.endswith("Z")) \
        if root.exists() else []
    return snaps[-1] if snaps else None
