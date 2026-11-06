"""Recover snapshot ownership only after a launch has stopped using its old facts."""

from tools.environments.base_session_env import _split_cwd_marker


def invalidate_snapshot(env, reason):
    env._snapshot_ready = False
    env._snapshot_recovery_pending = reason


def _restore_failure(env, result):
    if not env._restore_marker:
        return None
    split = _split_cwd_marker(result.get("output", ""), env._restore_marker)
    if split is None:
        return None
    reason, result["output"] = split
    return reason if reason in {"missing", "unavailable"} else "unavailable"


def finish_snapshot_restore(env, result):
    """Record restore failure without any post-return probe or command replay."""
    failure = _restore_failure(env, result)
    if failure:
        invalidate_snapshot(env, failure)
        result["snapshot_recovery"] = "pending"
        return
