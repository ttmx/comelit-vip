"""Bootstrap ViP LAN credentials from the panel's local installer web UI."""
from __future__ import annotations

import gzip
import io
import re
import tarfile
from dataclasses import dataclass
from urllib.parse import urljoin

import requests

DEFAULT_WEB_PORT = 8080
DEFAULT_VIPER_PORT = 64100
_BACKUP_RE = re.compile(r"""href=['"](\d+\.tar\.gz)['"]""", re.IGNORECASE)
_USER_LINE_RE = re.compile(r"^mspUsersMap\.\d+\.(\d+)\s*=\s*(.*)$")
_FIELD_RE = re.compile(r'(\d+):(?:2|4):(?:"([^"]*)"|([^\s]+))')
_TOKEN_RE = re.compile(r"^[0-9a-fA-F]{32}$")
_VIP_ADDRESS_RE = re.compile(r'"(SB\d{6})"')


class PanelWebError(RuntimeError):
    """The panel web UI could not provide usable ViP credentials."""


@dataclass(frozen=True)
class PanelUser:
    """One active ViP user stored in the panel configuration."""

    slot: int
    description: str
    token: str
    email: str = ""


@dataclass(frozen=True)
class PanelBackup:
    """LAN configuration recovered from a panel backup."""

    users: list[PanelUser]
    apartment_address: str = ""
    entrance_address: str = ""


def _read_config(bundle: tarfile.TarFile, name: str) -> str:
    member = bundle.getmember(f"etc/comelit/{name}")
    nested = bundle.extractfile(member)
    if nested is None:
        raise PanelWebError(f"backup contains no readable {name}")
    data = nested.read()
    if data.startswith(b"\x1f\x8b"):
        data = gzip.decompress(data)
    return data.decode("utf-8", "replace")


def parse_panel_backup(archive: bytes) -> PanelBackup:
    """Return ViP users and addresses from a configuration backup.

    ``users.cfg`` is itself gzip-compressed inside the outer ``tar.gz``.
    Current firmware stores the description in field 6, the persistent LAN
    token in field 9, and the account email in field 11.
    """
    try:
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as bundle:
            users_text = _read_config(bundle, "users.cfg")
            apartments_text = _read_config(bundle, "apartments.cfg")
            addressbook_text = _read_config(bundle, "addressbook.cfg")
    except (KeyError, OSError, tarfile.TarError) as exc:
        raise PanelWebError("invalid or incomplete panel backup") from exc

    users: list[PanelUser] = []
    for line in users_text.splitlines():
        match = _USER_LINE_RE.match(line)
        if not match:
            continue
        fields = {}
        for field_match in _FIELD_RE.finditer(match.group(2)):
            quoted, bare = field_match.group(2), field_match.group(3)
            fields[int(field_match.group(1))] = quoted if quoted is not None else bare
        # Field 4 is the enabled/occupied flag. Empty slots use value 2.
        token = fields.get(9, "")
        if fields.get(4) != "1" or not _TOKEN_RE.fullmatch(token):
            continue
        users.append(
            PanelUser(
                slot=int(match.group(1)),
                description=fields.get(6, ""),
                token=token.lower(),
                email=fields.get(11, ""),
            )
        )
    if not users:
        raise PanelWebError("backup contains no active ViP user tokens")
    apartment_match = _VIP_ADDRESS_RE.search(apartments_text)
    entrance_match = next(
        (
            _VIP_ADDRESS_RE.search(line)
            for line in addressbook_text.splitlines()
            if line.startswith("mspAddressBookEntrances.")
        ),
        None,
    )
    return PanelBackup(
        users=users,
        apartment_address=apartment_match.group(1) if apartment_match else "",
        entrance_address=entrance_match.group(1) if entrance_match else "",
    )


def parse_users_backup(archive: bytes) -> list[PanelUser]:
    """Return active ViP users from a configuration backup."""
    return parse_panel_backup(archive).users


class PanelWebClient:
    """Client for the password-protected installer UI on TCP port 8080."""

    def __init__(
        self,
        host: str,
        password: str,
        *,
        port: int = DEFAULT_WEB_PORT,
        timeout: float = 15.0,
        session: requests.Session | None = None,
    ):
        if not host:
            raise ValueError("panel host is required")
        if not password:
            raise ValueError("installer password is required")
        if "://" in host:
            self.base_url = host.rstrip("/") + "/"
        else:
            self.base_url = f"http://{host}:{port}/"
        self.password = password
        self.timeout = timeout
        self.session = session or requests.Session()

    def _url(self, path: str) -> str:
        return urljoin(self.base_url, path)

    def login(self) -> None:
        response = self.session.post(
            self._url("do-login.html"),
            data={"l-pwd": self.password},
            timeout=self.timeout,
        )
        response.raise_for_status()
        page = self.session.get(
            self._url("config-backup.html"), timeout=self.timeout
        )
        page.raise_for_status()
        if "create-backup.html" not in page.text:
            raise PanelWebError("installer login failed")

    def list_backups(self) -> list[str]:
        response = self.session.get(
            self._url("config-backup.html"), timeout=self.timeout
        )
        response.raise_for_status()
        if response.text.strip() == "LOGIN_IS_REQUIRED":
            raise PanelWebError("installer login is required")
        return sorted(set(_BACKUP_RE.findall(response.text)))

    def create_backup(self) -> str:
        before = set(self.list_backups())
        response = self.session.post(
            self._url("create-backup.html"), timeout=self.timeout
        )
        response.raise_for_status()
        if response.text.strip() == "LOGIN_IS_REQUIRED":
            raise PanelWebError("installer login is required")
        after = self.list_backups()
        created = sorted(set(after) - before)
        if created:
            return created[-1]
        message = response.text.strip()
        if len(after) >= 5:
            raise PanelWebError(
                "panel backup limit reached; delete an old backup in the installer UI"
            )
        raise PanelWebError(message or "panel did not create a new configuration backup")

    def download_backup(self, filename: str) -> bytes:
        if not re.fullmatch(r"\d+\.tar\.gz", filename):
            raise ValueError(f"invalid backup filename: {filename!r}")
        response = self.session.get(self._url(filename), timeout=self.timeout)
        response.raise_for_status()
        return response.content

    def fetch_config(self) -> PanelBackup:
        """Log in, create/download a backup, and return its LAN configuration."""
        self.login()
        filename = self.create_backup()
        return parse_panel_backup(self.download_backup(filename))

    def fetch_users(self) -> list[PanelUser]:
        """Log in, create/download a backup, and return its active ViP users."""
        return self.fetch_config().users
