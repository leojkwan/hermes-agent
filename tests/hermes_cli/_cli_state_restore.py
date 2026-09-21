"""Shared cli module-state snapshot/restore for stubbed-reload test files.

Several hermes_cli test files build a real HermesCLI by re-executing cli.py
via ``importlib.reload`` while every prompt_toolkit module is stubbed with a
MagicMock (the ``_make_cli()`` pattern). ``patch.dict(sys.modules, ...)``
restores ``sys.modules`` on exit, but the reload has already re-executed
cli.py top-to-bottom inside the stubbed context: the same ``cli`` module
object now carries MagicMock globals (``_pt_print``, ``_PT_ANSI``, ...) and a
rebuilt ``HermesCLI`` class. Nothing restores those bindings, so every later
test in the same process that renders through cli (e.g.
test_skill_collision_visibility's ``/help skills`` check) sees silently
empty output — the order-dependent failure class.

``snapshot_cli_module_state()`` / ``restore_cli_module_state()`` capture and
put back the exact state boundary the stubbed reload poisons. They
deliberately do not re-reload: a reload would mint new module/class objects
again instead of restoring the ones the rest of the process already holds
references to (the failure mode of the superseded reload-in-finally
approach — dangling commit 1077c00371).

tests/hermes_cli/conftest.py wires these into autouse fixtures for the
Tier-1 stubbed-reload files; see ``_CLI_STATE_RESTORE_FILES`` there.
"""

from __future__ import annotations

import sys

AGENT_SKILL_COMMANDS_GLOBALS = (
    "_skill_commands",
    "_skill_commands_platform",
    "_skill_commands_home",
    "_skill_commands_project",
)


def snapshot_cli_module_state():
    """Snapshot the process-global state the stubbed cli reload poisons."""
    import tools.skills_tool as skills_tool
    import agent.skill_commands as agent_skill_commands

    import cli as cli_mod

    saved = {
        "cli_mod": cli_mod,
        "cli_dict": dict(cli_mod.__dict__),
        "cli_in_sys_modules": sys.modules.get("cli"),
    }
    hermes_cls = cli_mod.__dict__.get("HermesCLI")
    saved["hermes_cls"] = hermes_cls
    if hermes_cls is not None:
        saved["hermes_cls_dict"] = dict(hermes_cls.__dict__)
    saved["skills_cache_obj"] = skills_tool._SKILLS_CACHE
    saved["skills_cache_items"] = dict(skills_tool._SKILLS_CACHE)
    saved["agent_skill_commands"] = {
        name: getattr(agent_skill_commands, name, None)
        for name in AGENT_SKILL_COMMANDS_GLOBALS
    }
    return saved


def restore_cli_module_state(saved):
    """Restore what :func:`snapshot_cli_module_state` captured, in place."""
    import agent.skill_commands as agent_skill_commands

    # (a) sys.modules entry + the cli module attribute dict. The stubbed
    # reload rebinds ~20 module globals (and rebuilds the classes they close
    # over); restore the snapshot in place so the rest of the process keeps
    # using the original module objects rather than a re-minted reload.
    cli_mod = saved["cli_mod"]
    if sys.modules.get("cli") is not saved["cli_in_sys_modules"]:
        if saved["cli_in_sys_modules"] is None:
            sys.modules.pop("cli", None)
        else:
            sys.modules["cli"] = saved["cli_in_sys_modules"]
    cli_mod.__dict__.clear()
    cli_mod.__dict__.update(saved["cli_dict"])
    # (b) HermesCLI.__dict__['finalize_preloaded_skills'] and any other
    # attribute the stubbed reload (or a test) rebinds in place on the
    # original class. ``cli.__dict__`` restore above already rebinds the
    # ``HermesCLI`` name itself; this restores the class body.
    hermes_cls = saved["hermes_cls"]
    if hermes_cls is not None:
        for name, value in saved["hermes_cls_dict"].items():
            if hermes_cls.__dict__.get(name) is not value:
                setattr(hermes_cls, name, value)
    # (c) shared skill caches. cli._skill_commands/_skill_bundles ride on the
    # cli.__dict__ restore; the skills-tool cache may have been re-keyed to a
    # tmp HERMES_HOME mid-test (tests monkeypatch the name, monkeypatch
    # restores the dict object itself — restore its contents here), and
    # agent.skill_commands keys its module-level scan cache the same way.
    cache = saved["skills_cache_obj"]
    cache.clear()
    cache.update(saved["skills_cache_items"])
    for name, value in saved["agent_skill_commands"].items():
        setattr(agent_skill_commands, name, value)
