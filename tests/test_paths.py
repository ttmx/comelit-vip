import os
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from comelit import _paths


class DefaultSecretsPathTests(unittest.TestCase):
    def test_environment_variable_takes_precedence(self):
        with tempfile.TemporaryDirectory() as directory:
            cwd = pathlib.Path(directory)
            (cwd / "secrets.json").write_text("{}")
            env_path = cwd / "from-env.json"
            previous = pathlib.Path.cwd()
            try:
                os.chdir(cwd)
                with patch.dict(os.environ, {_paths.ENV_VAR: str(env_path)}):
                    self.assertEqual(_paths.default_secrets_path(), env_path)
            finally:
                os.chdir(previous)

    def test_existing_cwd_secrets_is_used(self):
        with tempfile.TemporaryDirectory() as directory:
            cwd = pathlib.Path(directory)
            secrets = cwd / "secrets.json"
            secrets.write_text("{}")
            previous = pathlib.Path.cwd()
            try:
                os.chdir(cwd)
                with patch.dict(os.environ, {}, clear=True):
                    self.assertEqual(_paths.default_secrets_path(), secrets)
            finally:
                os.chdir(previous)

    def test_config_path_is_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            cwd = pathlib.Path(directory)
            config_path = cwd / ".config/comelit/secrets.json"
            previous = pathlib.Path.cwd()
            try:
                os.chdir(cwd)
                with patch.dict(os.environ, {}, clear=True):
                    with patch.object(_paths, "CONFIG_PATH", config_path):
                        self.assertEqual(_paths.default_secrets_path(), config_path)
            finally:
                os.chdir(previous)


if __name__ == "__main__":
    unittest.main()
