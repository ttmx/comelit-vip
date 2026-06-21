"""Automatic bootstrap and persistence of Comelit LAN ViP credentials."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

from ._paths import default_secrets_path
from .viper import ViperClient
from .web import DEFAULT_VIPER_PORT, PanelUser, PanelWebClient


def _panel_hostname(panel_host: str) -> str:
    hostname = urlparse(panel_host).hostname if "://" in panel_host else panel_host
    if not hostname:
        raise ValueError(f"invalid panel host: {panel_host!r}")
    return hostname


class ViperCredentials:
    def __init__(self, secrets_path: Path | str | None = None):
        self.path = Path(secrets_path) if secrets_path is not None else default_secrets_path()
        self.data = json.loads(self.path.read_text()) if self.path.is_file() else {}
        self.viper = self.data.setdefault("viper", {})
        self._installer: dict | None = None

    @classmethod
    def from_token(
        cls,
        panel_host: str,
        user_token: str,
        *,
        panel_port: int = DEFAULT_VIPER_PORT,
        source_address: str | None = None,
        entrance_address: str | None = None,
    ) -> "ViperCredentials":
        """Create non-persistent credentials from an explicit LAN token."""
        credentials = cls.__new__(cls)
        credentials.path = None
        credentials.data = {
            "viper": {
                "panel_host": _panel_hostname(panel_host),
                "panel_port": panel_port,
                "user_token": user_token,
            }
        }
        credentials.viper = credentials.data["viper"]
        if source_address:
            credentials.viper["source_address"] = source_address
        if entrance_address:
            credentials.viper["entrance_address"] = entrance_address
        credentials._installer = None
        return credentials

    @classmethod
    def from_installer(
        cls,
        panel_host: str,
        installer_password: str | None = None,
        *,
        cache_path: Path | str | None = None,
        ignore_cache: bool = False,
        web_port: int = 8080,
        panel_port: int = DEFAULT_VIPER_PORT,
        user_slot: int | None = None,
        description: str | None = None,
        web_client_factory: Callable[..., PanelWebClient] = PanelWebClient,
    ) -> "ViperCredentials":
        """Use matching cached credentials or retrieve and cache them locally."""
        credentials = cls(cache_path)
        host = _panel_hostname(panel_host)
        cache_matches = (
            credentials.viper.get("panel_host") == host
            and credentials.viper.get("panel_port", DEFAULT_VIPER_PORT) == panel_port
            and bool(credentials.viper.get("user_token"))
        )
        credentials._installer = {
            "panel_host": panel_host,
            "installer_password": installer_password,
            "web_port": web_port,
            "panel_port": panel_port,
            "user_slot": user_slot,
            "description": description,
            "web_client_factory": web_client_factory,
        }
        if ignore_cache or not cache_matches:
            credentials._refresh_from_installer()
        return credentials

    def _refresh_from_installer(self) -> PanelUser:
        if self._installer is None:
            raise RuntimeError("installer credentials are not configured")
        password = self._installer["installer_password"]
        if not password:
            raise RuntimeError(
                "installer password is required because no usable cached token exists"
            )
        return self.bootstrap_local(
            self._installer["panel_host"],
            password,
            web_port=self._installer["web_port"],
            panel_port=self._installer["panel_port"],
            user_slot=self._installer["user_slot"],
            description=self._installer["description"],
            web_client_factory=self._installer["web_client_factory"],
        )

    def bootstrap_local(
        self,
        panel_host: str,
        installer_password: str,
        *,
        web_port: int = 8080,
        panel_port: int = DEFAULT_VIPER_PORT,
        user_slot: int | None = None,
        description: str | None = None,
        web_client_factory: Callable[..., PanelWebClient] = PanelWebClient,
    ) -> PanelUser:
        """Load a persistent LAN token from the panel's installer backup.

        If several users exist, select one with ``user_slot`` or an exact
        ``description``. Without either selector, the first active user is used.
        """
        backup = web_client_factory(
            panel_host, installer_password, port=web_port
        ).fetch_config()
        users = backup.users
        selected = users[0]
        if user_slot is not None:
            selected = next((user for user in users if user.slot == user_slot), None)
            if selected is None:
                raise ValueError(f"no active panel user in slot {user_slot}")
        elif description is not None:
            selected = next(
                (user for user in users if user.description == description), None
            )
            if selected is None:
                raise ValueError(f"no active panel user named {description!r}")

        self.viper.update(
            {
                "panel_host": _panel_hostname(panel_host),
                "panel_port": panel_port,
                "user_token": selected.token,
            }
        )
        if selected.description:
            self.viper["description"] = selected.description
        if backup.apartment_address:
            self.viper["source_address"] = f"{backup.apartment_address}{selected.slot}"
        if backup.entrance_address:
            self.viper["entrance_address"] = backup.entrance_address
        self._save()
        return selected

    def ensure_connection_config(self) -> dict:
        """Return cached LAN configuration or require local bootstrap."""
        if not self.viper.get("panel_host"):
            raise RuntimeError(
                f"no LAN configuration in {self.path}; "
                "run `comelit bootstrap-local PANEL_IP`"
            )
        return self.viper

    def ensure_authenticated(self, client: ViperClient) -> dict:
        """Authenticate with the cached LAN token."""
        token = self.viper.get("user_token")
        if not token:
            raise RuntimeError(
                f"no LAN token in {self.path}; "
                "run `comelit bootstrap-local PANEL_IP`"
            )
        try:
            return client.authenticate(token)
        except PermissionError as exc:
            if self._installer is not None and self._installer["installer_password"]:
                self._refresh_from_installer()
                try:
                    return client.authenticate(self.viper["user_token"])
                except PermissionError as refreshed_exc:
                    raise PermissionError(
                        "LAN token retrieved from the installer UI was rejected"
                    ) from refreshed_exc
            raise PermissionError(
                "cached LAN token was rejected; rerun "
                "`comelit bootstrap-local PANEL_IP`"
            ) from exc

    def _save(self):
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2) + "\n")
        os.chmod(tmp, 0o600)
        tmp.replace(self.path)
