from __future__ import annotations

import importlib
import os
import sys
from unittest.mock import MagicMock, patch

import pytest


def _make_real_cli(**kwargs):
    clean_config = {
        "model": {
            "default": "anthropic/claude-opus-4.6",
            "base_url": "https://openrouter.ai/api/v1",
            "provider": "auto",
        },
        "display": {"compact": False, "tool_progress": "all"},
        "agent": {},
        "terminal": {"env_type": "local"},
    }
    clean_env = {"LLM_MODEL": "", "HERMES_MAX_ITERATIONS": ""}
    prompt_toolkit_stubs = {
        "prompt_toolkit": MagicMock(),
        "prompt_toolkit.history": MagicMock(),
        "prompt_toolkit.styles": MagicMock(),
        "prompt_toolkit.patch_stdout": MagicMock(),
        "prompt_toolkit.application": MagicMock(),
        "prompt_toolkit.layout": MagicMock(),
        "prompt_toolkit.layout.processors": MagicMock(),
        "prompt_toolkit.filters": MagicMock(),
        "prompt_toolkit.layout.dimension": MagicMock(),
        "prompt_toolkit.layout.menus": MagicMock(),
        "prompt_toolkit.widgets": MagicMock(),
        "prompt_toolkit.key_binding": MagicMock(),
        "prompt_toolkit.completion": MagicMock(),
        "prompt_toolkit.formatted_text": MagicMock(),
    }
    with patch.dict(sys.modules, prompt_toolkit_stubs), patch.dict(
        "os.environ", clean_env, clear=False
    ):
        import cli as cli_mod

        cli_mod = importlib.reload(cli_mod)
        with patch.object(cli_mod, "get_tool_definitions", return_value=[]), patch.dict(
            cli_mod.__dict__, {"CLI_CONFIG": clean_config}
        ):
            return cli_mod.HermesCLI(**kwargs)


class _DummyCLI:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.session_id = "session-123"
        self.system_prompt = "base prompt"
        self.preloaded_skills = []

    def show_banner(self):
        return None

    def show_tools(self):
        return None

    def show_toolsets(self):
        return None

    def run(self):
        return None


def _real_finalize(cli_obj):
    """Call the real HermesCLI.finalize_preloaded_skills on a dummy object."""
    return _REAL_FINALIZE(cli_obj)


def _capture_real_finalize():
    import cli as cli_mod
    return cli_mod.HermesCLI.__dict__["finalize_preloaded_skills"]


_REAL_FINALIZE = _capture_real_finalize()


# ---- cli module state isolation (the leak boundary) ----
# ``_make_real_cli()`` below re-executes cli.py via ``importlib.reload`` while
# every prompt_toolkit module is stubbed with a MagicMock. ``patch.dict``
# restores ``sys.modules`` on exit, but the reload has already re-executed
# cli.py top-to-bottom inside the stubbed context: the same ``cli`` module
# object now carries MagicMock globals (``_pt_print``, ``_PT_ANSI``, …) and a
# rebuilt ``HermesCLI`` class. Nothing restores those bindings, so every later
# test in the same process that renders through cli (e.g.
# test_skill_collision_visibility's ``/help skills`` check) sees silently
# empty output — the order-dependent failure.
#
# This fixture snapshots that exact state boundary before each test in this
# file and puts the ORIGINAL objects back afterwards. It deliberately does
# not re-reload: a reload would mint new module/class objects again instead
# of restoring the ones the rest of the process already holds references to.

_AGENT_SKILL_COMMANDS_GLOBALS = (
    "_skill_commands",
    "_skill_commands_platform",
    "_skill_commands_home",
    "_skill_commands_project",
)


@pytest.fixture(autouse=True)
def _restore_cli_module_state():
    """Snapshot and restore the process-global state the stubbed reload poisons."""
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
        for name in _AGENT_SKILL_COMMANDS_GLOBALS
    }

    yield

    # (a) sys.modules entry + the cli module attribute dict. The stubbed
    # reload rebinds ~20 module globals (and rebuilds the classes they close
    # over); restore the snapshot in place so the rest of the process keeps
    # using the original module objects rather than a re-minted reload.
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


def test_main_applies_preloaded_skills_to_system_prompt(monkeypatch):
    import cli as cli_mod

    created = {}

    def fake_cli(**kwargs):
        created["cli"] = _DummyCLI(**kwargs)
        return created["cli"]

    monkeypatch.setattr(cli_mod, "HermesCLI", fake_cli)
    monkeypatch.setattr(
        cli_mod,
        "build_preloaded_skills_prompt",
        lambda skills, task_id=None, excluded_loaded_names=None: ("skill prompt", ["hermes-agent-dev", "github-auth"], []),
    )

    with pytest.raises(SystemExit):
        cli_mod.main(skills="hermes-agent-dev,github-auth", list_tools=True)

    cli_obj = created["cli"]
    # The preload now runs in a background thread and is folded in at agent
    # init via finalize_preloaded_skills() (startup-latency change). Drive
    # the finalize explicitly — the same call _init_agent makes.
    _real_finalize(cli_obj)
    assert cli_obj.system_prompt == "base prompt\n\nskill prompt"
    assert cli_obj.preloaded_skills == ["hermes-agent-dev", "github-auth"]


def test_main_raises_for_unknown_preloaded_skill(monkeypatch):
    import cli as cli_mod

    created = {}

    def fake_cli(**kwargs):
        created["cli"] = _DummyCLI(**kwargs)
        return created["cli"]

    monkeypatch.setattr(cli_mod, "HermesCLI", fake_cli)
    monkeypatch.setattr(
        cli_mod,
        "build_preloaded_skills_prompt",
        lambda skills, task_id=None, excluded_loaded_names=None: ("", [], ["missing-skill"]),
    )

    with pytest.raises(SystemExit):
        cli_mod.main(skills="missing-skill", list_tools=True)

    # The all-skills-unknown hard failure now surfaces when the preload is
    # finalized (agent init), preserving the fail-loud contract.
    with pytest.raises(ValueError, match=r"Unknown skill\(s\): missing-skill"):
        _real_finalize(created["cli"])


def test_show_banner_does_not_print_skills():
    """show_banner() no longer prints the activated skills line — it moved to run()."""
    cli_obj = _make_real_cli(compact=False)
    cli_obj.preloaded_skills = ["hermes-agent-dev", "github-auth"]
    cli_obj.console = MagicMock()

    with patch("hermes_cli.banner.build_welcome_banner") as mock_banner, patch(
        "shutil.get_terminal_size", return_value=os.terminal_size((120, 40))
    ):
        cli_obj.show_banner()

    print_calls = [
        call.args[0]
        for call in cli_obj.console.print.call_args_list
        if call.args and isinstance(call.args[0], str)
    ]
    startup_lines = [line for line in print_calls if "Activated skills:" in line]
    assert len(startup_lines) == 0
    assert mock_banner.call_count == 1
