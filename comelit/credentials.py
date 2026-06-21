"""Automatic bootstrap and persistence of Comelit LAN ViP credentials."""
from __future__ import annotations

import json
import os
import socket
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

from ._paths import default_secrets_path
from .api import CcApi
from .auth import Auth
from .provision import Provisioning
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
        if selected.activation_code:
            self.viper["activation_code"] = selected.activation_code
        if selected.description:
            self.viper["description"] = selected.description
        if backup.apartment_address:
            self.viper["source_address"] = f"{backup.apartment_address}{selected.slot}"
        if backup.entrance_address:
            self.viper["entrance_address"] = backup.entrance_address
        self._save()
        return selected

    def ensure_connection_config(self) -> dict:
        """Populate the panel address and activation metadata from the cloud."""
        if not self.viper.get("panel_host"):
            self._load_provisioning()
        return self.viper

    def ensure_authenticated(self, client: ViperClient) -> dict:
        """Authenticate with a stored token, or activate and persist a new one."""
        token = self.viper.get("user_token")
        if token:
            try:
                return client.authenticate(token)
            except PermissionError:
                # A revoked token can be recovered from the account's activation blob.
                pass

        activation_code = self.viper.get("activation_code")
        if not activation_code:
            self._load_provisioning()
            activation_code = self.viper.get("activation_code")
        if not activation_code:
            raise RuntimeError("cloud provisioning contains no ViP activation code")

        token = client.activate_user(
            activation_code,
            self.viper.get("description") or f"Python {socket.gethostname()}",
        )
        self.viper["user_token"] = token
        self._save()
        return {"response-code": 200, "response-string": "Access Granted", "activated": True}

    def _load_provisioning(self):
        auth = Auth(self.path)
        provisioning = Provisioning(CcApi(auth), auth.owner_auth_id)
        mac = self.viper.get("mac")
        if not mac:
            macs = provisioning.discover_macs()
            if len(macs) != 1:
                raise RuntimeError(f"select viper.mac; discovered devices: {macs}")
            mac = macs[0]

        sub_address = self.viper.get("sub_address")
        connection = provisioning.connection(mac, sub_address)
        self.viper.update(
            {
                "mac": mac,
                "panel_host": connection.local_address,
                "panel_port": connection.local_tcp_port,
                "activation_code": connection.activation_code,
                "sub_address": connection.sub_address,
            }
        )
        if connection.mqtt_topic_base:
            apartment = connection.mqtt_topic_base.split("/")[-2]
            self.viper.setdefault("my_address", apartment)
            self.viper.setdefault("source_address", f"{apartment}{connection.sub_address}")
        self._save()

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2) + "\n")
        os.chmod(tmp, 0o600)
        tmp.replace(self.path)
