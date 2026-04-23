import logging
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from code_review.adk_runner import build_context_cache_config, create_runner


def _llm_cfg(
    *,
    provider="gemini",
    model="gemini-3.1",
):
    return SimpleNamespace(
        provider=provider,
        model=model,
    )


@patch("code_review.adk_runner.get_llm_config")
def test_build_context_cache_config_for_native_gemini_3_plus(mock_get_llm_config):
    mock_get_llm_config.return_value = _llm_cfg(model="gemini-3-flash-preview")

    cache_config = build_context_cache_config()

    assert cache_config is not None


@patch("code_review.adk_runner.get_llm_config")
def test_build_context_cache_config_skips_non_native_models(mock_get_llm_config):
    mock_get_llm_config.return_value = _llm_cfg(model="gemini-2.5-flash")
    assert build_context_cache_config() is None

    mock_get_llm_config.return_value = _llm_cfg(provider="openai", model="gpt-5.4")
    assert build_context_cache_config() is None


@patch("code_review.adk_runner.get_llm_config")
def test_build_context_cache_config_skips_litellm_wrapped_gemini(mock_get_llm_config):
    mock_get_llm_config.return_value = _llm_cfg()
    agent = SimpleNamespace(model=SimpleNamespace(model="openrouter/google/gemini-3.1"))

    assert build_context_cache_config(agent=agent) is None


@patch("google.adk.apps.app.App")
@patch("google.adk.runners.Runner")
@patch("code_review.adk_runner.get_llm_config")
def test_create_runner_wraps_gemini_3_agent_in_cached_app(
    mock_get_llm_config, mock_runner_cls, mock_app_cls, caplog
):
    mock_get_llm_config.return_value = _llm_cfg()
    mock_app_cls.return_value = MagicMock(name="cached_app")
    runner = SimpleNamespace()
    mock_runner_cls.return_value = runner
    session_service = MagicMock()
    agent = SimpleNamespace(model="gemini-3-flash-preview")

    caplog.set_level(logging.INFO)
    result = create_runner(agent=agent, app_name="code_review", session_service=session_service)

    _, app_kwargs = mock_app_cls.call_args
    assert app_kwargs["name"] == "code_review"
    assert app_kwargs["root_agent"] is agent
    assert app_kwargs["context_cache_config"] is not None
    assert result.agent is agent
    assert result.context_cache_enabled is True
    assert result.context_cache_config is app_kwargs["context_cache_config"]
    assert (
        "adk_context_cache enabled app=code_review provider=gemini model=gemini-3.1" in caplog.text
    )
    mock_runner_cls.assert_called_once_with(
        app=mock_app_cls.return_value,
        session_service=session_service,
        auto_create_session=True,
    )


@patch("google.adk.apps.app.App")
@patch("google.adk.runners.Runner")
@patch("code_review.adk_runner.get_llm_config")
def test_create_runner_preserves_cached_runner_agent_when_adk_exposes_one(
    mock_get_llm_config, mock_runner_cls, mock_app_cls
):
    mock_get_llm_config.return_value = _llm_cfg()
    mock_app_cls.return_value = MagicMock(name="cached_app")
    existing_agent = SimpleNamespace(model="gemini-3-flash-preview")
    runner = SimpleNamespace(agent=existing_agent)
    mock_runner_cls.return_value = runner
    session_service = MagicMock()
    agent = SimpleNamespace(model="gemini-3-flash-preview")

    result = create_runner(agent=agent, app_name="code_review", session_service=session_service)

    assert result.agent is existing_agent


@patch("google.adk.runners.Runner")
@patch("code_review.adk_runner.get_llm_config")
def test_create_runner_keeps_plain_runner_when_cache_not_supported(
    mock_get_llm_config, mock_runner_cls
):
    mock_get_llm_config.return_value = _llm_cfg(model="gemini-2.5-flash")
    session_service = MagicMock()
    agent = SimpleNamespace(model="gemini-2.5-flash")

    create_runner(agent=agent, app_name="code_review", session_service=session_service)

    mock_runner_cls.assert_called_once_with(
        agent=agent,
        app_name="code_review",
        session_service=session_service,
        auto_create_session=True,
    )
