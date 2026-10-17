"""The Electron child needs a socket-safe temp root, not a long CLI scratch path (#124688)."""

import argparse
import os
import secrets
import shutil
import socket
import tempfile
from pathlib import Path

import pytest

from hermes_cli import main_desktop


def _launch_env(tmp_path) -> dict:
    env, _flags = main_desktop._desktop_launch_env(argparse.Namespace(cwd=str(tmp_path)))
    return env


def _bind_singleton_socket(tmpdir: str) -> None:
    # Chromium's ProcessSingleton binds $TMPDIR/scoped_dirXXXXXX/SingletonSocket (6 random chars).
    directory = Path(tmpdir) / f"scoped_dir{secrets.token_hex(3)}"
    directory.mkdir()
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(str(directory / "SingletonSocket"))
    finally:
        shutil.rmtree(directory)


@pytest.mark.platforms("linux")
def test_long_scratch_tmpdir_hosts_the_singleton_socket_and_is_kept_for_children(monkeypatch, tmp_path):
    scratch = tmp_path / ("long-home-" * 8) / "cache" / "scratch"
    scratch.mkdir(parents=True)
    for key in ("TMPDIR", "TMP", "TEMP", "HERMES_SCRATCH_DIR"):
        monkeypatch.setenv(key, str(scratch))
    monkeypatch.setattr(tempfile, "tempdir", None)

    env = _launch_env(tmp_path)

    _bind_singleton_socket(env["TMPDIR"])
    # Electron main restores this for the backend, so the agent keeps writing to scratch.
    assert env["HERMES_DESKTOP_TMPDIR"] == str(scratch)


@pytest.mark.platforms("linux")
def test_tmpdir_that_fits_the_socket_budget_is_passed_through(monkeypatch, tmp_path):
    from hermes_constants import socket_safe_tmpdir

    with tempfile.TemporaryDirectory(prefix="dt-", dir=socket_safe_tmpdir()) as root:
        # Longest TMPDIR whose scoped_dirXXXXXX/SingletonSocket still fits sun_path.
        tmpdir = os.path.join(root, "x" * (74 - len(root) - 1))
        os.mkdir(tmpdir)
        assert len(os.fsencode(tmpdir)) == 74
        _bind_singleton_socket(tmpdir)
        monkeypatch.setenv("TMPDIR", tmpdir)
        monkeypatch.setattr(tempfile, "tempdir", None)

        env = _launch_env(tmp_path)

        assert env["TMPDIR"] == tmpdir
        assert "HERMES_DESKTOP_TMPDIR" not in env


def test_launch_env_drops_electron_run_as_node(tmp_path, monkeypatch) -> None:
    """With it set, Electron runs as plain Node and exits 0 without a window."""
    monkeypatch.setenv("ELECTRON_RUN_AS_NODE", "1")
    assert "ELECTRON_RUN_AS_NODE" not in _launch_env(tmp_path)
    assert os.environ["ELECTRON_RUN_AS_NODE"] == "1"


@pytest.mark.platforms("macos")
def test_installed_macos_app_launch_drops_electron_run_as_node(monkeypatch) -> None:
    """The /Applications/Hermes.app launch must not inherit it either."""
    from hermes_cli import bundled_app

    class _InstalledApp(type(Path())):
        def is_file(self) -> bool:
            return True

    launched: dict = {}
    monkeypatch.setenv("ELECTRON_RUN_AS_NODE", "1")
    monkeypatch.setattr(main_desktop, "Path", _InstalledApp)
    monkeypatch.setattr(bundled_app, "launch_detached", lambda argv, **kwargs: launched.update(kwargs) or 4242)

    assert main_desktop._launch_installed_macos_desktop_app() is True
    # No env= means the child inherits os.environ, which still carries it.
    assert "ELECTRON_RUN_AS_NODE" not in (launched.get("env") or os.environ)
    assert os.environ["ELECTRON_RUN_AS_NODE"] == "1"
