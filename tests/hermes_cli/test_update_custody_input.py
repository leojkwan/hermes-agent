"""macOS updater input must not deadlock against a full git output pipe."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parents[2]


@pytest.fixture
def batch_repo(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    root = tmp_path / "repo"
    root.mkdir()

    def git(*args, input=None):
        return subprocess.run(
            ["git", "-C", str(root), *args],
            input=input,
            capture_output=True,
            check=True,
            timeout=10,
        ).stdout.strip()

    git("init", "-q")
    blob = git("hash-object", "-w", "--stdin", input=bytes(range(256)) * 64)
    entries = b"".join(
        b"100644 blob " + blob + f"\tfile-{i:04d}\n".encode() for i in range(512)
    )
    tree = git("mktree", input=entries).decode()
    return root, tree


def _probe(tmp_path, script, *args):
    program = tmp_path / "probe.py"
    program.write_text(
        "import sys\nsys.path.insert(0, sys.argv[1])\n" + textwrap.dedent(script),
        encoding="utf-8",
    )
    # The outer deadline still works if communicate() blocks inside os.write past its own timeout.
    result = subprocess.run(
        [sys.executable, "-I", str(program), str(SOURCE), *map(str, args)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.platforms("macos")
def test_target_batch_read_drains_output_while_accepting_requests(batch_repo, tmp_path):
    root, tree = batch_repo
    _probe(
        tmp_path,
        """
        import faulthandler
        from pathlib import Path
        from hermes_cli.update_cmd_commit import read_target_files
        from hermes_cli.update_lock import UpdateLock

        faulthandler.dump_traceback_later(5)
        root, tree = Path(sys.argv[2]), sys.argv[3]
        lock = UpdateLock(path=root.parent / 'marker', install_root=root)
        assert lock.acquire()
        try:
            names = [f'file-{i:04d}' for i in range(512)]
            found = read_target_files(['git'], root, tree, [*names, names[0], 'absent'])
            assert found == {**dict.fromkeys(names, bytes(range(256)) * 64), 'absent': None}
        finally:
            lock.release()
        contender = UpdateLock(path=root.parent / 'next-marker', install_root=root)
        assert contender.acquire()
        contender.release()
    """,
        root,
        tree,
    )


@pytest.mark.platforms("macos")
@pytest.mark.parametrize("mode", ["bytes", "text", "empty"])
def test_owner_watched_input_preserves_data_and_timeout(tmp_path, mode):
    _probe(
        tmp_path,
        """
        import subprocess
        from hermes_cli.update_custody import _run_owner_watched

        mode = sys.argv[2]
        text = mode == 'text'
        payload = '雪\\n' * 32768 if text else (b'' if mode == 'empty' else bytes(range(256)) * 512)
        options = dict(input=payload, capture_output=True, timeout=3)
        if text:
            options.update(text=True, encoding='utf-8', errors='strict')
        echo = [sys.executable, '-I', '-c',
                'import sys; data=sys.stdin.buffer.read(); sys.stdout.buffer.write(data)']
        result = _run_owner_watched(echo, dict(options))
        assert result.returncode == 0 and result.stdout == payload
        assert result.stderr == ('' if text else b'')

        # A child that fills stdout before reading stdin used to trap the parent in os.write.
        blocked = [sys.executable, '-I', '-c',
                   'import os, threading; os.write(1, b"x" * 1048576); threading.Event().wait()']
        try:
            _run_owner_watched(blocked, dict(options))
        except subprocess.TimeoutExpired as exc:
            assert exc.output == ('x' if text else b'x') * 1048576
        else:
            raise AssertionError('the child must time out')
    """,
        mode,
    )
