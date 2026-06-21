"""Where to find the ``secrets.json`` credential/state file.

An installed package must never read or write inside ``site-packages``, so the
default location is resolved at call time from the environment and the user's
working/config directories instead of being baked relative to this module.
"""
from __future__ import annotations

import os
from pathlib import Path

ENV_VAR = "COMELIT_SECRETS"
CONFIG_PATH = Path.home() / ".config" / "comelit" / "secrets.json"


def default_secrets_path() -> Path:
    """Resolve the default secrets path.

    Resolution order:
      1. ``$COMELIT_SECRETS`` if set,
      2. ``./secrets.json`` in the current working directory if it exists,
      3. ``~/.config/comelit/secrets.json``.

    Only an existing CWD file wins step 2; otherwise the config-dir path is
    returned (it may not exist yet — the caller decides whether that is an error
    or a file to create).
    """
    env = os.environ.get(ENV_VAR)
    if env:
        return Path(env).expanduser()
    cwd = Path("secrets.json")
    if cwd.is_file():
        return cwd.resolve()
    return CONFIG_PATH
