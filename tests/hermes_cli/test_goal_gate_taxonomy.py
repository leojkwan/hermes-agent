"""Contract tests for real user-gate handling in goal continuations."""

from __future__ import annotations

import inspect
from pathlib import Path

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


def test_judge_prompt_defines_real_gates_and_reversible_continue():
    prompt = goals.JUDGE_SYSTEM_PROMPT.lower()

    for gate_class in GATE_CLASSES:
        assert gate_class in prompt

    assert "reversible choice" in prompt
    assert "local merge" in prompt
    assert "return continue" in prompt
    assert "not blocked" in prompt

    source = Path(inspect.getsourcefile(goals)).read_text(encoding="utf-8")
    assert "If you are blocked and need input from the user, say so clearly and stop." not in source
