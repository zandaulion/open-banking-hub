"""Runtime configuration.

Everything secret or personal lives in the data directory, never in the
repository: `$BANKHUB_DATA_DIR` (default `~/.local/share/bankhub`).
"""

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_DATA_DIR = Path.home() / ".local" / "share" / "bankhub"


def _read_env_file(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            env[key.strip()] = value.strip()
    return env


@dataclass(frozen=True)
class Config:
    data_dir: Path
    app_id: str | None
    key_path: Path
    redirect_url: str
    country: str
    one_off_ron: float = 50_000.0

    @property
    def db_path(self) -> Path:
        return self.data_dir / "bankhub.db"

    @property
    def rules_path(self) -> Path:
        """User rules override the packaged defaults when present."""
        return self.data_dir / "categories.yaml"


def load_config(data_dir: Path | None = None) -> Config:
    data_dir = Path(data_dir or os.environ.get("BANKHUB_DATA_DIR") or DEFAULT_DATA_DIR)
    env = {**_read_env_file(data_dir / ".env"), **os.environ}
    return Config(
        data_dir=data_dir,
        app_id=env.get("ENABLE_BANKING_APP_ID"),
        key_path=Path(env.get("BANKHUB_KEY_PATH") or data_dir / "secrets" / "enable_banking.pem"),
        redirect_url=env.get("BANKHUB_REDIRECT_URL", "https://enablebanking.com/"),
        country=env.get("BANKHUB_COUNTRY", "RO"),
        one_off_ron=float(env.get("BANKHUB_ONE_OFF_RON", "50000")),
    )
