"""FBL-369: updater preflight reads complete batches without bidirectional pipe stalls."""

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

from hermes_cli.update_cmd_commit import read_target_files


def _git(root, *args):
    return subprocess.check_output(
        ["git", "-C", str(root), *args], text=True,
        env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull},
    ).strip()


def test_large_batch_returns_every_blob_and_missing_entry(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    expected = {}
    for index in range(300):
        rel = f"objects/{index:04}_{'x' * 100}.py"
        data = (f"VALUE = {index}\n" * 512).encode()
        path = root / rel
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(data)
        expected[rel] = hashlib.sha256(data).hexdigest()
    _git(root, "add", ".")
    _git(root, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
         "-c", f"core.hooksPath={os.devnull}", "commit", "-qm", "synthetic source")
    manifest = tmp_path / "expected.json"
    manifest.write_text(json.dumps(expected))
    child = """
import hashlib, json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from hermes_cli.update_cmd_commit import read_target_files
expected = json.loads(Path(sys.argv[3]).read_text())
found = read_target_files(['git'], Path(sys.argv[2]), 'HEAD', [*expected, 'missing.py'])
assert found.pop('missing.py') is None
assert {name: hashlib.sha256(data).hexdigest() for name, data in found.items()} == expected
print(len(found))
"""
    result = subprocess.run(
        [sys.executable, "-c", child, str(Path(__file__).resolve().parents[2]),
         str(root), str(manifest)], capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "300"


def test_missing_target_does_not_supply_source_bytes(tmp_path):
    _git(tmp_path, "init", "-q")
    assert read_target_files(["git"], tmp_path, "missing-ref", ["module.py"]) == {"module.py": None}
