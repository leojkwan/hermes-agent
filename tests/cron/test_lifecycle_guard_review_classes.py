"""FBL-370 lexical budgets and conservative execution-fact mutation classes."""

import pytest

from cron import lifecycle_guard as guard
from tests.cron.lifecycle_context_fixtures import known_context, npm_context


@pytest.mark.parametrize(
    "prefix", ["echo hi # don't\n", 'echo hi # "unterminated\n', "echo 'one\\\ntwo'\n"]
)
def test_comments_and_single_quoted_backslashes_leave_following_command(
    tmp_path, prefix
):
    (tmp_path / "stop.cjs").write_text("execSync('hermes gateway restart');\n")
    assert guard.scan_gateway_lifecycle(
        prefix + "node stop.cjs", cwd=str(tmp_path), execution_context=known_context()
    ) == (True, None)


@pytest.mark.parametrize(
    "cap,command", [(5, "echo one; echo two"), (2, 'echo "one two"')]
)
def test_original_tokens_are_charged_once(monkeypatch, cap, command):
    monkeypatch.setattr(guard, "_MAX_LIFECYCLE_SCAN_TOKENS", cap)
    assert guard.scan_gateway_lifecycle(command) == (False, None)


@pytest.mark.parametrize(
    "prefix",
    [
        "bash -k -c 'node safe.cjs NODE_OPTIONS=--require=./stop.cjs'",
        "bash -ekc 'node safe.cjs NODE_OPTIONS=--require=./stop.cjs'",
        "bash -o keyword -c 'node safe.cjs NODE_OPTIONS=--require=./stop.cjs'",
        "bash -a -c 'npm test'",
        "shopt -so keyword; node safe.cjs NODE_OPTIONS=--require=./stop.cjs",
        "shopt -so allexport; npm test",
        "env SHELLOPTS=keyword bash -c 'node safe.cjs NODE_OPTIONS=--require=./stop.cjs'",
        "BASHOPTS=keyword bash -c 'node safe.cjs'",
        ": ${x:PATH=0}; npm test",
        "a[PATH=0]=1; npm test",
        "declare -i n; n=PATH=0; npm test",
    ],
)
def test_unmodeled_mutation_classes_cannot_reuse_facts(tmp_path, prefix):
    (tmp_path / "safe.cjs").write_text("console.log('safe');\n")
    (tmp_path / "package.json").write_text('{"scripts":{"test":"node safe.cjs"}}')
    unsafe, reason = guard.scan_gateway_lifecycle(
        prefix, cwd=str(tmp_path), execution_context=npm_context(tmp_path)
    )
    assert unsafe and reason is not None


@pytest.mark.parametrize(
    "prefix",
    [
        "echo '${x:PATH=0} a[PATH=0]=1 declare -i n'; ",
        "echo okay # ${x:PATH=0}\n",
        "for ((i=0;i<3;i++)); do :; done; ",
    ],
)
def test_neutral_and_inert_scripts_keep_known_context(tmp_path, prefix):
    (tmp_path / "package.json").write_text('{"scripts":{"test":"node -e 1"}}')
    assert guard.scan_gateway_lifecycle(
        prefix + "npm test", cwd=str(tmp_path), execution_context=npm_context(tmp_path)
    ) == (False, None)


def test_single_quoted_backslash_newline_is_literal_data():
    from cron.lifecycle_guard_lex import _iter_command_segments

    assert list(_iter_command_segments("echo 'one\\\ntwo'\necho after")) == [
        ["echo", "one\\\ntwo"],
        ["echo", "after"],
    ]
