"""Base review criteria prompt fragments."""

from pathlib import Path

_PROMPTS_DIR = Path(__file__).parent


def _read_prompt_fragment(filename: str, *, required: bool = False) -> str:
    """
    Read a prompt fragment from a sibling .md file.

    Optional fragments return an empty string when not present.
    Required fragments raise a clear error on missing files.
    """
    path = _PROMPTS_DIR / filename
    try:
        return path.read_text(encoding="utf-8").rstrip()
    except FileNotFoundError as exc:
        if required:
            raise RuntimeError(
                f"Missing prompt fragment '{filename}' under '{_PROMPTS_DIR}'."
            ) from exc
        return ""


BASE_REVIEW_PROMPT = _read_prompt_fragment("base.md", required=True)
