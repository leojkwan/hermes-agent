"""Contract tests for real user-gate handling in goal continuations."""

from __future__ import annotations

import pytest

from hermes_cli import goals


GATE_CLASSES = (
    "credentials or account access",
    "spending money",
    "sending or publishing anything outside this machine",
    "destructive or irreversible action",
    "product decision that cannot be undone",
)


@pytest.mark.parametrize(
    "template",
    (
        goals.CONTINUATION_PROMPT_TEMPLATE,
        goals.CONTINUATION_PROMPT_WITH_CONTRACT_TEMPLATE,
        goals.CONTINUATION_PROMPT_WITH_SUBGOALS_TEMPLATE,
    ),
    ids=("plain", "contract", "subgoals"),
)
def test_each_continuation_template_names_all_real_user_gates(template):
    rendered = template.format(
        goal="ship the change",
        contract_block="Stop condition: verified",
        subgoals_block="one criterion",
    ).lower()

    for gate_class in GATE_CLASSES:
        assert gate_class in rendered
    assert "every remaining step" in rendered
    assert "no recorded grant" in rendered
    assert "continue with the rest" in rendered
    assert "if you are blocked and need input from the user, say so clearly and stop." not in rendered


def test_judge_prompt_defines_real_gates_and_reversible_continue(monkeypatch):
    from types import SimpleNamespace

    seen = {}

    def fake_call_llm(*args, **kwargs):
        seen["messages"] = kwargs["messages"]
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            content='{"verdict": "continue", "reason": "required work remains"}',
        ))])

    monkeypatch.setattr("agent.auxiliary_client.call_llm", fake_call_llm)
    goals.judge_goal("finish required work", "One deliverable is ready.")
    prompt = seen["messages"][0]["content"].lower()

    for gate_class in GATE_CLASSES:
        assert gate_class in prompt

    assert "reversible choice" in prompt
    assert "local merge" in prompt
    assert "return continue" in prompt
    assert "not blocked" in prompt

    assert "every remaining step" in prompt
    assert "recorded grant" in prompt
    assert "remaining action" in prompt
    assert "missing grant" in prompt
    assert "required work within that goal remains" in prompt
    assert "optional follow-ups outside the stated goal do not prevent done" in prompt
    assert "destination and payload" in prompt
    assert "rollback alone does not authorize it" in prompt
