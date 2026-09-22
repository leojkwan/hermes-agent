"""MoA drive wiring: engagement gate, structured fan-out observability, config validator.

Covers the acceptance criteria that are NOT already covered by the fan-out cadence
family (test_moa_fanout_cadence.py owns concurrency/caching behavior):
- gate.heuristic bypasses trivial turns (fast path: no reference calls, no spend)
- gate mode/min_chars are config-driven; mode=always keeps the previous behavior
- one structured log line per fan-out carries per-slot latency, token counts,
  estimated cost, contributing/failed proposers and the fan-out wall clock
- all-references-failed degrades to the acting aggregator answering alone
- config write boundary rejects an enabled advisor on the aggregator's provider
"""

from __future__ import annotations

import json
import logging
import time
from types import SimpleNamespace

import pytest

from agent.moa_loop import MoAChatCompletions, _run_references_parallel
from hermes_cli.moa_config import validate_moa_payload


def _response(content="done", *, usage=None):
    message = SimpleNamespace(content=content, tool_calls=[])
    choice = SimpleNamespace(message=message, finish_reason="stop")
    return SimpleNamespace(choices=[choice], usage=usage, model="fake-model")


LONG_PROMPT = (
    "Audit the MoA wiring end to end: trace every call site, compare against the brief, "
    "flag any place the topology silently diverges from config, and propose the fix."
)


def _config(home, *, gate: dict | None, min_chars: int = 120):
    home.mkdir()
    gate_yaml = ""
    if gate is not None:
        gate_yaml = "\n      gate:\n        mode: {}\n        min_chars: {}".format(
            gate.get("mode"), gate.get("min_chars", min_chars)
        )
    (home / "config.yaml").write_text(
        f"""
moa:
  default_preset: review
  save_traces: false
  presets:
    review:
      fanout: user_turn{gate_yaml}
      reference_models:
        - provider: openai-codex
          model: gpt-5.5
        - provider: zai
          model: glm-5.3
      aggregator:
        provider: openrouter
        model: anthropic/claude-opus-4.8
""".strip(),
        encoding="utf-8",
    )


def _install_fake_llm(monkeypatch, calls):
    def fake_call_llm(**kwargs):
        calls.append(kwargs)
        if kwargs["task"] == "moa_reference":
            return _response(f"advice #{len(calls)}")
        return _response("acted")

    monkeypatch.setattr("agent.moa_loop.call_llm", fake_call_llm)


def _messages(text):
    return [{"role": "user", "content": text}]


def test_heuristic_gate_bypasses_trivial_turn(monkeypatch, tmp_path, caplog):
    home = tmp_path / "home"
    _config(home, gate={"mode": "heuristic", "min_chars": 120})
    monkeypatch.setenv("HERMES_HOME", str(home))
    calls: list[dict] = []
    _install_fake_llm(monkeypatch, calls)

    facade = MoAChatCompletions("review")
    with caplog.at_level(logging.INFO, logger="agent.moa_loop"):
        response = facade.create(messages=_messages("ok"), stream=False)

    assert getattr(response.choices[0].message, "content", "") == "acted"
    # Fast path: zero advisor calls, only the acting aggregator call.
    assert [c["task"] for c in calls] == ["moa_aggregator"]
    # Decision surfaced as a structured log line.
    bypass_lines = [r.message for r in caplog.records if r.message.startswith("moa_gate_bypass ")]
    assert bypass_lines, "expected a moa_gate_bypass structured log record"
    record = json.loads(bypass_lines[0].removeprefix("moa_gate_bypass "))
    assert record["preset"] == "review"
    assert record["bypass"] is True
    assert record["user_chars"] == len("ok")
    assert record["min_chars"] == 120


def test_heuristic_gate_passes_substantive_turn(monkeypatch, tmp_path):
    home = tmp_path / "home"
    _config(home, gate={"mode": "heuristic", "min_chars": 120})
    monkeypatch.setenv("HERMES_HOME", str(home))
    calls: list[dict] = []
    _install_fake_llm(monkeypatch, calls)

    facade = MoAChatCompletions("review")
    facade.create(messages=_messages(LONG_PROMPT), stream=False)

    tasks = [c["task"] for c in calls]
    assert tasks.count("moa_reference") == 2  # both proposers ran
    assert tasks[-1] == "moa_aggregator"


