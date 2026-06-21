import pathlib
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from comelit.intercom import DEFAULT_ENTRANCE, DEFAULT_SOURCE, Intercom


class FakeCredentials:
    def __init__(self, config):
        self.config = config
        self.authenticated_with = None

    def ensure_connection_config(self):
        return self.config

    def ensure_authenticated(self, client):
        self.authenticated_with = client


class FakeClient:
    def __init__(self, host, port, *, timeout):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.calls = []

    def connect(self):
        self.calls.append(("connect",))

    def close(self):
        self.calls.append(("close",))

    def get_configuration(self, target):
        self.calls.append(("get_configuration", target))
        return {"target": target}

    def open_door(self, source, target, relay):
        self.calls.append(("open_door", source, target, relay))
        return "opened"

    def listen_rings(self, source):
        self.calls.append(("listen_rings", source))
        return iter(())

    def open_video_stream(self, source, target, **options):
        self.calls.append(("open_video_stream", source, target, options))
        return "stream"


class IntercomTests(unittest.TestCase):
    def make_panel(self, config=None, timeout=15):
        credentials = FakeCredentials(config or {"panel_host": "192.0.2.1"})
        patcher = patch("comelit.intercom.ViperClient", FakeClient)
        patcher.start()
        self.addCleanup(patcher.stop)
        return Intercom(credentials, timeout=timeout), credentials

    def test_lifecycle_connects_authenticates_initializes_and_closes(self):
        panel, credentials = self.make_panel(timeout=7)
        self.assertEqual(panel.client.host, "192.0.2.1")
        self.assertEqual(panel.client.port, 64100)
        self.assertEqual(panel.client.timeout, 7)

        with panel as connected:
            self.assertIs(connected, panel)
            self.assertIs(credentials.authenticated_with, panel.client)
            panel.connect()

        self.assertEqual(
            panel.client.calls,
            [("connect",), ("get_configuration", "none"), ("close",)],
        )

    def test_source_and_entrance_defaults(self):
        panel, _ = self.make_panel()
        self.assertEqual(panel.source, DEFAULT_SOURCE)
        self.assertEqual(panel.entrance, DEFAULT_ENTRANCE)
        self.assertEqual(panel.open_door(), "opened")
        self.assertEqual(
            panel.client.calls[-1],
            ("open_door", DEFAULT_SOURCE, DEFAULT_ENTRANCE, 1),
        )

    def test_configured_addresses_and_door_address_alias(self):
        panel, _ = self.make_panel(
            {
                "panel_host": "192.0.2.2",
                "panel_port": 12345,
                "source_address": "SB0000099",
                "door_address": "SB100099",
            }
        )
        self.assertEqual(panel.source, "SB0000099")
        self.assertEqual(panel.entrance, "SB100099")
        self.assertEqual(panel.client.port, 12345)

    def test_entrance_address_takes_precedence_and_actions_allow_targets(self):
        panel, _ = self.make_panel(
            {
                "panel_host": "192.0.2.3",
                "source_address": "SB0000088",
                "entrance_address": "SB100088",
                "door_address": "SB100077",
            }
        )
        self.assertEqual(panel.entrance, "SB100088")
        self.assertEqual(panel.video(hd=True), "stream")
        self.assertEqual(
            panel.client.calls[-1],
            (
                "open_video_stream",
                "SB0000088",
                "SB100088",
                {"hd": True, "resolution": None, "bitrate": None},
            ),
        )
        panel.open_door(relay=2, target="SB100066")
        self.assertEqual(
            panel.client.calls[-1],
            ("open_door", "SB0000088", "SB100066", 2),
        )


if __name__ == "__main__":
    unittest.main()
