"""Capture modes omit unused driver work without losing snapshot or target identity."""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.computer_use.cua_backend import CuaDriverBackend
from tools.computer_use.cua_backend_session import _CuaDriverSession


PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAgAAAAICAYAAADED76LAAAADUlEQVR4nG"
    "NgGAUgAAABCAABgukLHQAAAABJRU5ErkJggg=="
)
PIXELS = base64.b64encode(PNG).decode("ascii")
TARGET = {"app_name": "Fixture", "pid": 607, "window_id": 382,
          "title": "Fixture window", "is_on_screen": True, "z_index": 1}
CONTROLS = {"include_accessibility_tree", "include_screenshot"}


class _WireDriver:
    """Defaults to both operations; screenshot_out_file overrides screenshot omission."""

    def __init__(self, properties, *, standalone=False, cli=False):
        self.properties = properties
        self.standalone, self.cli = standalone, cli
        self.calls = []
        self.ax_walks = self.screenshots = 0

    async def list_tools(self):
        schemas = {"list_windows": {}, "get_window_state": self.properties}
        if self.standalone:
            schemas["screenshot"] = {"properties": {}}
        return SimpleNamespace(tools=[SimpleNamespace(name=name, inputSchema=schema)
                                     for name, schema in schemas.items()])

    def execute(self, name, args, transport) -> dict:
        self.calls.append((name, dict(args), transport))
        if name == "list_windows":
            return {"windows": [dict(TARGET)]}
        if name == "screenshot":
            assert self.standalone
            assert args == {"window_id": TARGET["window_id"], "format": "jpeg",
                            "quality": 85, "session": "fixture-session"}
            self.screenshots += 1
            return {"screenshot_png_b64": PIXELS}
        assert name == "get_window_state"
        advertised = (self.properties or {}).get("properties", {})
        assert not (CONTROLS.intersection(args) - advertised.keys())
        assert {key: args[key] for key in ("pid", "window_id", "session", "max_elements")} == {
            "pid": TARGET["pid"], "window_id": TARGET["window_id"],
            "session": "fixture-session", "max_elements": 37,
        }
        # Degenerate MCP responses exercise the actual capture re-fetch and CLI file transport.
        if self.cli and transport == "mcp":
            return {}
        result = {"window_title": TARGET["title"]}
        if args.get("include_accessibility_tree", True):
            self.ax_walks += 1
            result.update(tree_markdown=f'AXWindow "{TARGET["title"]}"\n- [1] AXButton "OK"',
                          element_count=1, elements=[{
                              "element_index": 1, "role": "AXButton", "label": "OK",
                              "frame": {"x": 10, "y": 20, "w": 80, "h": 30},
                              "element_token": f"snapshot-{self.ax_walks}",
                          }])
        if args.get("include_screenshot", True) or args.get("screenshot_out_file"):
            self.screenshots += 1
            if path := args.get("screenshot_out_file"):
                Path(path).write_bytes(PNG)
                result["screenshot_file_path"] = path
            else:
                result["screenshot_png_b64"] = PIXELS
        return result

    async def call_tool(self, name, args):
        body = self.execute(name, args, "mcp")
        content = []
        if tree := body.get("tree_markdown"):
            content.append(SimpleNamespace(type="text", text=f'{body["element_count"]} elements\n{tree}'))
        if pixels := body.get("screenshot_png_b64"):
            content.append(SimpleNamespace(type="image", data=pixels, mimeType="image/png"))
        return SimpleNamespace(content=content, structuredContent=body, isError=False)


class _Bridge:
    def run(self, coro, timeout=None):
        return asyncio.run(coro)


def _backend(monkeypatch, tmp_path, driver):
    (tmp_path / "config.yaml").write_text("computer_use:\n  ax_max_elements: 37\n")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    session = _CuaDriverSession(_Bridge())
    asyncio.run(session._populate_capabilities(driver))
    session._session, session._started = driver, True
    backend = CuaDriverBackend.__new__(CuaDriverBackend)
    backend._clear_active_target()
    backend._session, backend._session_id = session, "fixture-session"
    if driver.cli:
        # Resolve subprocess PATH only inside the fixture, with the real-home guard enabled.
        monkeypatch.setenv("PATH", str(tmp_path))
        monkeypatch.setattr("tools.computer_use.cua_backend_driver.resolve_cua_driver_cmd",
                            lambda: "/fixture/cua-driver")
        monkeypatch.setattr("tools.computer_use.cua_backend.cua_driver_child_env", lambda: {})
        # Real CLI command construction, JSON decoding and screenshot file read/cleanup;
        # only the subprocess boundary is substituted, so no real GUI process can run.
        def run_cli(cmd, **kwargs):
            assert cmd[:2] == ["/fixture/cua-driver", "call"]
            body = driver.execute(cmd[2], json.loads(cmd[3]), "cli")
            return SimpleNamespace(stdout=json.dumps(body), stderr="", returncode=0)
        monkeypatch.setattr("subprocess.run", run_cli)
        monkeypatch.setattr("tempfile.tempdir", str(tmp_path))
    return backend


@pytest.mark.parametrize("transport", ["mcp", "cli"])
@pytest.mark.parametrize("controls", [CONTROLS, {"include_accessibility_tree"},
                                      {"include_screenshot"}, set(), None],
                         ids=["both", "tree-control", "pixel-control", "old-schema", "missing-schema"])
