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
        self.activated = None

    def authenticate(self, token):
        if self.authenticate_error:
            raise self.authenticate_error
        return {"response-code": 200, "token": token}

    def activate_user(self, code, description):
        self.activated = (code, description)
        return "0123456789abcdef0123456789abcdef"


class CredentialTests(unittest.TestCase):
    def test_bootstrap_local_creates_secrets_and_selects_user(self):
        class FakeWebClient:
            def __init__(self, host, password, *, port):
                self.args = (host, password, port)

            def fetch_config(self):
                return PanelBackup(
                    [
                        PanelUser(1, "Phone", "a" * 32, activation_code="code-one"),
                        PanelUser(2, "Home", "b" * 32, activation_code="code-two"),
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
                    "activation_code": "code-two",
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

    def test_connection_config_does_not_call_cloud_when_present(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "secrets.json"
            path.write_text(json.dumps({"viper": {"panel_host": "192.0.2.1"}}))
            credentials = ViperCredentials(path)
            credentials._load_provisioning = lambda: self.fail("unexpected cloud call")
            self.assertEqual(
                credentials.ensure_connection_config()["panel_host"], "192.0.2.1"
            )

    def test_uses_existing_token_without_activation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "secrets.json"
            path.write_text(json.dumps({"viper": {"user_token": "existing"}}))
            credentials = ViperCredentials(path)
            client = FakeClient()
            result = credentials.ensure_authenticated(client)
            self.assertEqual(result["token"], "existing")
            self.assertIsNone(client.activated)

    def test_activates_and_persists_missing_token(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "secrets.json"
            path.write_text(
                json.dumps(
                    {
                        "viper": {
                            "activation_code": "cloud-code",
                            "description": "Test client",
                        }
                    }
                )
            )
            credentials = ViperCredentials(path)
            client = FakeClient()
            result = credentials.ensure_authenticated(client)
            self.assertTrue(result["activated"])
            self.assertEqual(client.activated, ("cloud-code", "Test client"))
            saved = json.loads(path.read_text())
            self.assertEqual(
                saved["viper"]["user_token"], "0123456789abcdef0123456789abcdef"
            )
