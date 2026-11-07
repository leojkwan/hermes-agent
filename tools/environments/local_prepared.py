"""Ephemeral local launch inputs shared by admission and the actual child."""

from __future__ import annotations

import os
import logging
import shutil
import tempfile
import threading
import time
from dataclasses import dataclass, field
from types import MappingProxyType
from collections.abc import Mapping

from tools.environments.base_session_env import (
    _snapshot_bootstrap_script,
    _wrap_command_script,
)
from tools.environments.local_execution_context import (
    ExecutionContext,
    _snapshot_bytes,
    prepared_execution_context,
)

PREPARATION_ERROR = (
    "Hermes could not prepare the saved Bash environment; command was not started. "
    "Retry after the session snapshot or temporary directory becomes available."
)

logger = logging.getLogger(__name__)


def _remove_launch_directory(directory):
    try:
        shutil.rmtree(directory)
    except FileNotFoundError:
        pass
    except OSError:
        logger.warning("Could not remove prepared local launch directory")


@dataclass(repr=False)
class _LaunchLifetime:
    directory: str | None
    lock: threading.Lock = field(default_factory=threading.Lock)
    consumed: bool = False
    closed: bool = False
    process: object = None

    def consume(self):
        with self.lock:
            if self.closed or self.consumed:
                raise RuntimeError("Prepared local launch already consumed or closed")
            self.consumed = True

    def close(self):
        with self.lock:
            if not self.closed:
                self.closed = True
                if self.directory is not None:
                    _remove_launch_directory(self.directory)

    def close_if_finished(self):
        if self.process is not None:
            poll = getattr(self.process, "poll", None)
            alive = getattr(self.process, "isalive", None)
            try:
                if poll is not None:
                    finished = poll() is not None
                elif alive is not None:
                    finished = not alive()
                else:
                    return
            except Exception:
                logger.exception("Could not determine prepared local child exit")
                return
            if finished:
                self.close()


