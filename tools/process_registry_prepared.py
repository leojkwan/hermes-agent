"""Prepared terminal launches reuse registry ownership, readers and routing."""

import os
import subprocess


def _try_pty(registry, session, launch):
    from tools.process_registry import _stop_systemd_unit

    try:
        return registry._spawn_local_pty(
            session, launch.script, launch.spawn_env, prepared_launch=launch
        )
    except Exception as error:
        if session._pty is not None:
            from tools.process_registry_ownership import refuse_pty_replay

            refuse_pty_replay(registry, session)
        if not isinstance(error, (ImportError, OSError, RuntimeError, ValueError)):
            raise
        session.pty_fallback = (
            "the PTY library (ptyprocess/pywinpty) is not installed"
            if isinstance(error, ImportError)
            else "the PTY spawn failed"
        )
        if session.systemd_unit:
            if not _stop_systemd_unit(session.systemd_unit):
                raise RuntimeError(
                    "PTY scope could not be reaped; refusing pipe fallback to avoid duplicate command execution"
                ) from None
            session.systemd_unit = ""
    return None


def spawn_prepared_local(
    registry,
    launch,
    *,
    task_id,
    session_key,
    owner_task_id,
    use_pty,
    persist_on_release,
):
    from tools import process_registry as runtime

    if not launch.background:
        raise RuntimeError("Foreground prepared launch must retain its original Popen")
    launch.consume()
    session = registry._new_session(
        launch.command,
        task_id,
        owner_task_id,
        session_key,
        launch.cwd,
        persist_on_release=persist_on_release,
    )
    session._prepared_launch = launch
    try:
        log = runtime.worker_output_log(session.id)
        if use_pty and launch.stdin_data is not None:
            session.pty_fallback = "prepared command requires pipe stdin"
        if use_pty and log is None and launch.stdin_data is None:
            ready = _try_pty(registry, session, launch)
            if ready is not None:
                return ready
        suffix = f"{session.id}-pipe-fallback" if session.pty_fallback else session.id
        script = (
            runtime.record_exit_command(log, launch.script) if log else launch.script
        )
        argv = registry._scope_argv(
            session, script, suffix, "Local", prepared_launch=launch
        )
        launch.remaining()
        output = runtime.open_output_log(log) if log else subprocess.PIPE
        try:
            proc = registry._spawn_job(
                session,
                argv,
                persist_on_release,
                text=True,
                cwd=launch.cwd,
                env=launch.spawn_env,
                encoding="utf-8",
                errors="replace",
                stdout=output,
                stderr=subprocess.STDOUT,
                stdin=subprocess.PIPE
                if launch.stdin_data is not None
                else subprocess.DEVNULL,
                **(
                    {"creationflags": runtime.windows_hide_flags()}
                    if runtime._IS_WINDOWS
                    else {}
                ),
            )
        finally:
            if log:
                os.close(output)
        launch.attach(proc)
        if launch.stdin_data is not None:
            from tools.environments.base_output import _pipe_stdin

            _pipe_stdin(proc, launch.stdin_data)
        session.process, session.output_log = proc, str(log or "")
        session.pid = proc.pid
        session.host_start_time = registry._safe_host_start_time(proc.pid)
        try:
            registry._track_started(
                session,
                registry._log_reader_loop if log else registry._reader_loop,
                f"proc-reader-{session.id}",
            )
        except Exception:
            registry._reap_untracked(session, proc)
            raise
        return session
    except Exception:
        if launch._lifetime.process is None:
            launch.close()
        else:
            launch.close_if_finished()
        raise
