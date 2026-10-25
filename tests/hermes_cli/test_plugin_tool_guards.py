"""Final dispatch guards survive native refresh without owning the tool registration."""

import json
from textwrap import dedent

import pytest


TOOL = "mcp__guard_fixture__list_threads"
CURSOR = "complete-opaque-fixture-cursor"


def _plugin(home, marker):
    directory = home / "plugins" / "guard_probe"
    directory.mkdir(parents=True)
    (directory / "plugin.yaml").write_text("name: guard_probe\nversion: 0.1.0\n")
    (home / "config.yaml").write_text("plugins:\n  enabled: [guard_probe]\n")
    (directory / "__init__.py").write_text(dedent(f"""
        import json

        SEEN, TOKENS, SAVED = [], [], []
        MODE = 'allow'
        HANDLES = []

        def first(args, next_call, registration, **kwargs):
            TOKENS.append(registration)
            if MODE == 'raise':
                raise ValueError('guard failure')
            if MODE == 'retain':
                SAVED.append(next_call)
                return json.dumps({{'error': 'refused'}})
            args['nested']['value'] = 'changed by guard'
            result = next_call()
            if MODE == 'double':
                try:
                    next_call()
                except RuntimeError:
                    pass
            return result

        def validate(args, next_call, session_id='', **kwargs):
            SEEN.append((args, session_id))
            if args.get('cursor') not in (None, {CURSOR!r}):
                return json.dumps({{'error': {marker!r} + ': provider was NOT called'}})
            return next_call()

        def register(ctx):
            HANDLES.append(ctx.register_tool_guard({TOOL!r}, first))
            HANDLES.append(ctx.register_tool_guard({TOOL!r}, validate))
    """))


@pytest.fixture
def isolated_plugins(tmp_path, monkeypatch):
    from hermes_cli import plugins

    monkeypatch.setattr(plugins, "get_bundled_plugins_dir", lambda: tmp_path / "empty")
    monkeypatch.setattr(plugins.PluginManager, "_scan_entry_points", lambda self: [])
    return plugins


def test_guard_checks_refreshed_handler_and_cannot_mutate_or_repeat_dispatch(
    tmp_path, monkeypatch, isolated_plugins,
):
    import model_tools
    from tools.registry import registry

    home = tmp_path / "profile"
    _plugin(home, "profile")
    monkeypatch.setenv("HERMES_HOME", str(home))
    isolated_plugins.discover_plugins()
    manager = isolated_plugins.get_plugin_manager()
    module = manager._plugins["guard_probe"].module
    calls = []

    def provider(args, *, session_id=""):
        calls.append((args, session_id))
        return json.dumps({"threads": []})

    def register():
        registry.register(
            name=TOOL, toolset="mcp-guard_fixture",
            schema={"name": TOOL, "parameters": {"type": "object"}},
            handler=provider, execution_timeout=17, max_result_size_chars=12345,
            scope=str(home),
        )

    register()
    args = {"nested": {"value": "original"}, "cursor": CURSOR}
    result = model_tools.handle_function_call(TOOL, args, session_id="session")
    assert json.loads(result) == {"threads": []}
    assert calls == [(args, "session")]
    assert module.SEEN[-1] == (args, "session")
    token = module.TOKENS[-1]
    original_dispatch = registry.dispatch

    def refresh_after_pre_hook(name, final_args, **kwargs):
        register()
        return original_dispatch(name, final_args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(registry, "dispatch", refresh_after_pre_hook)
        refused = model_tools.handle_function_call(
            TOOL, {**args, "cursor": "truncated..."}, session_id="session")
    assert "NOT called" in refused
    assert len(calls) == 1
    assert module.TOKENS[-1] is not token
    entry = registry.get_entry(TOOL)
    assert entry.toolset == "mcp-guard_fixture"
    assert entry.execution_timeout == 17 and entry.max_result_size_chars == 12345

    module.MODE = "raise"
    assert "guard failure" in original_dispatch(TOOL, args)
    assert len(calls) == 1
    module.MODE = "double"
    assert "contract was violated" in original_dispatch(TOOL, args)
    assert len(calls) == 2
    module.MODE = "retain"
    assert "refused" in original_dispatch(TOOL, args)
    with pytest.raises(RuntimeError, match="single-use"):
        module.SAVED[-1]()
    assert len(calls) == 2
    registry.deregister(TOOL, scope=str(home))


def test_guards_follow_explicit_profile_and_dispose_without_changing_native_tools(
    tmp_path, monkeypatch, isolated_plugins,
):
    from hermes_constants import get_hermes_home, reset_hermes_home_override, set_hermes_home_override
    from tools.registry import registry

    homes = [tmp_path / name for name in ("one", "two")]
    modules, managers = [], []
    for home in homes:
        _plugin(home, home.name)
        token = set_hermes_home_override(home)
        try:
            isolated_plugins.discover_plugins()
            manager = isolated_plugins.get_plugin_manager()
            managers.append(manager)
            modules.append(manager._plugins["guard_probe"].module)
        finally:
            reset_hermes_home_override(token)

    monkeypatch.setenv("HERMES_HOME", str(homes[0]))
    calls = []

    async def provider(args):
        calls.append((args, get_hermes_home()))
        return json.dumps({"threads": []})

    for home in homes:
        registry.register(
            name=TOOL, toolset="mcp-guard_fixture",
            schema={"name": TOOL, "parameters": {"type": "object"}},
            handler=provider, is_async=True, scope=str(home),
        )
    args = {"nested": {"value": "original"}, "cursor": "bad..."}
    assert "one: provider was NOT called" in registry.dispatch(TOOL, args)
    assert "two: provider was NOT called" in registry.dispatch(TOOL, args, scope=str(homes[1]))
    assert not calls
    with monkeypatch.context() as patch:
        patch.setattr("agent.safe_worker_policy.safe_worker_enabled", lambda: True)
        assert json.loads(registry.dispatch(TOOL, args)) == {"threads": []}
    assert calls == [(args, homes[0])]
    calls.clear()
    entry = registry.get_entry(TOOL, scope=str(homes[1]))
    for handle in modules[1].HANDLES:
        handle.dispose()
        handle.dispose()
    assert registry.get_entry(TOOL, scope=str(homes[1])) is entry
    assert json.loads(registry.dispatch(TOOL, args, scope=str(homes[1]))) == {"threads": []}
    assert calls == [(args, homes[1])]
    managers[0].unload("guard_probe")
    assert not managers[0]._tool_guards
    assert json.loads(registry.dispatch(TOOL, args)) == {"threads": []}
    assert calls[-1] == (args, homes[0])
    for home in homes:
        registry.deregister(TOOL, scope=str(home))