@dataclass(frozen=True)
class PreparedLocalLaunch:
    bash: str
    cwd: str = field(repr=False)
    spawn_env: Mapping[str, str] = field(repr=False)
    snapshot: bytes = field(repr=False)
    snapshot_path: str = field(repr=False)
    context: ExecutionContext = field(repr=False)
    command: str = field(repr=False)
    script: str = field(repr=False)
    stdin_data: str | None = field(repr=False)
    deadline: float
    background: bool
    publishes: bool
    environment_recreated: bool
    state_not_persisted: bool
    _owner: object = field(repr=False, compare=False)
    _lifetime: _LaunchLifetime = field(repr=False, compare=False)

    def remaining(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError(PREPARATION_ERROR)
        return remaining

    def consume(self):
        self.remaining()
        self._lifetime.consume()

    def attach(self, process):
        self._lifetime.process = process
        process._hermes_prepared_launch = self

    def close(self):
        self._lifetime.close()

    def close_if_finished(self):
        self._lifetime.close_if_finished()


def recover_local_snapshot(env, deadline):
    """Recreate lost state before admission. Never run or replay a user command."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise RuntimeError(PREPARATION_ERROR)
    bootstrap = _snapshot_bootstrap_script(
        excluded_names=env._snapshot_excluded_passthrough_names(strict=True),
        **env._snapshot_script_kwargs(env.cwd),
    )
    proc = env._run_bash(bootstrap, login=True, timeout=remaining)
    try:
        result = env._wait_for_process(
            proc, timeout=max(0.001, deadline - time.monotonic())
        )
        if result.get("returncode") != 0 or time.monotonic() >= deadline:
            raise RuntimeError(PREPARATION_ERROR)
        env._snapshot_ready = True
        env._snapshot_recovery_pending = None
        env._update_cwd(result)
    finally:
        if proc.poll() is None:
            env._kill_process(proc)


def _read_or_recover(env, deadline):
    try:
        if env._snapshot_ready or env._snapshot_recovery_pending == "unavailable":
            snapshot = _snapshot_bytes(env._snapshot_path)
            env._snapshot_ready = True
            env._snapshot_recovery_pending = None
            return snapshot, False
    except FileNotFoundError:
        pass
    if not _publisher_available(env):
        raise RuntimeError(PREPARATION_ERROR)
    env._snapshot_ready = False
    env._snapshot_recovery_pending = "missing"
    env._recover_local_snapshot(deadline)
    return _snapshot_bytes(env._snapshot_path), True


def _publisher_available(env):
    previous = env._snapshot_publisher
    if previous is None:
        return True
    previous.close_if_finished()
    return previous.closed


def _prepare(env, command, cwd, deadline, background):
    from tools.environments import local
    from tools.terminal_tool_sudo import _rewrite_compound_background

    if not env._execution_context_lock.acquire(
        timeout=max(0, deadline - time.monotonic())
    ):
        raise RuntimeError(PREPARATION_ERROR)
    directory = None
    try:
        snapshot, recreated = _read_or_recover(env, deadline)
        excluded = env._snapshot_excluded_passthrough_names(strict=True)
        spawn = local._make_run_env(env.env)
        if background:
            spawn["PYTHONUNBUFFERED"] = "1"
            if local.platform.system() == "Linux":
                from tools.process_registry import systemd_user_bus_env

                spawn = systemd_user_bus_env(spawn)
        bash = local._find_bash()
        if not os.path.isabs(bash):
            raise RuntimeError(PREPARATION_ERROR)
        effective_cwd = local._resolve_safe_cwd(cwd or env.cwd)
        executed, stdin = env._prepare_command(command)
        executed = _rewrite_compound_background(executed)
        context = prepared_execution_context(snapshot, spawn, excluded)
        directory = tempfile.mkdtemp(prefix="hermes-launch-", dir=env.get_temp_dir())
        private = os.path.join(directory, "state.sh")
        with open(private, "xb") as stream:
            os.chmod(private, 0o600)
            stream.write(snapshot)
        # Frozen state lives outside the shared snapshot's .tmp.* cleanup namespace.
        publishes = not background and env._persist_env and _publisher_available(env)
        kwargs = env._snapshot_script_kwargs(effective_cwd)
        publication_path = kwargs["quoted_snap"]
        if background:
            kwargs["cwd_marker"] = ""
        kwargs["quoted_snap"] = env._quote_shell_path(private)
        kwargs["snap_tmp_template"] = env._quote_shell_path(
            os.path.join(directory, "restore.XXXXXXXXXX")
        )
        script = _wrap_command_script(
            executed,
            snapshot_ready=True,
            persist_env=publishes,
            passthrough_names=excluded,
            restore_marker=env._restore_marker,
            pipe_marker="" if background else env._pipe_marker,
            quoted_publish_snap=publication_path,
            **kwargs,
        )
        launch = PreparedLocalLaunch(
            bash,
            effective_cwd,
            MappingProxyType(spawn),
            snapshot,
            private,
            context,
            executed,
            script,
            stdin,
            deadline,
            background,
            publishes,
            recreated,
            not background and env._persist_env and not publishes,
            env,
            _LaunchLifetime(directory),
        )
        launch.remaining()
        if publishes:
            env._snapshot_publisher = launch._lifetime
        return launch
    except Exception:
        if directory is not None:
            _remove_launch_directory(directory)
        raise
    finally:
        env._execution_context_lock.release()


def prepare_local_launch(env, command, cwd="", *, timeout=None, background=False):
    from agent.deadline import run_bounded_sync

    deadline = time.monotonic() + float(timeout if timeout is not None else env.timeout)
    cancelled = threading.Event()
    holder = []

    def prepare():
        try:
            launch = _prepare(env, command, cwd, deadline, background)
            holder.append(launch)
            if cancelled.is_set():
                launch.close()
                raise RuntimeError(PREPARATION_ERROR)
            return launch
        except (OSError, RuntimeError, ValueError, ImportError, TypeError, LookupError):
            raise RuntimeError(PREPARATION_ERROR) from None

    bounded = run_bounded_sync(
        prepare, max(0.001, deadline - time.monotonic()), label="terminal.prepare-local"
    )
    if bounded.timed_out:
        cancelled.set()
        if holder:
            holder[0].close()
        raise RuntimeError(PREPARATION_ERROR)
    return bounded.value


def execute_prepared(env, launch, **kwargs):
    if launch._owner is not env or launch.background:
        raise RuntimeError("Prepared local launch has a different owner or route")
    launch.consume()
    try:
        from tools.environments.base import BaseEnvironment

        return BaseEnvironment.execute(
            env,
            launch.command,
            cwd=launch.cwd,
            timeout=launch.remaining(),
            _prepared_launch=launch,
            **kwargs,
        )
    finally:
        if launch._lifetime.process is None:
            launch.close()
        else:
            launch.close_if_finished()


def prepare_raw_snapshot(env, timeout):
    """Keep raw internal callers' launch API, with recovery inside their deadline."""
    from agent.deadline import run_bounded_sync

    deadline = time.monotonic() + float(timeout)
    if env._snapshot_recovery_pending == "missing":
        bounded = run_bounded_sync(
            lambda: env._recover_local_snapshot(deadline),
            timeout,
            label="terminal.recover-local",
        )
        if bounded.timed_out:
            raise RuntimeError(PREPARATION_ERROR)
        env._recreated_notice_pending = "local_snapshot"
    else:
        # Retry the saved state, never a bare shell. A failed restore still exits
        # before the user command and leaves the original snapshot in place.
        env._snapshot_ready = True
    env._snapshot_recovery_pending = None
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise RuntimeError(PREPARATION_ERROR)
    return remaining


def claim_raw_publication(env):
    """Raw internal commands participate in the same single-publisher lifetime."""
    with env._execution_context_lock:
        if (
            not env._snapshot_ready
            or not env._persist_env
            or not _publisher_available(env)
        ):
            return None
        lifetime = _LaunchLifetime(None)
        env._snapshot_publisher = lifetime
        return lifetime
