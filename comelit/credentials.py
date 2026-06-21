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


class ViperCredentials:
    def __init__(self, secrets_path: Path | str | None = None):
        self.path = Path(secrets_path) if secrets_path is not None else default_secrets_path()
        self.data = json.loads(self.path.read_text()) if self.path.is_file() else {}
        self.viper = self.data.setdefault("viper", {})

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

        viper_host = (
            urlparse(panel_host).hostname if "://" in panel_host else panel_host
        )
        if not viper_host:
            raise ValueError(f"invalid panel host: {panel_host!r}")
        self.viper.update(
            {
                "panel_host": viper_host,
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
            raise PermissionError(
                "cached LAN token was rejected; rerun "
                "`comelit bootstrap-local PANEL_IP`"
            ) from exc

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2) + "\n")
        os.chmod(tmp, 0o600)
        tmp.replace(self.path)
