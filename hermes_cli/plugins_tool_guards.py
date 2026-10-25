"""Plugin guards at the immutable registry-handler boundary."""

from __future__ import annotations

from contextlib import nullcontext
from copy import deepcopy
import inspect
import logging
from pathlib import Path
from threading import get_ident
from typing import Callable, TYPE_CHECKING

if TYPE_CHECKING:
    from hermes_cli.plugins_ledger import PluginRegistration

logger = logging.getLogger("hermes_cli.plugins")


class PluginCallbackContextMixin:
    def _track_callback(
        self, kind: str, key: str, callback: Callable, mapping: dict[str, list[Callable]],
        valid: set[str],
    ) -> PluginRegistration:
        """Append ``callback`` under ``key`` (warning on unknown ``key``) and lease its removal."""
        if key not in valid:
            logger.warning("Plugin '%s' registered unknown %s '%s' (valid: %s)", self.manifest.name, kind,
                           key, ", ".join(sorted(valid)))
        mapping.setdefault(key, []).append(callback)
        handle = self._track(kind, key, lambda: self._manager._remove_callback(mapping, key, callback))
        logger.debug("Plugin %s registered %s: %s", self.manifest.name, kind, key)
        return handle


def register_tool_guard(ctx, tool_name: str, callback: Callable) -> PluginRegistration:
    """Register a synchronous guard through the normal callback ownership ledger."""
    if not isinstance(tool_name, str) or not tool_name or not callable(callback):
        raise ctx._refuse("a tool guard without a tool name and callable")
    if inspect.iscoroutinefunction(callback) or inspect.iscoroutinefunction(callback.__call__):
        raise ctx._refuse("an async tool guard; guards must be synchronous")
    return ctx._track_callback(
        "tool_guard", tool_name, callback, ctx._manager._tool_guards, {tool_name}
    )


def run_tool_guards(tool_name, args, execute, *, registration, scope=None, **context):
    """Guard one captured handler without granting argument replacement or retries."""
    from agent.safe_worker_policy import safe_worker_enabled

    if safe_worker_enabled():
        return execute(args)
    from hermes_cli.plugins import _delivery_manager
    from hermes_cli.plugins_loader import _plugin_home_scope

    with _plugin_home_scope(Path(scope)) if scope is not None else nullcontext():
        manager = _delivery_manager()
        callbacks = tuple(manager._tool_guards.get(tool_name, ()))
        if not callbacks:
            return execute(args)
        fixed_args = deepcopy(args)
        owner_thread = get_ident()

        def call_at(index):
            if index == len(callbacks):
                return execute(fixed_args)
            active, called, violated = True, False, False

            def next_call():
                nonlocal called, violated
                if not active or called or get_ident() != owner_thread:
                    violated = True
                    raise RuntimeError("Tool guard next_call() is synchronous and single-use")
                called = True
                return call_at(index + 1)

            payload = {
                **context, "tool_name": tool_name, "args": deepcopy(fixed_args),
                "next_call": next_call, "registration": registration,
            }
            try:
                callback = callbacks[index]
                result = callback(**manager._hook_callback_kwargs(callback, payload))
                if violated:
                    raise RuntimeError("Tool guard next_call() contract was violated")
                return result
            finally:
                active = False

        return call_at(0)
