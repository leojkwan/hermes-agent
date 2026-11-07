"""The bash wrapper reports each pipeline segment's own exit status."""

import subprocess

import pytest

from tools.environments.base_session_env import _split_cwd_marker, _wrap_command_script

CWD_MARKER = "__HERMES_CWD_test__"
PIPE_MARKER = "__HERMES_PIPE_test__"


def _script(command: str, pipe_marker: str = PIPE_MARKER) -> str:
    return _wrap_command_script(
        command, quoted_cwd="/tmp", quoted_snap="/nonexistent-snapshot",
        snap_tmp_template="/tmp/hermes-snap.XXXXXX", passthrough_names=(),
        snapshot_ready=False, cwd_marker=CWD_MARKER, pipe_marker=pipe_marker,
    )


def _run_result(command: str, pipe_marker: str = PIPE_MARKER) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-c", _script(command, pipe_marker)], capture_output=True, text=True, check=False,
    )


def _run(command: str) -> str:
    return _run_result(command).stdout


def test_pipeline_status_captures_the_leading_command():
    output = _run("false | true")

    assert f"{PIPE_MARKER}1 0{PIPE_MARKER}" in output


def test_plain_command_reports_one_status():
    output = _run("false")

    assert f"{PIPE_MARKER}1{PIPE_MARKER}" in output


def _seen(result: subprocess.CompletedProcess) -> tuple[int, str, str]:
    """Exit code and output as the model sees them, markers stripped like production does."""
    stdout = result.stdout
    for marker in (PIPE_MARKER, CWD_MARKER):
        split = _split_cwd_marker(stdout, marker)
        stdout = split[1] if split else stdout
    return result.returncode, stdout, result.stderr


@pytest.mark.parametrize(
    ("command", "exit_code", "stdout"),
    [
        ("if false; then echo yes; fi", 0, ""),
        ("while read line; do echo \"$line\"; done < /dev/null", 0, ""),
        ("set -o pipefail; false | true", 1, ""),
        ("! false", 0, ""),
        ("! true", 1, ""),
        ("echo 'unbalanced", 2, ""),
        ('echo "unbalanced', 2, ""),
        ("echo $(echo unclosed", 2, ""),
        ("echo hi \\", 0, "hi \\\n"),
        ("cat <<'EOF'\nunterminated heredoc", 0, "unterminated heredoc\n"),
        ("exit 3", 3, ""),
        ("trap 'echo T' ERR; false", 1, "T\n"),
    ],
)
def test_capturing_pipestatus_changes_nothing_the_model_sees(command, exit_code, stdout):
    """The capture is side data: the shell's own ``$?`` and output, no wrapper text."""
    seen_code, seen_stdout, seen_stderr = _seen(_run_result(command))

    assert (seen_code, seen_stdout) == (exit_code, stdout)
    assert "__hermes" not in seen_stderr


@pytest.mark.parametrize("pipe_marker", [PIPE_MARKER, ""])
@pytest.mark.parametrize("command", [
    "echo 'unbalanced", 'echo "unbalanced', "echo $(echo unclosed",
    "echo ${unclosed", "echo `echo unclosed",
])
def test_incomplete_syntax_reports_shell_error_without_wrapper_text(pipe_marker, command):
    code, stdout, stderr = _seen(_run_result(command, pipe_marker))
    assert (code, stdout) == (2, "")
    assert stderr and "__hermes" not in stderr


@pytest.mark.parametrize("pipe_marker", [PIPE_MARKER, ""])
@pytest.mark.parametrize("command", ["printf '%s\\n' hi \\", "printf '%s\\n' hi \\\\"])
def test_terminal_backslashes_keep_bash_eof_semantics(pipe_marker, command):
    assert _seen(_run_result(command, pipe_marker)) == (0, "hi\n\\\n", "")


def test_syntax_check_does_not_execute_substitutions_twice(tmp_path):
    path = tmp_path / "runs"
    command = "printf '%s' \"$(printf run >> '" + str(path) + "'; printf once)\""
    assert _seen(_run_result(command)) == (0, "once", "")
    assert path.read_text() == "run"


@pytest.mark.parametrize("command,expected", [
    ("shopt -s extglob\ncase foo in @(foo)) printf match;; esac", "match"),
    ("shopt -s expand_aliases\nalias greet='printf hello'\ngreet", "hello"),
])
def test_commands_can_enable_parse_options_before_later_lines(command, expected):
    assert _seen(_run_result(command)) == (0, expected, "")


def test_quoted_unterminated_heredoc_keeps_terminal_backslash_data():
    code, stdout, stderr = _seen(_run_result("cat <<'EOF'\nbody " + chr(92)))
    assert (code, stdout) == (0, "body \\\n")
    assert "__hermes" not in stderr


def test_sequential_extglob_with_substitution_runs_once(tmp_path):
    counter = tmp_path / "substitutions"
    command = (
        "shopt -s extglob\nx=$(printf run >> '" + str(counter) + "'; printf foo)\n"
        "case $x in @(foo)) printf match;; esac"
    )
    assert _seen(_run_result(command)) == (0, "match", "")
    assert counter.read_text() == "run"


def test_complete_substitution_keeps_pipeline_side_data():
    result = _run_result('false | printf "%s" "$(printf fixture)"')
    assert _seen(result) == (0, "fixture", "")
    assert f"{PIPE_MARKER}1 0{PIPE_MARKER}" in result.stdout
