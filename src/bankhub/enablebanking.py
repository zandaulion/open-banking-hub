"""Minimal Enable Banking API client (account information only).

Only GET data endpoints plus the consent calls (/auth, /sessions) exist here:
there is deliberately no payment code path.
"""

import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

import httpx
import jwt

API_URL = "https://api.enablebanking.com"


class EnableBankingError(RuntimeError):
    def __init__(self, status: int, method: str, path: str, body: str):
        # Error bodies describe the request, never account data.
        super().__init__(f"{method} {path} -> {status}: {body[:300]}")
        self.status = status


class EnableBanking:
    def __init__(self, app_id: str, key_path: Path, base_url: str = API_URL,
                 transport: httpx.BaseTransport | None = None):
        if not app_id:
            raise ValueError("ENABLE_BANKING_APP_ID is not configured")
        self.app_id = app_id
        self._key = Path(key_path).read_bytes()
        self._http = httpx.Client(base_url=base_url, timeout=60, transport=transport)
        self.data_calls = 0       # transaction requests made (what the daily limit counts)
        self.balance_calls = 0

    def close(self) -> None:
        self._http.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _token(self) -> str:
        now = int(time.time())
        return jwt.encode(
            {"iss": "enablebanking.com", "aud": "api.enablebanking.com", "iat": now, "exp": now + 3600},
            self._key, algorithm="RS256", headers={"kid": self.app_id},
        )

    def _call(self, method: str, path: str, **kw) -> dict:
        r = self._http.request(method, path, headers={"Authorization": f"Bearer {self._token()}"}, **kw)
        if r.status_code >= 400:
            raise EnableBankingError(r.status_code, method, path, r.text)
        return r.json()

    # --- application and consent -------------------------------------------------

    def application(self) -> dict:
        return self._call("GET", "/application")

    def aspsps(self, country: str) -> list[dict]:
        return self._call("GET", "/aspsps", params={"country": country, "psu_type": "personal"})["aspsps"]

    def start_auth(self, aspsp: dict, country: str, redirect_url: str,
                   state: str | None = None) -> tuple[str, str, datetime]:
        """Returns (consent url, state, valid_until). Requests the longest consent the bank allows."""
        max_seconds = aspsp.get("maximum_consent_validity") or 90 * 86400
        valid_until = datetime.now(timezone.utc) + timedelta(seconds=max_seconds - 3600)
        state = state or str(uuid.uuid4())
        resp = self._call("POST", "/auth", json={
            "access": {"valid_until": valid_until.isoformat()},
            "aspsp": {"name": aspsp["name"], "country": country},
            "state": state,
            "redirect_url": redirect_url,
            "psu_type": "personal",
        })
        return resp["url"], state, valid_until

    def create_session(self, code: str) -> dict:
        return self._call("POST", "/sessions", json={"code": code})

    def get_session(self, session_id: str) -> dict:
        return self._call("GET", f"/sessions/{session_id}")

    # --- data ----------------------------------------------------------------------

    def balances(self, account_uid: str) -> list[dict]:
        self.balance_calls += 1
        return self._call("GET", f"/accounts/{account_uid}/balances").get("balances", [])

    def transactions(self, account_uid: str, date_from: str | None = None,
                     longest: bool = False, date_to: str | None = None) -> Iterator[dict]:
        """Yields all transactions, following continuation keys.

        `longest` asks the bank for the maximum history (use right after consent).
        """
        params: dict[str, str] = {}
        if longest:
            params["strategy"] = "longest"
        elif date_from:
            params["date_from"] = date_from
        if date_to and not longest:
            params["date_to"] = date_to
        continuation = None
        while True:
            p = dict(params)
            if continuation:
                p["continuation_key"] = continuation
            self.data_calls += 1
            page = self._call("GET", f"/accounts/{account_uid}/transactions", params=p)
            yield from page.get("transactions", [])
            continuation = page.get("continuation_key")
            if not continuation:
                return
