"""A kanban worker never falls back onto a tool-less provider (regression-case D1).

Failing over a dispatcher-owned worker onto a model that cannot carry tool
calls guarantees a clean exit with no terminal board call — a protocol
violation even though the work succeeded. Skipping that candidate keeps the
turn's ORIGINAL failure (usually a rate limit), which the dispatcher books as
a transient requeue instead. Only a positively-declared tool-less model is
skipped; unknown capability fails open.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from agent import chat_completion_helpers as cch


def _caps(supports_tools):
    return SimpleNamespace(supports_tools=supports_tools)


def test_toolless_candidate_skipped_for_worker(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_x")
    agent = SimpleNamespace()
    with patch(
        "agent.models_dev.get_model_capabilities", return_value=_caps(False)
    ):
        assert (
            cch._kanban_worker_toolless_candidate(agent, "muse-local", "muse-spark-1.3")
            is True
        )
        assert (
            cch._should_skip_fallback_candidate(
                agent,
                {"provider": "muse-local", "model": "muse-spark-1.3"},
                ("muse-local", "muse-spark-1.3", ""),
                "muse-local",
                "muse-spark-1.3",
                set(),
            )
            is True
        )


def test_capable_candidate_not_skipped(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_x")
    with patch("agent.models_dev.get_model_capabilities", return_value=_caps(True)):
        assert (
            cch._kanban_worker_toolless_candidate(
                SimpleNamespace(), "openai", "gpt-4o"
            )
            is False
        )


def test_unknown_capability_fails_open(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_x")
    with patch("agent.models_dev.get_model_capabilities", return_value=None):
        assert (
            cch._kanban_worker_toolless_candidate(
                SimpleNamespace(), "custom", "whatever"
            )
            is False
        )


def test_non_worker_unaffected(monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    with patch(
        "agent.models_dev.get_model_capabilities", return_value=_caps(False)
    ):
        assert (
            cch._kanban_worker_toolless_candidate(
                SimpleNamespace(), "muse-local", "muse-spark-1.3"
            )
            is False
        )