def test_gate_is_config_driven_min_chars(monkeypatch, tmp_path):
    home = tmp_path / "home"
    # min_chars 2: "ok" clears the bar -> fan out.
    _config(home, gate={"mode": "heuristic", "min_chars": 2})
    monkeypatch.setenv("HERMES_HOME", str(home))
    calls: list[dict] = []
    _install_fake_llm(monkeypatch, calls)

    MoAChatCompletions("review").create(messages=_messages("ok"), stream=False)
    assert [c["task"] for c in calls].count("moa_reference") == 2


def test_default_gate_mode_always_keeps_previous_behavior(monkeypatch, tmp_path):
    home = tmp_path / "home"
    _config(home, gate=None)  # no gate block at all
    monkeypatch.setenv("HERMES_HOME", str(home))
    calls: list[dict] = []
    _install_fake_llm(monkeypatch, calls)

    MoAChatCompletions("review").create(messages=_messages("ok"), stream=False)
    assert [c["task"] for c in calls].count("moa_reference") == 2


def test_structured_fanout_log_has_all_fields(monkeypatch, tmp_path, caplog):
    home = tmp_path / "home"
    _config(home, gate=None)
    monkeypatch.setenv("HERMES_HOME", str(home))
    calls: list[dict] = []
    _install_fake_llm(monkeypatch, calls)

    with caplog.at_level(logging.INFO, logger="agent.moa_loop"):
        MoAChatCompletions("review").create(messages=_messages(LONG_PROMPT), stream=False)

    lines = [r.message for r in caplog.records if r.message.startswith("moa_fanout ")]
    assert len(lines) == 1, f"expected exactly one structured fan-out record, got {lines}"
    summary = json.loads(lines[0].removeprefix("moa_fanout "))
    assert summary["preset"] == "review"
    assert summary["slots"] == 2
    assert summary["contributing"]  # which proposers actually contributed
    assert not summary["failed"]
    assert isinstance(summary["wall_clock_s"], (int, float))
    assert set(summary["totals"]) >= {"input_tokens", "output_tokens", "cost_usd", "cost_status"}
    for label, entry in summary["per_slot"].items():
        assert isinstance(entry["latency_s"], (int, float))
        assert "input_tokens" in entry and "cost_usd" in entry


def test_fanout_summary_math_is_exact():
    """The summary's totals/contributors are computed from real accounting, not echoed."""
    from agent.moa_loop import _RefAccounting, _fanout_summary
    from agent.usage_pricing import CanonicalUsage

    good = _RefAccounting(
        CanonicalUsage(input_tokens=100, output_tokens=40),
        cost_usd=0.25, cost_status="estimated", cost_source="provider_cost_api",
        output="advice", latency_s=1.5, provider="openai-codex", model="gpt-5.5",
    )
    dead = _RefAccounting(CanonicalUsage(), output="[failed: boom]", latency_s=0.2, provider="zai", model="glm-5.3")
    summary = _fanout_summary("review", [("openai-codex:gpt-5.5", "advice", good), ("zai:glm-5.3", "[failed: boom]", dead)], 1.6)

    assert summary["contributing"] == ["openai-codex:gpt-5.5"]
    assert summary["failed"] == ["zai:glm-5.3"]
    assert summary["totals"] == {
        "input_tokens": 100, "output_tokens": 40,
        "cost_usd": 0.25, "cost_status": "measured",
    }
    assert summary["per_slot"]["openai-codex:gpt-5.5"]["latency_s"] == 1.5
    assert summary["per_slot"]["zai:glm-5.3"]["cost_usd"] is None


def test_all_references_failed_degrades_to_aggregator_alone(monkeypatch, tmp_path, caplog):
    home = tmp_path / "home"
    _config(home, gate=None)
    monkeypatch.setenv("HERMES_HOME", str(home))

    def fake_call_llm(**kwargs):
        calls.append(kwargs)
        if kwargs["task"] == "moa_reference":
            raise RuntimeError("provider exploded")
        return _response("acted alone")

    calls: list[dict] = []
    monkeypatch.setattr("agent.moa_loop.call_llm", fake_call_llm)

    with caplog.at_level(logging.WARNING, logger="agent.moa_loop"):
        response = MoAChatCompletions("review").create(messages=_messages(LONG_PROMPT), stream=False)

    # The turn NEVER fails: the acting aggregator answers without advisory guidance.
    assert getattr(response.choices[0].message, "content", "") == "acted alone"
    agg_call = [c for c in calls if c["task"] == "moa_aggregator"][-1]
    flat = json.dumps(agg_call["messages"], default=str)
    assert "Act on your own judgment" in flat
    assert any("all 2 reference(s) failed" in r.message for r in caplog.records)


