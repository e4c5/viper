"""Helpers for constructing ADK runners with project-level defaults."""

from __future__ import annotations

import logging
import re
import warnings
from typing import Any
from unittest.mock import Mock

from code_review.config import get_llm_config

logger = logging.getLogger(__name__)


def create_runner(
    *,
    agent,
    app_name: str,
    session_service,
    auto_create_session: bool = True,
):
    """Create an ADK Runner, enabling Gemini context caching when supported."""
    from google.adk.runners import Runner

    cache_config = build_context_cache_config(agent=agent)
    if cache_config is None:
        return Runner(
            agent=agent,
            app_name=app_name,
            session_service=session_service,
            auto_create_session=auto_create_session,
        )

    from google.adk.apps.app import App

    app = App(
        name=app_name,
        root_agent=agent,
        context_cache_config=cache_config,
    )
    logger.debug("ADK context caching enabled for app=%s config=%s", app_name, cache_config)
    return Runner(
        app=app,
        session_service=session_service,
        auto_create_session=auto_create_session,
    )


def build_context_cache_config(*, agent=None) -> Any | None:
    """Return an ADK context cache config for native Gemini 3+ models."""
    cfg = get_llm_config()
    agent_model = _agent_model_name(agent)
    if agent_model is not None:
        if not _is_gemini_3_or_newer_model(agent_model):
            return None
    elif not _is_native_gemini_3_or_newer(cfg.provider, cfg.model):
        return None

    from google.adk.agents.context_cache_config import ContextCacheConfig

    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message=r"\[EXPERIMENTAL\] ContextCacheConfig:",
            category=UserWarning,
        )
        warnings.filterwarnings(
            "ignore",
            message=r"\[EXPERIMENTAL\] feature FeatureName\.AGENT_CONFIG is enabled\.",
            category=UserWarning,
        )
        return ContextCacheConfig()


def _agent_model_name(agent) -> str | None:
    """Return an agent's concrete model name, or None when it has no model field."""
    if isinstance(agent, Mock):
        return ""
    model = getattr(agent, "model", None)
    if isinstance(model, str):
        return model
    if getattr(model, "model", None):
        return ""
    return None


def _is_native_gemini_3_or_newer(provider: str, model: str) -> bool:
    """Return True for Google-native Gemini model IDs whose major version is >= 3."""
    if provider not in {"gemini", "vertex"}:
        return False
    return _is_gemini_3_or_newer_model(model)


def _is_gemini_3_or_newer_model(model: str) -> bool:
    """Return True for Gemini model IDs whose major version is >= 3."""
    normalized = (model or "").strip().lower()
    match = re.search(r"gemini-(\d+)", normalized)
    return bool(match and int(match.group(1)) >= 3)
