"""A plugin that ships only a dashboard tab (``dashboard/manifest.json``, no ``__init__.py``) is complete
without Python. The agent loader must accept it instead of warning "No __init__.py" on every start."""

from __future__ import annotations

import json
import logging

import hermes_yaml as yaml

from hermes_cli.plugins import PluginManager


def _write_dashboard_only_plugin(home, name="conversations"):
    plugin = home / "plugins" / name
    (plugin / "dashboard" / "dist").mkdir(parents=True)
    manifest = {"name": name, "version": "0.1.0", "description": "Dashboard tab only.",
                "manifest_version": 1, "kind": "standalone", "provides_tools": [], "provides_hooks": []}
    (plugin / "plugin.yaml").write_text(json.dumps(manifest), encoding="utf-8")
    (plugin / "dashboard" / "manifest.json").write_text(json.dumps({"name": name, "label": "Chats"}),
                                                        encoding="utf-8")
    (home / "config.yaml").write_text(yaml.safe_dump({"plugins": {"enabled": [name]}}), encoding="utf-8")


def test_dashboard_only_plugin_loads_without_init_warning(tmp_path, monkeypatch, caplog):
    from hermes_cli import plugins as plugins_mod

    home = tmp_path / "home"
    home.mkdir()
    empty_bundled = tmp_path / "bundled"
    empty_bundled.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "os-home"))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(plugins_mod, "get_bundled_plugins_dir", lambda: empty_bundled)
    _write_dashboard_only_plugin(home)

    manager = PluginManager()
    with caplog.at_level(logging.WARNING):
        manager.discover_and_load()

    loaded = manager._plugins["conversations"]
    assert loaded.enabled, loaded.error
    assert "No __init__.py" not in caplog.text