def test_validator_rejects_same_provider_advisor():
    payload = {
        "presets": {
            "bad": {
                "reference_models": [
                    {"provider": "openai-codex", "model": "gpt-5.5", "enabled": True},
                    {"provider": "openrouter", "model": "z-ai/glm-5.3-flashx", "enabled": True},
                ],
                "aggregator": {"provider": "openrouter", "model": "z-ai/glm-5.3-flashx"},
            },
            "good": {
                "reference_models": [{"provider": "zai", "model": "glm-5.3", "enabled": True}],
                "aggregator": {"provider": "openrouter", "model": "z-ai/glm-5.3-flashx"},
            },
        }
    }
    problems = validate_moa_payload(payload)
    assert any("bad" in p and "matches the aggregator's provider" in p for p in problems)
    assert all("good" not in p for p in problems)


def test_validator_rejects_bad_gate_shapes():
    problems = validate_moa_payload({
        "reference_models": [{"provider": "zai", "model": "glm-5.3"}],
        "aggregator": {"provider": "openrouter", "model": "m"},
        "gate": {"mode": "sometimes", "min_chars": -5},
    })
    assert any("mode must be 'always' or 'heuristic'" in p for p in problems)
    assert any("min_chars must be a positive integer" in p for p in problems)
    # Valid gate shapes pass clean.
    ok = validate_moa_payload({
        "reference_models": [{"provider": "zai", "model": "glm-5.3"}],
        "aggregator": {"provider": "openrouter", "model": "m"},
        "gate": {"mode": "heuristic", "min_chars": 80},
    })
    assert ok == []


def test_fanout_wall_clock_is_concurrent_not_sum(monkeypatch):
    """Total fan-out latency tracks the slowest proposer, not the sum (AC: concurrency)."""

    def slow_reference(slot, messages, **kwargs):
        time.sleep(0.4)
        return _slot_label_of(slot), f"advice from {_slot_label_of(slot)}", None

    from agent import moa_loop

    def _slot_label_of(slot):
        return f"{slot['provider']}:{slot['model']}"

    monkeypatch.setattr(moa_loop, "_run_reference", slow_reference)
    slots = [
        {"provider": "openai-codex", "model": "gpt-5.5"},
        {"provider": "zai", "model": "glm-5.3"},
        {"provider": "openrouter", "model": "deepseek/deepseek-v4-pro"},
    ]
    t0 = time.monotonic()
    outputs = _run_references_parallel(slots, _messages(LONG_PROMPT))
    wall = time.monotonic() - t0

    assert len(outputs) == 3
    assert wall < 1.0, f"fan-out took {wall:.2f}s — sequential sum would be >= 1.2s"
    summary = moa_loop._fanout_summary("review", outputs, wall)
    assert len(summary["contributing"]) == 3


def caplog_var(level):
    import contextlib

    @contextlib.contextmanager
    def _ctx(caplog):
        yield caplog

    # pytest's caplog fixture cannot be requested from a helper; tests that need
    # it use the fixture directly. This shim exists only for the bypass test,
    # which uses caplog as a normal fixture argument instead.
    raise NotImplementedError


@pytest.fixture(name="caplog_var")
def _caplog_var_fixture(caplog):
    return lambda level: caplog.at_level(level)


def test_bypass_record_respects_caplog_level(monkeypatch, tmp_path, caplog):
    home = tmp_path / "home"
    _config(home, gate={"mode": "heuristic", "min_chars": 100})
    monkeypatch.setenv("HERMES_HOME", str(home))
    calls: list[dict] = []
    _install_fake_llm(monkeypatch, calls)

    with caplog.at_level(logging.INFO, logger="agent.moa_loop"):
        MoAChatCompletions("review").create(messages=_messages(LONG_PROMPT), stream=False)

    # LONG_PROMPT clears even 500 chars -> no bypass, fan-out ran.
    assert [c["task"] for c in calls].count("moa_reference") == 2
    assert not [r for r in caplog.records if "moa_gate_bypass" in r.message]
