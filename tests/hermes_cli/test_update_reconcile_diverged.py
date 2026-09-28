"""_reconcile_diverged_checkout must never silently discard local commits.

Regression for kanban regression-case: a live install lost two landed commits when the
same-branch divergence path ran ``reset --hard origin/main``. Contract now:
unique local commits are rebased onto ``origin/<branch>``; on rebase conflict the
update parks the pre-update HEAD behind a rescue ref and refuses (exit 1); the
plain reset survives only when local has nothing origin lacks.
"""

import subprocess
from pathlib import Path

import pytest

import hermes_cli.main as main_mod
from hermes_cli import update_cmd

EMAIL = ["-c", "user.email=t@x", "-c", "user.name=t"]


def _git(cwd, *args, check=True):
    r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    if check and r.returncode != 0:
        raise AssertionError(f"git {args}: rc={r.returncode} {r.stderr.strip()}")
    return r


def _commit(cwd, name, message, content=None):
    (Path(cwd) / name).write_text(content if content is not None else f"{name}\n")
    _git(cwd, "add", "-A")
    _git(cwd, *EMAIL, "commit", "-m", message)


@pytest.fixture()
def diverged(tmp_path, monkeypatch):
    """bare origin + `work` clone + `remote` clone; PROJECT_ROOT pointed at work."""
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "--bare", "-b", "main", origin.name)
    seed = tmp_path / "seed"
    _git(tmp_path, "clone", str(origin), seed.name)
    _commit(seed, "app.txt", "base A", content="base\n")
    _git(seed, "push", "origin", "main")
    work = tmp_path / "work"
    _git(tmp_path, "clone", str(origin), work.name)
    remote = tmp_path / "remote"
    _git(tmp_path, "clone", str(origin), remote.name)
    monkeypatch.setattr(main_mod, "PROJECT_ROOT", work)
    return work, remote


def test_diverged_local_commits_are_rebased_not_discarded(diverged):
    work, remote = diverged
    _commit(remote, "remote.txt", "remote R")
    _git(remote, "push", "origin", "main")
    _commit(work, "local.txt", "local L")
    pre = _git(work, "rev-parse", "HEAD").stdout.strip()
    _git(work, "fetch", "origin")

    update_cmd._reconcile_diverged_checkout(["git"], "main", pre)

    head = _git(work, "rev-parse", "HEAD").stdout.strip()
    assert head != pre  # update still advanced onto origin/main
    assert _git(work, "rev-list", "--count", "origin/main..HEAD").stdout.strip() == "1"
    assert _git(work, "rev-list", "--count", "HEAD..origin/main").stdout.strip() == "0"
    assert "local L" in _git(work, "log", "--format=%s", "origin/main..HEAD").stdout
    assert "remote R" in _git(work, "log", "--format=%s", "-3").stdout
    assert _git(work, "for-each-ref", "refs/hermes-update-backups/").stdout.strip() == ""


def test_diverged_rebase_conflict_parks_head_and_refuses(diverged):
    work, remote = diverged
    _commit(remote, "app.txt", "remote R", content="remote line\n")
    _git(remote, "push", "origin", "main")
    _commit(work, "app.txt", "local L", content="local line\n")
    pre = _git(work, "rev-parse", "HEAD").stdout.strip()
    _git(work, "fetch", "origin")

    with pytest.raises(SystemExit) as exc:
        update_cmd._reconcile_diverged_checkout(["git"], "main", pre)
    assert exc.value.code == 1

    # Nothing moved, nothing dirty, pre-update HEAD parked behind a rescue ref.
    assert _git(work, "rev-parse", "HEAD").stdout.strip() == pre
    assert _git(work, "status", "--porcelain").stdout.strip() == ""
    refs = _git(
        work, "for-each-ref", "refs/hermes-update-backups/diverged-main-*",
        "--format=%(objectname)").stdout.strip()
    assert pre in refs.split()


def test_diverged_without_local_commits_still_resets(diverged):
    work, remote = diverged
    _commit(remote, "remote.txt", "remote R")
    _git(remote, "push", "origin", "main")
    _git(work, "fetch", "origin")
    pre = _git(work, "rev-parse", "HEAD").stdout.strip()

    update_cmd._reconcile_diverged_checkout(["git"], "main", pre)

    assert _git(work, "rev-parse", "HEAD").stdout.strip() == (
        _git(work, "rev-parse", "origin/main").stdout.strip())