def test_capture_modes_request_only_needed_work(monkeypatch, tmp_path, transport, controls):
    schema = {"properties": {key: {"type": "boolean"} for key in controls}} if controls is not None else None
    driver = _WireDriver(schema, cli=transport == "cli")
    backend = _backend(monkeypatch, tmp_path, driver)
    supported = controls or set()
    for mode, expected_work in [
        ("som", (1, 1)),
        ("vision", (int("include_accessibility_tree" not in supported), 1)),
        ("ax", (1, int("include_screenshot" not in supported))),
        ("som", (1, 1)),
    ]:
        before = driver.ax_walks, driver.screenshots
        cap = backend.capture(mode=mode, app="Fixture")
        assert (driver.ax_walks - before[0], driver.screenshots - before[1]) == expected_work
        assert cap.mode == mode and cap.app == TARGET["app_name"]
        assert cap.window_title == TARGET["title"]
        assert backend._last_target == {"pid": TARGET["pid"], "window_id": TARGET["window_id"]}
        assert cap.ax_max_elements == (0 if mode == "vision" else 37)
        if expected_work[1]:
            assert cap.png_b64 == PIXELS
            assert cap.image_mime_type == ("image/png" if transport == "mcp" or mode == "vision" else None)
            assert (cap.width, cap.height, cap.png_bytes_len) == (8, 8, len(PNG))
        else:
            assert cap.png_b64 is None and cap.png_bytes_len == 0
        if mode == "vision":
            assert cap.elements == [] and backend._snapshot_tokens == {}
        else:
            assert len(cap.elements) == 1
            element = cap.elements[0]
            assert (element.index, element.role, element.label, element.bounds) == (1, "AXButton", "OK", (10, 20, 80, 30))
            assert element.element_token == f"snapshot-{driver.ax_walks}"
            assert backend._snapshot_tokens == {1: element.element_token}
    cli_args = [args for name, args, via in driver.calls if name == "get_window_state" and via == "cli"]
    for args in cli_args:
        if args.get("include_screenshot") is False:
            assert "screenshot_out_file" not in args
        else:
            assert "screenshot_out_file" in args
            assert not Path(args["screenshot_out_file"]).exists()


def test_standalone_screenshot_keeps_older_driver_path_and_clears_tokens(monkeypatch, tmp_path):
    driver = _WireDriver({}, standalone=True)
    backend = _backend(monkeypatch, tmp_path, driver)
    backend.capture(mode="som", app="Fixture")
    assert backend._snapshot_tokens
    before = len(driver.calls)
    cap = backend.capture(mode="vision", app="Fixture")
    assert [name for name, _, _ in driver.calls[before:]] == ["list_windows", "screenshot"]
    assert cap.png_b64 == PIXELS and (cap.width, cap.height) == (8, 8)
    assert cap.app == "Fixture" and cap.elements == []
    assert backend._snapshot_tokens == {}


def test_cli_respects_explicit_screenshot_omission(monkeypatch, tmp_path):
    driver = _WireDriver({"properties": {key: {} for key in CONTROLS}}, cli=True)
    backend = _backend(monkeypatch, tmp_path, driver)
    args = {"pid": TARGET["pid"], "window_id": TARGET["window_id"],
            "session": "fixture-session", "max_elements": 37, "include_screenshot": False}
    out = backend._session._call_tool_via_cli("get_window_state", args, 30.0)
    assert driver.ax_walks == 1 and driver.screenshots == 0
    assert out["images"] == []
    assert out["structuredContent"]["elements"][0]["element_token"] == "snapshot-1"
    assert "screenshot_out_file" not in driver.calls[-1][1]
    assert "screenshot_out_file" not in args


def test_empty_ax_snapshot_is_not_repeated_or_reuses_old_tokens(monkeypatch, tmp_path):
    driver = _WireDriver({"properties": {key: {} for key in CONTROLS}})
    backend = _backend(monkeypatch, tmp_path, driver)
    backend.capture(mode="som", app="Fixture")
    assert backend._snapshot_tokens
    execute = driver.execute

    def empty_snapshot(name, args, transport):
        result = execute(name, args, transport)
        if name == "get_window_state":
            result.pop("tree_markdown", None)
            result.update(elements=[], element_count=0)
        return result

    monkeypatch.setattr(driver, "execute", empty_snapshot)
    before = len(driver.calls)
    cap = backend.capture(mode="ax", app="Fixture")
    requests = [call for call in driver.calls[before:] if call[0] == "get_window_state"]
    assert len(requests) == 1
    assert requests[0][2] == "mcp"
    assert cap.png_b64 is None and cap.elements == []
    assert cap.window_title == TARGET["title"]
    assert backend._snapshot_tokens == {}


@pytest.mark.parametrize("metadata", [
    {"window_title": {"unexpected": "object"}},
    {"window_title": 7},
    [1],
])
def test_vision_ignores_malformed_optional_title_with_usable_pixels(monkeypatch, tmp_path, metadata):
    driver = _WireDriver({"properties": {key: {} for key in CONTROLS}})
    backend = _backend(monkeypatch, tmp_path, driver)
    call_tool = driver.call_tool

    async def malformed_title(name, args):
        result = await call_tool(name, args)
        if name == "get_window_state":
            result.structuredContent = metadata
        return result

    monkeypatch.setattr(driver, "call_tool", malformed_title)
    cap = backend.capture(mode="vision", app="Fixture")
    assert cap.png_b64 == PIXELS and (cap.width, cap.height) == (8, 8)
    assert isinstance(cap.window_title, str) and cap.window_title == TARGET["title"]
    assert cap.elements == [] and backend._snapshot_tokens == {}
    assert backend._last_target == {"pid": TARGET["pid"], "window_id": TARGET["window_id"]}
