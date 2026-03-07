"""Load Jenkins-related secrets from .env.

Variable names in .env match Jenkins credential IDs so the same .env can drive
both local runs and Playwright scripts that configure Jenkins (e.g. SCM_TOKEN,
GOOGLE_API_KEY). Optional: OPENAI_API_KEY, ANTHROPIC_API_KEY, etc.
"""

import os
from pathlib import Path

# Credential IDs used in Jenkins; values are read from env (after load_dotenv).
JENKINS_CREDENTIAL_IDS = (
    "SCM_TOKEN",
    "GOOGLE_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
)


def find_dotenv() -> Path | None:
    """Locate .env from repo root (cwd or parent up to repo root)."""
    cwd = Path.cwd()
    for d in [cwd, *cwd.parents]:
        env_file = d / ".env"
        if env_file.is_file():
            return env_file
        if (d / "pyproject.toml").is_file():
            env_file = d / ".env"
            return env_file if env_file.is_file() else None
    return None


class EnvLoader:
    """Load and expose Jenkins secrets from .env (same names as credential IDs)."""

    def __init__(self, env_path: Path | str | None = None) -> None:
        try:
            from dotenv import load_dotenv
        except ImportError:
            load_dotenv = None
        if load_dotenv:
            path = Path(env_path) if env_path else find_dotenv()
            if path:
                load_dotenv(path)
            else:
                load_dotenv()  # fallback: cwd / current env
        self._credential_ids = list(JENKINS_CREDENTIAL_IDS)

    def get_credentials(self) -> dict[str, str]:
        """Return a dict credential_id -> value for all set env vars (no empty values)."""
        out: dict[str, str] = {}
        for cid in self._credential_ids:
            val = os.environ.get(cid, "").strip()
            if val:
                out[cid] = val
        return out

    def get(self, credential_id: str, default: str = "") -> str:
        """Return the value for one credential ID from the environment."""
        return os.environ.get(credential_id, default).strip()
