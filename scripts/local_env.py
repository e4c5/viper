"""Helpers for local developer scripts that should read repo-root .env files.

This module is intentionally lightweight and does not depend on python-dotenv.
It is meant for local helper scripts under ``scripts/`` and should not be used
by the production review app, which continues to rely on the real process
environment only.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def find_local_env_file(start: Path | None = None) -> Path | None:
    """Return the nearest repo-root ``.env`` file from ``start``/cwd upward."""
    cwd = (start or Path.cwd()).resolve()
    for directory in [cwd, *cwd.parents]:
        env_file = directory / ".env"
        if env_file.is_file():
            return env_file
        if (directory / "pyproject.toml").is_file():
            return env_file if env_file.is_file() else None
    return None


def parse_env_file(path: Path) -> dict[str, str]:
    """Parse a simple ``.env`` file into a dict.

    Supported forms:
    - ``KEY=value``
    - ``export KEY=value``
    - optional surrounding single/double quotes on the value

    The parser intentionally keeps interior spaces, so values like
    ``MY_KEY=value with spaces`` are preserved as-is.
    """

    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not _ENV_KEY_RE.match(key):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return values


def load_local_env(*, override: bool = False, env_path: Path | None = None) -> Path | None:
    """Load repo-local ``.env`` values into ``os.environ`` when present.

    Existing environment variables win unless ``override=True``.
    Returns the loaded path, or ``None`` when no ``.env`` file was found.
    """

    path = env_path or find_local_env_file()
    if path is None:
        return None
    for key, value in parse_env_file(path).items():
        if override or key not in os.environ:
            os.environ[key] = value
    return path
