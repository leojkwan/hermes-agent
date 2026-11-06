"""Behavioral POSIX parity, including fallback and Python argv literals."""
import shlex

import pytest

from cron.lifecycle_guard import scan_gateway_lifecycle
from cron.lifecycle_guard_lex import _shlex_tokens


@pytest.mark.parametrize("line", [
    "a 'b c' d", 'a "b c" d', "kick\"start\" ai.hermes.gateway", r"kick\start x",
    "echo a#comment", "echo 'a#data'", "a && b || c; (d)", r'echo "a\q"',
    "echo '' x", "echo 'a\\b'", "écho '你好'", "echo a\\ b", "echo \"a\\$b\"",
])
def test_posix_tokens_match_standard_library(line):
    oracle = shlex.shlex(line, posix=True, punctuation_chars=";&|()")
    oracle.whitespace_split = True
    oracle.commenters = "#"
    assert _shlex_tokens(line) == list(oracle)


@pytest.mark.parametrize("command", [
    'launchctl kick"start" ai.hermes.gateway', r"launchctl kick\start ai.hermes.gateway",
    'subprocess.run(["launchctl", "bootout", "gui/501/ai.hermes.gateway"])',
    "broken 'quote\nhermes gateway restart\n", "hermes gateway \\\nrestart",
])
def test_quotes_fallback_continuations_and_argv_literals(command):
    assert scan_gateway_lifecycle(command) == (True, None)
