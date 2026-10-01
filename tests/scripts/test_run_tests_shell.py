"""The canonical shell must run tests and propagate a failing test's status."""
from pathlib import Path
import os
import subprocess
import sys

import pytest


@pytest.mark.platforms("posix")
def test_shell_runner_executes_tests_and_propagates_failure(tmp_path):
    case = tmp_path / "test_runner_canary.py"
    marker = tmp_path / "executed"
    case.write_text(
        "from pathlib import Path\nimport os\n"
        "def test_canary():\n"
        f"    Path({str(marker)!r}).write_text('executed')\n"
        "    assert os.environ['PATHEXT'] == '.COM;.EXE;.BAT;.CMD'\n"
        "    assert False, 'runner failure propagation canary'\n",
        encoding="utf-8",
    )
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        ["bash", str(root / "scripts/run_tests.sh"), "-j", "1", str(case)],
        cwd=tmp_path, capture_output=True, text=True, timeout=180,
        env={**os.environ, "HERMES_PYTHON": sys.executable, "HERMES_TEST_FILE_RETRIES": "0",
             "PATHEXT": ".COM;.EXE;.BAT;.CMD"},
    )
    assert marker.read_text(encoding="utf-8") == "executed", result.stdout + result.stderr
    assert result.returncode != 0, result.stdout + result.stderr
    assert "runner failure propagation canary" in result.stdout + result.stderr


@pytest.mark.platforms("posix")
def test_out_of_tree_cache_mode_leaves_checkout_clean(tmp_path):
    cache_root = tmp_path / "cache"
    marker = tmp_path / "executed"
    case = tmp_path / "test_cache_canary.py"
    case.write_text(
        "from pathlib import Path\n"
        f"def test_cache_canary():\n    Path({str(marker)!r}).write_text('executed')\n",
        encoding="utf-8",
    )
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        ["bash", str(root / "scripts/run_tests.sh"), "-j", "1", str(case)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=180,
        env={
            **os.environ,
            "HERMES_PYTHON": sys.executable,
            "HERMES_TEST_FILE_RETRIES": "0",
            "HERMES_TEST_OUT_OF_TREE_CACHE": str(cache_root),
        },
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert marker.read_text(encoding="utf-8") == "executed"
    assert cache_root.joinpath("test_durations.json").is_file()
    assert cache_root.joinpath("pytest").is_dir()
    for artifact in (
        root / ".pytest_cache",
        root / ".pytest-cache",
        root / "test_durations.json",
        root / ".hermes",
    ):
        assert not artifact.exists(), artifact
    assert not any(root.rglob("__pycache__")), "bytecode cache leaked into checkout"
