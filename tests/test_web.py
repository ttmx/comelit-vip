import gzip
import io
import pathlib
import sys
import tarfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from comelit.web import (
    PanelWebClient,
    PanelWebError,
    parse_panel_backup,
    parse_users_backup,
)


def make_backup(users_text: str) -> bytes:
    configs = {
        "users.cfg": gzip.compress(users_text.encode()),
        "apartments.cfg": gzip.compress(
            b'mspApartmentsMap.0 = 2:4:"SB000123"\n'
        ),
        "addressbook.cfg": gzip.compress(
            b'mspAddressBookEntrances.0.0 = 2:4:"Door" 3:4:"SB100456"\n'
        ),
    }
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for name, contents in configs.items():
            info = tarfile.TarInfo(f"etc/comelit/{name}")
            info.size = len(contents)
            archive.addfile(info, io.BytesIO(contents))
    return output.getvalue()


class FakeResponse:
    def __init__(self, *, text="", content=b"", status=200):
        self.text = text
        self.content = content
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeSession:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return next(self.responses)

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return next(self.responses)


class BackupParserTests(unittest.TestCase):
    def test_parses_only_active_users(self):
        archive = make_backup(
            'mspUsersMap.0.0 = 4:2:2 6:4:"" 9:4:""\n'
            'mspUsersMap.0.1 = 4:2:1 6:4:"Pixel 7" '
            '9:4:"0123456789abcdef0123456789abcdef" '
            '11:4:"owner@example.com" 18:4:"abcdefgh12"\n'
        )
        users = parse_users_backup(archive)
        self.assertEqual(len(users), 1)
        self.assertEqual(users[0].slot, 1)
        self.assertEqual(users[0].description, "Pixel 7")
        self.assertEqual(users[0].token, "0123456789abcdef0123456789abcdef")
        self.assertEqual(users[0].activation_code, "abcdefgh12")
        config = parse_panel_backup(archive)
        self.assertEqual(config.apartment_address, "SB000123")
        self.assertEqual(config.entrance_address, "SB100456")

    def test_rejects_backup_without_active_token(self):
        archive = make_backup('mspUsersMap.0.0 = 4:2:2 9:4:""\n')
        with self.assertRaisesRegex(PanelWebError, "no active"):
            parse_users_backup(archive)


class PanelWebClientTests(unittest.TestCase):
    def test_fetch_users_runs_login_create_download_flow(self):
        archive = make_backup(
            'mspUsersMap.0.2 = 4:2:1 6:4:"Home" '
            '9:4:"abcdef0123456789abcdef0123456789"\n'
        )
        backup_page_1 = "<a href='000001.tar.gz'>old</a><script>create-backup.html</script>"
        backup_page_2 = (
            backup_page_1 + "<a href='000002.tar.gz'>new</a>"
        )
        session = FakeSession(
            [
                FakeResponse(),                    # login POST
                FakeResponse(text=backup_page_1), # login verification GET
                FakeResponse(text=backup_page_1), # list before
                FakeResponse(),                    # create POST
                FakeResponse(text=backup_page_2), # list after
                FakeResponse(content=archive),    # download
            ]
        )
        users = PanelWebClient(
            "192.0.2.10", "secret", session=session
        ).fetch_users()
        self.assertEqual(users[0].slot, 2)
        self.assertEqual(
            [call[:2] for call in session.calls],
            [
                ("POST", "http://192.0.2.10:8080/do-login.html"),
                ("GET", "http://192.0.2.10:8080/config-backup.html"),
                ("GET", "http://192.0.2.10:8080/config-backup.html"),
                ("POST", "http://192.0.2.10:8080/create-backup.html"),
                ("GET", "http://192.0.2.10:8080/config-backup.html"),
                ("GET", "http://192.0.2.10:8080/000002.tar.gz"),
            ],
        )

    def test_login_rejects_login_page(self):
        session = FakeSession([FakeResponse(), FakeResponse(text="LOGIN_IS_REQUIRED")])
        with self.assertRaisesRegex(PanelWebError, "login failed"):
            PanelWebClient("192.0.2.10", "wrong", session=session).login()

    def test_authenticated_page_may_embed_login_required_javascript(self):
        page = "<script>const marker = 'LOGIN_IS_REQUIRED'</script>create-backup.html"
        session = FakeSession([FakeResponse(), FakeResponse(text=page)])
        PanelWebClient("192.0.2.10", "secret", session=session).login()

    def test_create_backup_rejects_full_unchanged_backup_list(self):
        page = "".join(
            f"<a href='{number:06}.tar.gz'>backup</a>" for number in range(1, 6)
        )
        session = FakeSession(
            [FakeResponse(text=page), FakeResponse(), FakeResponse(text=page)]
        )
        client = PanelWebClient("192.0.2.10", "secret", session=session)
        with self.assertRaisesRegex(PanelWebError, "backup limit reached"):
            client.create_backup()

    def test_create_backup_never_reuses_an_unverified_existing_backup(self):
        page = "<a href='000001.tar.gz'>old</a>"
        session = FakeSession(
            [FakeResponse(text=page), FakeResponse(), FakeResponse(text=page)]
        )
        client = PanelWebClient("192.0.2.10", "secret", session=session)
        with self.assertRaisesRegex(PanelWebError, "did not create a new"):
            client.create_backup()
