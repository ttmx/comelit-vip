import json
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from comelit.credentials import ViperCredentials
from comelit.web import PanelBackup, PanelUser


class FakeClient:
    def __init__(self, authenticate_error=None):
        self.authenticate_error = authenticate_error
    def authenticate(self, token):
        if self.authenticate_error:
            raise self.authenticate_error
        return {"response-code": 200, "token": token}


class CredentialTests(unittest.TestCase):
    def test_bootstrap_local_creates_secrets_and_selects_user(self):
        class FakeWebClient:
            def __init__(self, host, password, *, port):
                self.args = (host, password, port)

            def fetch_config(self):
                return PanelBackup(
                    [
                        PanelUser(1, "Phone", "a" * 32),
                        PanelUser(2, "Home", "b" * 32),
                    ],
                    apartment_address="SB000123",
                    entrance_address="SB100456",
                )

        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "config" / "secrets.json"
            credentials = ViperCredentials(path)
            selected = credentials.bootstrap_local(
                "192.0.2.5",
                "installer",
                user_slot=2,
                web_client_factory=FakeWebClient,
            )
            self.assertEqual(selected.description, "Home")
            saved = json.loads(path.read_text())
            self.assertEqual(
                saved["viper"],
                {
                    "panel_host": "192.0.2.5",
                    "panel_port": 64100,
                    "user_token": "b" * 32,
                    "description": "Home",
                    "source_address": "SB0001232",
                    "entrance_address": "SB100456",
                },
            )
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_bootstrap_local_url_persists_only_hostname(self):
        class FakeWebClient:
            def __init__(self, host, password, *, port):
                pass

            def fetch_config(self):
                return PanelBackup([PanelUser(1, "Phone", "a" * 32)])

        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "secrets.json"
            credentials = ViperCredentials(path)
            credentials.bootstrap_local(
                "http://192.0.2.8:8081/",
                "installer",
                web_client_factory=FakeWebClient,
            )
            self.assertEqual(
                json.loads(path.read_text())["viper"]["panel_host"], "192.0.2.8"
            )

    def test_connection_config_uses_cached_host(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "secrets.json"
            path.write_text(json.dumps({"viper": {"panel_host": "192.0.2.1"}}))
            credentials = ViperCredentials(path)
            self.assertEqual(
                credentials.ensure_connection_config()["panel_host"], "192.0.2.1"
            )

    def test_connection_config_requires_local_bootstrap(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "secrets.json"
            path.write_text("{}")
            with self.assertRaisesRegex(RuntimeError, "bootstrap-local"):
                ViperCredentials(path).ensure_connection_config()

    def test_uses_existing_token_without_activation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "secrets.json"
            path.write_text(json.dumps({"viper": {"user_token": "existing"}}))
            credentials = ViperCredentials(path)
            client = FakeClient()
            result = credentials.ensure_authenticated(client)
            self.assertEqual(result["token"], "existing")

    def test_missing_token_requires_local_bootstrap(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "secrets.json"
            path.write_text(json.dumps({"viper": {}}))
            credentials = ViperCredentials(path)
            with self.assertRaisesRegex(RuntimeError, "bootstrap-local"):
                credentials.ensure_authenticated(FakeClient())

    def test_rejected_token_requires_local_bootstrap(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "secrets.json"
            path.write_text(json.dumps({"viper": {"user_token": "expired"}}))
            credentials = ViperCredentials(path)
            client = FakeClient(PermissionError("rejected"))
            with self.assertRaisesRegex(PermissionError, "bootstrap-local"):
                credentials.ensure_authenticated(client)
