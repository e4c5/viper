"""Public API contract: __all__ names resolve and the package stays import-light."""

import subprocess
import sys

import code_review


def test_all_public_names_resolve():
    assert code_review.__all__, "__all__ must not be empty — the check would pass vacuously"
    expected = {
        "run_review",
        "FindingV1",
        "SCMConfig",
        "LLMConfig",
        "CodeReviewAppConfig",
        "get_scm_config",
        "get_llm_config",
        "get_code_review_app_config",
        "get_provider",
        "configure_logging",
    }
    missing = expected - set(code_review.__all__)
    assert not missing, f"expected public names missing from __all__: {sorted(missing)}"
    for name in code_review.__all__:
        assert getattr(code_review, name) is not None, name


def test_import_does_not_pull_google_adk():
    code = (
        "import sys\n"
        "import code_review\n"
        "assert 'google.adk' not in sys.modules, 'ADK imported eagerly'\n"
        "assert 'litellm' not in sys.modules, 'litellm imported eagerly'\n"
        "print('ok')\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60
    )
    assert proc.returncode == 0, proc.stderr
    assert "ok" in proc.stdout
