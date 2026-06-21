import argparse
import io
import json
import pathlib
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from comelit.cli import cmd_config


class CliTests(unittest.TestCase):
    def test_config_prints_cached_lan_config_without_token(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "secrets.json"
            token = "a" * 32
            path.write_text(
                json.dumps(
                    {
                        "viper": {
                            "panel_host": "192.0.2.5",
                            "panel_port": 64100,
                            "source_address": "SB0001231",
                            "entrance_address": "SB100456",
                            "description": "Phone",
                            "user_token": token,
                        }
                    }
                )
            )
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(cmd_config(argparse.Namespace(secrets=path)), 0)
            rendered = output.getvalue()
            self.assertIn("192.0.2.5:64100", rendered)
            self.assertIn("LAN token     : cached", rendered)
            self.assertNotIn(token, rendered)

    def test_config_requires_local_bootstrap(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "secrets.json"
            path.write_text("{}")
            with self.assertRaisesRegex(SystemExit, "bootstrap-local"):
                cmd_config(argparse.Namespace(secrets=path))
