"""Explicit execution attestations for pure source-scanner fixtures only."""

import json
from dataclasses import replace

from tools.environments.local_execution_context import EnvFact, ExecutionContext


def known_context(node_options=""):
    return ExecutionContext(
        EnvFact("absent") if node_options == "" else EnvFact("value", node_options),
        EnvFact("unknown", reason="fixture-requires-owned-npm-home"),
        EnvFact("unknown", reason="fixture-requires-owned-npm-path"),
        EnvFact("absent"),
        EnvFact("absent"),
        frozenset(),
        npm_path_safe=True,
        shell_startup_safe=True,
    )


def npm_context(tmp_path, node_options=""):
    """Static, never executable-in-tests npm/node fixtures with owned config paths."""
    prefix = tmp_path / ".fixture-tools"
    root = prefix / "lib/node_modules/npm"
    bin_dir = prefix / "bin"
    home = tmp_path / ".fixture-home"
    for directory in (root / "bin", bin_dir, home, prefix / "etc"):
        directory.mkdir(parents=True, exist_ok=True)
    files = {
        root / "package.json": json.dumps(
            {"name": "npm", "version": "11.19.0"}
        ).encode(),
        root / "bin/npm-cli.js": b"#!/usr/bin/env node\n// synthetic, never run\n",
        bin_dir / "node": b"\xcf\xfa\xed\xfeSYNTHETIC_NOT_EXECUTABLE_CODE",
    }
    for path, content in files.items():
        if not path.exists():
            path.write_bytes(content)
            path.chmod(0o700)
    npm = bin_dir / "npm"
    if not npm.is_symlink() and not npm.exists():
        npm.symlink_to(root / "bin/npm-cli.js")
    return replace(
        known_context(node_options),
        home=EnvFact("value", str(home)),
        path=EnvFact("value", str(bin_dir)),
    )
