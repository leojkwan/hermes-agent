"""A ready side answer must not wait for memory shutdown, or release its store early."""

import contextlib
from pathlib import Path
import queue
import sys
import threading
from types import SimpleNamespace

import pytest


def test_background_answer_arrives_while_cleanup_holds_profile_store_and_admission(tmp_path, monkeypatch):
    import hermes_state_registry as registry
    from agent.secret_scope import get_secret, set_multiplex_active
    from hermes_cli import backend_retirement
    from hermes_constants import get_hermes_home
    from tui_gateway import server

    fence = backend_retirement.RetirementFence()
    monkeypatch.setattr(backend_retirement, "retirement", fence)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    homes = [tmp_path / "a", tmp_path / "b"]
    for home in homes:
        home.mkdir()
        (home / "config.yaml").write_text("terminal:\n  backend: local\n")
        (home / ".env").write_text(f"BACKGROUND_SCOPE={home.name}\n")
    emitted = queue.Queue()
    monkeypatch.setattr(server, "_emit", lambda *args: emitted.put(args))
    set_multiplex_active(True)
    try:
        for home in [homes[0], homes[1], homes[0]]:
            parent_db = registry.acquire(home / "state.db")
            closing, release, closed = (threading.Event() for _ in range(3))
            observations = []
            task_ids = []

            class FakeAgent:
                def __init__(self, **kwargs):
                    self.db = kwargs["session_db"]

                def run_conversation(self, *, task_id, **kwargs):
                    task_ids.append(task_id)
                    registry.release_or_close(parent_db)
                    self.db.create_session(task_id, source="tui", model="fixture")
                    return {"final_response": "ready answer"}

                def close(self):
                    observations.append((get_hermes_home(), get_secret("BACKGROUND_SCOPE"), self.db._conn is not None))
                    closing.set()
                    assert release.wait(10), "cleanup barrier timed out"
                    closed.set()

            session = {"agent": object(), "profile_home": str(home), "cwd": str(tmp_path)}
            monkeypatch.setattr(server, "_sess", lambda *_args: (session, None))
            monkeypatch.setattr(server, "_background_agent_kwargs", lambda *_args: {"session_db": parent_db})
            monkeypatch.setitem(sys.modules, "run_agent", SimpleNamespace(AIAgent=FakeAgent))
            try:
                result = server._methods["prompt.background"]("r", {"session_id": "parent", "text": "ask"})
                assert result["result"]["task_id"].startswith("bg_")
                assert closing.wait(10)
                assert not closed.is_set()
                # Synchronize on the real event while close is blocked, rather than a latency guess.
                event, owner, payload = emitted.get(timeout=2)
                assert (event, owner, payload) == (
                    "background.complete", "parent", {"task_id": task_ids[0], "text": "ready answer"})
                assert observations == [(home, home.name, True)]
                assert fence.prepare() == {"ok": False, "idle": False}
                assert registry.stats()["live_generations"] == 1
            finally:
                release.set()
                for thread in threading.enumerate():
                    if thread.name in {f"side-agent-{task_id}" for task_id in task_ids}:
                        thread.join(10)
                registry.release_or_close(parent_db)
            assert closed.is_set()
            assert len(observations) == 1
            assert registry.stats()["live_generations"] == 0
            token = fence.prepare()["token"]
            assert fence.cancel(token) == {"ok": True}
    finally:
        set_multiplex_active(False)
        registry.close_all()


@pytest.mark.parametrize("failure", ["constructor", "turn", "scope_reentry"])
def test_background_failure_closes_once_and_releases_its_store(tmp_path, monkeypatch, failure):
    import hermes_state_registry as registry
    from tui_gateway import server

    parent_db = registry.acquire(tmp_path / "state.db")
    closed = []
    emitted = []

    class FakeAgent:
        def __init__(self, **kwargs):
            self.db = kwargs["session_db"]
            if failure == "constructor":
                raise RuntimeError("constructor failed")

        def run_conversation(self, **kwargs):
            if failure == "scope_reentry":
                return {"final_response": "ready answer"}
            raise RuntimeError("turn failed")

        def close(self):
            closed.append(self.db._conn is not None)
            raise RuntimeError("cleanup failed")

    session = {"agent": object(), "profile_home": None, "cwd": str(tmp_path)}
    monkeypatch.setattr(server, "_sess", lambda *_args: (session, None))
    monkeypatch.setattr(server, "_background_agent_kwargs", lambda *_args: {"session_db": parent_db})
    monkeypatch.setitem(sys.modules, "run_agent", SimpleNamespace(AIAgent=FakeAgent))
    monkeypatch.setattr(server, "_start_session_work", lambda target, **kwargs: (target(), SimpleNamespace())[1])
    monkeypatch.setattr(server, "_emit", lambda *args: emitted.append(args))
    if failure == "scope_reentry":
        original_scope = server._session_profile_runtime_scope
        entered = []

        @contextlib.contextmanager
        def scope(session):
            entered.append(True)
            if len(entered) > 1:
                raise RuntimeError("profile scope cannot be reacquired")
            with original_scope(session):
                yield

        monkeypatch.setattr(server, "_session_profile_runtime_scope", scope)
    try:
        result = server._methods["prompt.background"]("r", {"session_id": "parent", "text": "ask"})
        assert result["result"]["task_id"].startswith("bg_")
        assert len(emitted) == 1
        assert emitted[0][0:2] == ("background.complete", "parent")
        assert emitted[0][2]["text"] == ("ready answer" if failure == "scope_reentry" else f"error: {failure} failed")
        assert closed == ([] if failure == "constructor" else [True])
        if failure == "scope_reentry":
            assert entered == [True]
    finally:
        registry.release_or_close(parent_db)
        registry.close_all()
    assert registry.stats()["live_generations"] == 0
