"""Tests for kanban note-emission Langfuse tracing (kanban_output_tracing).

Contract under test: tracing is observability-only — ``guarded_text`` returns
byte-identical output to ``apply_render_guards`` with tracing on, off, or
broken — and each emission produces a root span per surface, one span per
pipeline stage, and a ``final_output`` span carrying the complete text.
"""

from __future__ import annotations

import uuid
from collections import deque

import pytest

from hermes_cli import kanban_output_tracing as kot
from hermes_cli.kanban_readability import apply_render_guards

SAMPLE = """STATUS
Done except for the final scan; review package lives at
~/.shadow/clean/snapshot-20260920 and the checklist below.

NEXT ACTION
Leo: accept or dictate corrections per state.
"""


class _FakeSpan:
    def __init__(self, client, name, **kwargs):
        self.client = client
        self.name = name
        self.kwargs = kwargs
        self.children: list[_FakeSpan] = []
        self.ended = False

    def start_observation(self, name, **kwargs):
        span = _FakeSpan(self.client, name, **kwargs)
        self.children.append(span)
        self.client.spans.append(span)
        return span

    def update(self, **kwargs):
        self.kwargs.update(kwargs)

    def end(self):
        self.ended = True


class _FakeCtx:
    def __init__(self, span):
        self.span = span

    def __enter__(self):
        return self.span

    def __exit__(self, *exc):
        return None


class _FakeClient:
    def __init__(self, explode: bool = False):
        self.roots: list[_FakeSpan] = []
        self.spans: list[_FakeSpan] = []
        self.seeds: list[str] = []
        self.explode = explode
        self.flushed = False

    def create_trace_id(self, seed=""):
        self.seeds.append(seed)
        return uuid.uuid4().hex

    def start_as_current_observation(self, **kwargs):
        if self.explode:
            raise RuntimeError("sdk broken")
        root = _FakeSpan(self, kwargs.get("name", ""), **{k: v for k, v in kwargs.items() if k != "name"})
        self.roots.append(root)
        self.spans.append(root)
        return _FakeCtx(root)

    def flush(self):
        self.flushed = True


@pytest.fixture
def fake_client(monkeypatch):
    client = _FakeClient()
    monkeypatch.setattr(kot, "_client", client)
    monkeypatch.setattr(kot, "_seen", set())
    monkeypatch.setattr(kot, "_recent_trace_ids", deque(maxlen=64))
    return client


@pytest.fixture
def no_client(monkeypatch):
    monkeypatch.setattr(kot, "_client", kot._INIT_FAILED)
    monkeypatch.setattr(kot, "_seen", set())


# ---------------------------------------------------------------------------
# Observability-only guarantee: output identical with tracing on/off/broken
# ---------------------------------------------------------------------------


def test_guarded_text_byte_identical_without_client(no_client):
    assert kot.guarded_text(SAMPLE, surface="task_body", task_id="t_x") == apply_render_guards(SAMPLE)


def test_guarded_text_byte_identical_with_client(fake_client):
    assert kot.guarded_text(SAMPLE, surface="task_body", task_id="t_x") == apply_render_guards(SAMPLE)


def test_guarded_text_none_and_empty_pass_through(fake_client):
    assert kot.guarded_text(None, surface="task_body") is None
    assert kot.guarded_text("", surface="task_body") == ""


def test_tracing_failure_cannot_break_guarded_text(monkeypatch):
    monkeypatch.setattr(kot, "_client", _FakeClient(explode=True))
    monkeypatch.setattr(kot, "_seen", set())
    assert kot.guarded_text(SAMPLE, surface="task_body", task_id="t_x") == apply_render_guards(SAMPLE)


# ---------------------------------------------------------------------------
# Trace shape: root per surface, stage spans, complete final output
# ---------------------------------------------------------------------------


def test_guarded_text_emits_root_stage_and_final_spans(fake_client):
    kot.guarded_text(SAMPLE, surface="task_body", task_id="t_x")
    assert len(fake_client.roots) == 1
    root = fake_client.roots[0]
    assert root.name == "kanban.output.task_body"
    assert root.kwargs["trace_context"]["session_id"] == "kanban-task-t_x"
    assert root.kwargs["metadata"]["pipeline"] == kot.PIPELINE_VERSION
    stage_names = [c.name for c in root.children]
    assert stage_names == ["stage.recompose", "stage.hash_truncate", "stage.row_expansion",
                           "stage.task_id_expansion", "stage.path_labeling",
                           "stage.chrome_strip", "final_output"]
    final = root.children[-1]
    assert final.kwargs["output"] == apply_render_guards(SAMPLE)
    assert final.kwargs["metadata"]["complete_output"] is True
    assert all(c.ended for c in root.children)
    assert root.ended


