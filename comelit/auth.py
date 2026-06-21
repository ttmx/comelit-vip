"""OAuth2 (Authorization Code + PKCE) token management for the Comelit cloud.

The interactive login happens in a webview; we don't reproduce it here. Instead we
bootstrap from a captured ``refresh_token`` (see work/captures/flows/*_token.resp.txt)
and keep it fresh. Comelit ROTATES the refresh token on every refresh, so we persist
the new one immediately to avoid invalidating our session.

Token endpoint:  POST https://api.comelitgroup.com/o-auth-2/token
  grant_type=refresh_token & refresh_token=... & client_id=... & scope=all
Response: {access_token, token_type:bearer, refresh_token, expires_in (7d), scope}
"""
from __future__ import annotations
import json, time, threading
from pathlib import Path
import requests

from ._paths import default_secrets_path

TOKEN_URL = "https://api.comelitgroup.com/o-auth-2/token"
CLIENT_ID = "kgDV0WRlQcSF4jPsz887lOTPyVVtP7Oh"
REDIRECT_URI = "https://app.comelitgroup.com/oauth_redirect/comelit"


class Auth:
    """Holds tokens, refreshes on demand, and persists rotation to a JSON file."""

    def __init__(self, secrets_path: Path | str | None = None):
        self.path = Path(secrets_path) if secrets_path is not None else default_secrets_path()
        self._lock = threading.Lock()
        self._data = json.loads(self.path.read_text())
        # owner ids needed for the data-store API
        self.owner_auth_id = self._data["ownerAuthId"]
        self.owner_uuid = self._data.get("ownerUuid")

    # --- token lifecycle -------------------------------------------------
    def _expired(self) -> bool:
        return time.time() >= self._data.get("expires_at", 0) - 120  # 2-min skew

    def refresh(self) -> str:
        r = requests.post(TOKEN_URL, data={
            "grant_type": "refresh_token",
            "refresh_token": self._data["refresh_token"],
            "client_id": CLIENT_ID,
            "scope": "all",
        }, headers={"user-agent": "ktor-client", "accept": "application/json"}, timeout=20)
        r.raise_for_status()
        tok = r.json()
        self._data["access_token"] = tok["access_token"]
        if tok.get("refresh_token"):
            self._data["refresh_token"] = tok["refresh_token"]   # rotation!
        self._data["expires_at"] = time.time() + int(tok.get("expires_in", 604800))
        self._save()
        return self._data["access_token"]

    @property
    def bearer(self) -> str:
        with self._lock:
            if "access_token" not in self._data or self._expired():
                return self.refresh()
            return self._data["access_token"]

    def _save(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, indent=2))
        tmp.replace(self.path)