def test_stage_spans_carry_progressive_outputs(fake_client):
    kot.guarded_text(SAMPLE, surface="task_body", task_id="t_x")
    stages = fake_client.roots[0].children[:-1]
    assert all(isinstance(c.kwargs["output"], str) for c in stages)
    assert stages[-1].kwargs["output"] == fake_client.roots[0].children[-1].kwargs["output"]


def test_repeat_emission_deduped_changed_content_retraces(fake_client):
    kot.guarded_text(SAMPLE, surface="task_body", task_id="t_x")
    kot.guarded_text(SAMPLE, surface="task_body", task_id="t_x")
    assert len(fake_client.roots) == 1
    kot.guarded_text(SAMPLE + "\n\nNOTES / PARKED\n- changed\n", surface="task_body", task_id="t_x")
    assert len(fake_client.roots) == 2


def test_record_emission_counts_only(fake_client):
    kot.record_emission(surface="activity_dedup", task_id="t_x", counts={"events_in": 3, "events_out": 2})
    root = fake_client.roots[0]
    assert root.name == "kanban.output.activity_dedup"
    final = root.children[-1]
    assert final.name == "final_output"
    assert final.kwargs["output"] == {"events_in": 3, "events_out": 2}


def test_record_emission_empty_is_silent(fake_client):
    kot.record_emission(surface="card_preview", task_id="t_x", output=None, counts=None)
    assert fake_client.roots == []


def test_recent_trace_ids_recorded(fake_client):
    kot.guarded_text(SAMPLE, surface="task_body", task_id="t_x")
    ids = kot.recent_trace_ids()
    assert ids and ids[0][0] == "task_body"
    assert fake_client.roots[0].kwargs["trace_context"]["trace_id"] == ids[0][1]


# ---------------------------------------------------------------------------
# Client lifecycle: fail-open without SDK or credentials
# ---------------------------------------------------------------------------


def test_missing_sdk_and_credentials_fail_open(monkeypatch):
    monkeypatch.setattr(kot, "Langfuse", None)
    monkeypatch.setattr(kot, "_client", None)
    monkeypatch.setattr(kot, "_secret", lambda name: "")
    assert kot._get_client() is None


def test_placeholder_prefix_credentials_rejected(monkeypatch):
    def _boom(**kwargs):
        raise AssertionError("Langfuse must not be constructed with placeholder keys")

    monkeypatch.setattr(kot, "Langfuse", _boom)
    monkeypatch.setattr(kot, "_client", None)
    creds = {"HERMES_LANGFUSE_PUBLIC_KEY": "placeholder", "HERMES_LANGFUSE_SECRET_KEY": "placeholder"}
    monkeypatch.setattr(kot, "_secret", lambda name: creds.get(name, ""))
    assert kot._get_client() is None


def test_valid_credentials_build_client(monkeypatch):
    constructed = {}

    class _C:
        def __init__(self, **kwargs):
            constructed.update(kwargs)

    monkeypatch.setattr(kot, "Langfuse", _C)
    monkeypatch.setattr(kot, "_client", None)
    creds = {"HERMES_LANGFUSE_PUBLIC_KEY": "pk-lf-test", "HERMES_LANGFUSE_SECRET_KEY": "sk-lf-test",
             "LANGFUSE_HOST": "http://127.0.0.1:13000"}
    monkeypatch.setattr(kot, "_secret", lambda name: creds.get(name, ""))
    assert kot._get_client() is not None
    assert constructed["public_key"] == "pk-lf-test"
    assert constructed["base_url"] == "http://127.0.0.1:13000"


def test_flush_traces_uses_settled_client(fake_client):
    kot.flush_traces()
    assert fake_client.flushed


def test_flush_traces_never_builds(monkeypatch):
    monkeypatch.setattr(kot, "_client", kot._INIT_FAILED)
    kot.flush_traces()  # no raise, no build attempt
