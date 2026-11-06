"""Gateway lifecycle guard for cron job creation.

A cron job that restarts/stops the gateway from inside the gateway (``hermes gateway restart``,
``launchctl kickstart ai.hermes.gateway``, ``systemctl restart hermes-gateway``) kills the process,
the supervisor revives it, auto-resume re-runs the turn: a SIGTERM-respawn loop.
``cron.jobs.create_job`` rejects such specs on every creation path. Patterns are command-shaped —
anchored on concrete command identifiers — so they cannot fire on prose. Defence-in-depth layer.
"""

from __future__ import annotations

import logging
import os
import re
import sys
from pathlib import Path
from typing import Callable, Iterator, Optional

from cron.lifecycle_guard_lex import (
    _iter_command_segments, _shlex_tokens, _split_segments, lexical_scope,
)

logger = logging.getLogger(__name__)


class GatewayLifecycleBlocked(ValueError):
    """Raised when a cron job spec contains a gateway-lifecycle command."""


# Shell-level command shapes that target the gateway lifecycle; each branch is anchored on a
# concrete command identifier so it fires only on command-shaped strings, never prose.
_GATEWAY_LIFECYCLE_PATTERN = re.compile(
    r"(?i)"
    # Branch A: destructive `hermes gateway` ops. `start` is excluded: starting from inside a
    # gateway is benign and a job may legitimately start a sibling profile. The lookbehind keeps
    # `hermes` from being a path component or word tail (`/docs/hermes gateway restart-notes.md`)
    # while every real command position (text start, whitespace, `;`/`&`/`|`, `$(`, backtick,
    # U+FFFD) still matches.
    # See #77173.
    # Windows spells the CLI with a launcher suffix (`hermes.exe`, npm-style `hermes.cmd`/`.ps1`);
    # same command, so the suffix is optional here.
    r"(?:(?<![/\w.\-])hermes(?:\.(?:exe|cmd|bat|com|ps1))?\s+gateway\s+(?:restart|stop|uninstall)\b)"
    # Branch B: launchctl ops anchored on a hermes-gateway label so unrelated hermes services stay
    # unblocked. `submit`/`bootstrap` register a NEW keepalive job wrapping an arbitrary helper (a
    # laundered restart); neutral-label submissions are caught by
    # `contains_launchctl_submit_command`. `bootout`/`remove`/`disable` are the
    # modern/legacy/durable forms of `unload`.
    # `submit` and `bootstrap` are included alongside the direct verbs (kickstart/etc.): `launchctl submit
    # -l ai.hermes.gateway-<suffix> -- <helper-script>` (or `launchctl bootstrap gui/<uid> <plist>`) creates
    # a NEW keepalive job wrapping an arbitrary helper, which is how a blocked direct restart/kill gets
    # laundered into a persistent restart loop instead (#62891) — same foot-gun, indirect shape.
    # Neutral-label submissions that dodge this text anchor are caught separately by
    # `contains_launchctl_submit_command` (execution-aware, label-independent). `bootout`/`remove`/`disable`
    # sit alongside `unload`: Apple deprecated load/unload in favour of bootstrap/bootout, so `bootout` is
    # the modern spelling of an already-listed verb, `remove` is its legacy sibling, and `disable` is what
    # makes an unload durable across boots. Omitting them left the bypassable approval layer
    # (tools/approval.py, skipped on force=True) as the only cover, while this hard block — documented as
    # "force=True cannot help here" — let them through (#80260).
    r"|(?:launchctl\s+(?:kickstart|unload|load|stop|restart|submit|bootstrap|bootout|remove|disable)\b[^\n]*\bhermes[.\-]?gateway)"
    # Branch C: systemctl ops on a hermes-gateway unit.
    r"|(?:systemctl\s+(?:-\S+\s+)*(?:restart|stop|start)\b[^\n]*\bhermes[.\-]?gateway)"
    # Branch D: pkill/kill of the gateway process, both token orders. Leading \b keeps "skill" from
    # matching as "kill".
    # `taskkill` / `Stop-Process` are the Windows spellings of the same operation; `\bp?kill\b`
    # cannot reach inside `taskkill`, so they are named outright. Service-control forms (`net stop`,
    # `sc stop`) presuppose a service install this guard has no evidence of and stay uncovered.
    r"|(?:\b(?:p?kill|taskkill|stop-process)\b[^\n]*\bhermes\b[^\n]*\bgateway)"
    r"|(?:\b(?:p?kill|taskkill|stop-process)\b[^\n]*\bgateway\b[^\n]*\bhermes)"
)

# Branch E: process killers whose TARGET is the interpreter image hosting the gateway. A supervised
# gateway is literally `python.exe` / `python3.12` (`python -m hermes_cli.main gateway run`), so
# `taskkill /F /IM python.exe`, `pkill -9 python3` or `killall python` carry no hermes/gateway token
# yet terminate it (#113667). Token-aware rather than a line regex: option VALUES are never read as
# targets (`pkill -u <user> chrome`), `-f` cmdline patterns are judged as patterns, and other image
# names (`taskkill /F /IM chrome.exe`) stay available. Numeric-PID kills are out of scope:
# the explicit PID / `proc_*` id IS the ownership-scoped route the rejection points to.
_INTERPRETER_IMAGE_RE = re.compile(r"^pythonw?(?:\d+(?:\.\d+)*)?(?:\.exe)?$")
_HOST_INTERPRETER_NAME = Path(sys.executable).name.lower() if sys.executable else ""
_KILLER_VALUE_OPTIONS = {
    # procps pkill/pgrep options that consume the next token, so the value is never the pattern.
    "pkill": frozenset({
        "-g", "--pgroup", "-G", "--group", "-P", "--parent", "-s", "--session", "-t", "--terminal",
        "-u", "--euid", "-U", "--uid", "-F", "--pidfile", "--ns", "--nslist", "-d", "--delimiter",
        "--signal", "-O", "--older", "-r", "--runstates", "--cgroup", "-A", "--ignore-ancestors",
    }),
    "killall": frozenset({"-s", "--signal", "-u", "--user", "-o", "--older-than", "-y", "--younger-than",
                          "-n", "--ns", "-Z", "--context"}),
}
_KILLER_VALUE_OPTIONS["pgrep"] = _KILLER_VALUE_OPTIONS["pkill"]
_KILLER_VALUE_OPTIONS["pidof"] = frozenset({"-o", "--omit-pid", "-S", "--separator"})
# Regex metacharacters a pkill/pgrep ERE may carry around the interpreter name (`^python3?$`, `python.*`).
_ERE_TAIL = re.compile(r"[.*+?\\\[\](){}|].*$")
_ERE_WILDCARD_ONLY = re.compile(r"^[.*+?$\s]*$")
_NAME_KILLERS = frozenset({"pkill", "killall", "taskkill", "stop-process"})
_NAME_ENUMERATORS = frozenset({"pgrep", "pidof", "get-process"})
_KILL_VERB_RE = re.compile(r"(?i)\b(?:kill|taskkill|stop-process)\b")
# A `-f` pattern that does not start with the interpreter reaches the gateway cmdline
# (`python -m hermes_cli.main gateway run` / `hermes gateway run`) only through its own tokens;
# an unrelated script that merely contains "hermes" (`hermes-polis/run.sh`, `my_hermes_bot.py`)
# cannot match it. Same hermes+gateway pairing as Branch D, plus the module path.
_GATEWAY_CMDLINE_TOKEN_RE = re.compile(r"(?i)hermes_cli|\bhermes\b[^\n]*\bgateway\b|\bgateway\b[^\n]*\bhermes\b")
# Rejection text for Branch E, shared by every tool surface that runs the guard so the agent is
# pointed at the ownership-scoped route (proc_* id / explicit PID) rather than the shell.
HOST_INTERPRETER_KILL_REJECTION = (
    "Blocked: this command kills every process whose image/name matches the Python "
    "interpreter, which is the process hosting this gateway (and this command). "
    "Stop only the process you own instead: process(action=\"kill\", session_id=\"proc_…\") "
    "for a background job Hermes started, or kill/taskkill by its explicit PID."
)


def _is_interpreter_image(value: str, *, substring: bool = False) -> bool:
    """True when *value* (a process/image name, `*`-wildcard allowed) denotes the Python interpreter
    that hosts the gateway. *substring*: pkill/pgrep match an ERE anywhere in the name, so `py`
    reaches `python3` too."""
    name = value.strip().strip("\"'").lower().removesuffix(".exe")
    if not name:
        return False
    if name.endswith("*"):
        prefix = name.rstrip("*")
        return "python".startswith(prefix) or _HOST_INTERPRETER_NAME.startswith(prefix)
    if _INTERPRETER_IMAGE_RE.match(name) or name == _HOST_INTERPRETER_NAME.removesuffix(".exe"):
        return True
    return substring and len(name) >= 2 and "python".startswith(name)


def _pattern_reaches_host_interpreter(pattern: str, *, full_cmdline: bool, exact: bool) -> bool:
    """pkill/pgrep/killall operand semantics: an ERE against the process NAME (or, with `-f`, the full
    command line). `python -m hermes_cli.main …` is the gateway's own cmdline, so a `-f` pattern
    that names the interpreter and then only wildcards or a `hermes` token reaches it, while
    `python mt_add.py` (a specific script) does not."""
    core = pattern.strip().strip("\"'").lstrip("^")
    core = core.removesuffix("$")
    head, _, rest = core.partition(" ") if full_cmdline else (core, "", "")
    # A literal interpreter name first (`python3.12`: the dot is a version separator, not an ERE
    # wildcard); only then read the head as an ERE with a metacharacter tail (`python3?`, `python.*`).
    match = None if _is_interpreter_image(head, substring=not exact) else _ERE_TAIL.search(head)
    if match:
        rest = head[match.start():] + " " + rest
        head = head[: match.start()]
    if not _is_interpreter_image(head, substring=not exact):
        return full_cmdline and bool(_GATEWAY_CMDLINE_TOKEN_RE.search(core))
    return not rest.strip() or bool(_ERE_WILDCARD_ONLY.match(rest)) or "hermes" in rest.lower()


def _killer_targets_host_interpreter(name: str, args: list[str]) -> bool:
    """Whether killer/enumerator *name* with argv *args* would select the host interpreter."""
    if name in ("pkill", "pgrep", "killall", "pidof"):
        # pkill/pgrep: ERE anywhere in the name unless -x; killall: exact name unless -r; pidof: exact.
        exact = name == "pidof" or (name == "killall") != any(t in ("-r", "--regexp", "-x", "--exact") for t in args)
        full_cmdline = name in ("pkill", "pgrep") and any(t in ("-f", "--full") for t in args)
        operands: list[str] = []
        position = 0
        while position < len(args):
            token = args[position]
            if token == "--":
                operands += args[position + 1:]
                break
            if token in _KILLER_VALUE_OPTIONS[name]:
                position += 2
                continue
            if not token.startswith("-"):
                operands.append(token)
            position += 1
        return any(_pattern_reaches_host_interpreter(op, full_cmdline=full_cmdline, exact=exact) for op in operands)
    if name == "taskkill":
        for position, token in enumerate(args[:-1]):
            option = token.lower().lstrip("/-")
            value = args[position + 1]
            if option == "im" and _is_interpreter_image(value):
                return True
            if option == "fi":
                filter_match = re.match(r"(?i)\s*['\"]?imagename\s+eq\s+(\S+)", value)
                if filter_match and _is_interpreter_image(filter_match.group(1)):
                    return True
        return False
    # Stop-Process / Get-Process: `-Name`/`-ProcessName` (also `-Name:value`), comma lists, positional
    # names for Get-Process.
    values: list[str] = []
    for position, token in enumerate(args):
        option, _, inline_value = token.partition(":")
        if option.lower() in ("-name", "-processname", "-n"):
            values.append(inline_value if inline_value else (args[position + 1] if position + 1 < len(args) else ""))
        elif name == "get-process" and not token.startswith("-") and (position == 0 or not args[position - 1].startswith("-")):
            values.append(token)
    return any(_is_interpreter_image(part) for value in values for part in value.split(","))


def _segment_names_host_interpreter(tokens: list[str], killers: frozenset[str]) -> bool:
    """Whether a tokenized segment runs one of *killers* against the host interpreter. The
    executable is read at the wrapper-peeled position first, then at the first killer token anywhere
    in the segment (`xargs kill`, Python argv lists)."""
    index = _executed_command_index(tokens)
    candidates = [index] if index is not None else []
    candidates += [i for i, token in enumerate(tokens) if _executable_name(token).lower().removesuffix(".exe") in killers]
    for position in candidates:
        name = _executable_name(tokens[position]).lower().removesuffix(".exe")
        if name in killers and _killer_targets_host_interpreter(name, tokens[position + 1:]):
            return True
    return False


def contains_host_interpreter_kill(text: str) -> bool:
    """Branch E entrypoint: a process killer aimed at the interpreter image hosting the gateway, or a
    name-derived PID feed into one (`pgrep python | xargs kill`, `kill $(pidof python3)`,
    `Get-Process python | Stop-Process`). Segment-tokenized, so quoted/spliced spellings and Python
    argv lists resolve the same way the shell resolves them."""
    normalized = _SHELL_LINE_CONTINUATION.sub(" ", text)
    kill_verb_present = bool(_KILL_VERB_RE.search(normalized))
    for segment in _iter_command_segments(normalized):
        joined = " ".join(segment)
        stripped = _ARGV_LIST_PUNCTUATION.sub(" ", joined)
        # Python argv lists (`subprocess.run(["taskkill", "/IM", "python.exe"])`) re-split only when
        # list punctuation was present, so a quoted `-f 'python my_script.py'` stays one operand.
        for tokens in ([segment, stripped.split()] if stripped != joined else [segment]):
            if _segment_names_host_interpreter(tokens, _NAME_KILLERS):
                return True
            if kill_verb_present and _segment_names_host_interpreter(tokens, _NAME_ENUMERATORS):
                return True
    return False

# Every branch uses `[^\n]*` between verb and label so matches cannot span unrelated lines. A POSIX
# backslash-newline continuation is therefore collapsed to a space before matching (as the shell
# does) rather than loosening `[^\n]*`.
# Every branch above uses `[^\n]*` between its verb and the gateway identifier so the match can't span
# unrelated lines of a longer cron prompt/script, but that also means a real multi-line shell invocation
# split across continuation lines (e.g. `launchctl submit \` / `  -l ai.hermes.gateway-... \` / `  -- ...`,
# the exact reported shape in #62891) would otherwise slip past. Collapse continuations to a single space
# before matching, mirroring what the shell itself does, rather than loosening `[^\n]*` and risking false
# positives across genuinely separate lines.
_SHELL_LINE_CONTINUATION = re.compile(r"\\\r?\n[ \t]*")

# Python argv-list punctuation (`subprocess.run(["launchctl", "bootout", ...])`) separates exec'd
# words with brackets/commas. Stripped only for the token-join re-scan, never from raw text.
# See #68289.
_ARGV_LIST_PUNCTUATION = re.compile(r"[\[\],]+")

# Branch A2: `hermes -p <profile> gateway restart|stop` (also `--profile <name>` /
# `--profile=<name>`). The selector breaks Branch A's adjacency. A sibling-profile restart is a
# legitimate fleet operation, so the profile name is captured and blocked only when it equals the
# profile running the guard. `start` stays excluded as in Branch A.
# Unlike Branch A this form is NOT unconditionally self-targeting: issued from inside gateway `zeus`,
# `hermes -p venus gateway restart` operates on a sibling profile's gateway and is a legitimate fleet
# operation. The pattern captures the named profile so `contains_gateway_lifecycle_command` can block only
# the self-targeting shape (named profile == the profile running the guard). See #78028.
_PROFILE_FLAG_LIFECYCLE_PATTERN = re.compile(
    r"(?i)"
    r"hermes\s+"
    # Each flag has one parse: '-' plus its remainder, never two ways to split
    # '--'. A following flag cannot also be an optional value (#129281).
    # Possessive token/space runs avoid repartitioning whitespace on failure;
    # whole flag groups can still backtrack to expose the profile selector.
    # Do not cap the number of flags: that would silently allow long self-stops.
    r"(?:-\S++(?:\s++(?!-\S)\S++)?\s++)*"
    # The selector: exactly the shapes the CLI's `_apply_profile_override` accepts.
    r"(?:--profile=([^\s]+)|(?:-p|--profile)\s+([^\s]+))"
    # Any global flags between the selector and the subcommand.
    r"(?:\s++-\S++(?:\s++(?!-\S)\S++)?)*"
    r"\s+gateway\s+(?:restart|stop)"
)

# Branch B needs the label AFTER the verb in one `[^\n]*` span; a loop that builds the label in an
# EARLIER `;`-segment (`label=${item%%:*}; launchctl bootout "gui/$uid/$label"`) leaves only
# `$label` next to the verb. These verbs act on an EXISTING job, so the hermes-gateway label anchor
# stays correct, but the check is "verb anywhere AND label anywhere".
# No profile identity available: cannot prove self-targeting, so do not block — sibling restarts must stay
# allowed (#78028).
_LAUNCHCTL_LIFECYCLE_VERBS_RE = re.compile(
    r"(?i)\blaunchctl\s+(?:kickstart|unload|load|stop|restart|bootout|kill|disable|remove)\b"
)
_HERMES_GATEWAY_LABEL_RE = re.compile(r"(?i)\bhermes[.\-]?gateway\b")

_SHELL_EXECUTABLES = frozenset({"sh", "bash", "dash", "ksh", "zsh"})
_SHELL_OPTIONS_WITH_VALUES = frozenset({"-O", "+O", "-o", "+o"})
_SHELL_COMMAND_FLAGS = {"-c", "--command"}
_MAX_REFERENCED_SCRIPT_BYTES = 8 * 1024 * 1024
_MAX_REFERENCED_SCRIPT_DEPTH = 8

# Directory names directly under `Library` that mark a FileProvider-backed subtree: `Mobile
# Documents` is iCloud Drive; `CloudStorage` hosts third-party providers (Dropbox, OneDrive, Google
# Drive, ...).
_CLOUD_PLACEHOLDER_MARKERS = frozenset({"Mobile Documents", "CloudStorage"})

# Executables whose arguments are DATA (search patterns, SQL, log filters) and cannot execute their
# argument text. Deliberately conservative: no `awk` (system()), no `sed` (`s///e`), no
# `echo`/`printf` (routinely piped into a shell), no `mysql` (`\\!` and `system` escapes).
_DATA_SINK_EXECUTABLES = frozenset(
    {"grep", "egrep", "fgrep", "rg", "ag", "ack", "journalctl", "sqlite3", "psql"}
)
# Argument shapes that smuggle execution back INTO a data sink (command/process substitution, psql
# `\!`). Any hit disables masking for the whole segment — fail closed to the plain regex verdict.
_UNSAFE_DATA_ARG_MARKERS = ("`", "$(", "<(", ">(", "\\!")
# sqlite3 dot-commands (`.shell`, `.system`) also disable masking. Dot must be followed by a NAME
# character so relative paths (`.`, `./x`) stay paths.
_DOT_COMMAND_ARGUMENT = re.compile(r"^\.[A-Za-z]")
# A data sink piped into a shell/interpreter can feed matched lines to execution; never mask it.
_PIPE_TO_INTERPRETER = re.compile(
    r"\|\s*&?\s*(?:sudo\s+)?(?:sh|bash|dash|ksh|zsh|xargs|eval|source)\b"
)

# Bytes sniffed before reading a referenced file in full (see _BINARY_MAGICS).
_BINARY_SNIFF_BYTES = 4096

_ReadRemoteScriptFn = Callable[[str], Optional[str]]

# Wrappers that hand execution to their argument tail: the real command sits further right, so a
# first-token-only guard would let `sudo bash ~/restart.sh` / `sudo launchctl submit ...` walk past.
# A guard that reads only the first token sees `sudo`/`env`/`nohup` and never inspects what they run, so
# `sudo bash ~/restart.sh` walked past the same walk that stops `bash ~/restart.sh`, and `sudo launchctl
# submit ...` past the label-independent submit block (#62891). `_PIPE_TO_INTERPRETER` above already reads
# `sudo ` this way for the pipe case; this generalises that reading to the command position.
_TRANSPARENT_COMMAND_PREFIXES = frozenset({
    "sudo", "doas", "env", "nohup", "setsid", "nice", "ionice", "stdbuf",
    "timeout", "exec", "command", "builtin", "eatmydata",
    # Privilege and namespace wrappers: options, then the command they run.
    "pkexec", "su", "runuser", "setpriv", "systemd-run", "nsenter", "unshare",
})

# Wrapper options that consume the NEXT token, so a value is never mistaken for the command.
_TRANSPARENT_PREFIX_VALUE_OPTIONS = {
    "sudo": {"-u", "-g", "-U", "-C", "-p", "-r", "-t", "-T", "--user", "--group", "--prompt"},
    "doas": {"-u", "-C"},
    "env": {"-u", "--unset", "-S", "--split-string", "-C", "--chdir"},
    "nice": {"-n", "--adjustment"},
    "ionice": {"-c", "-n", "-p", "--class", "--classdata"},
    "stdbuf": {"-i", "-o", "-e", "--input", "--output", "--error"},
    "timeout": {"-s", "-k", "--signal", "--kill-after"},
    "pkexec": {"--user"},
    "su": {"-s", "--shell", "-g", "--group", "-G", "--supp-group"},
    "runuser": {"-u", "--user", "-s", "--shell", "-g", "--group", "-G", "--supp-group"},
    "setpriv": {"--reuid", "--regid", "--groups", "--inh-caps", "--ambient-caps", "--bounding-set",
                "--selinux-label", "--apparmor-profile"},
    "systemd-run": {"-u", "--unit", "-p", "--property", "-E", "--setenv", "--slice",
                    "--description", "--uid", "--gid", "--on-calendar", "--service-type"},
    "nsenter": {"-t", "--target", "-S", "--setuid", "-G", "--setgid", "-r", "--root", "-w", "--wd"},
    "unshare": {"--map-user", "--map-group", "--setgroups", "-R", "--root", "-w", "--wd"},
}

# Wrapper options carrying a COMMAND STRING (shell source, re-scanned like `sh -c`); treating it as
# an opaque value would hide whatever it runs (`env -S 'bash ~/restart.sh'`).
_STRING_COMMAND_OPTIONS = {
    "env": ("-S", "--split-string"),
    "su": ("-c", "--command"),
    "runuser": ("-c", "--command"),
}

# Wrappers whose first non-option operand is a VALUE, not the command (`timeout 60 bash x.sh`).
_TRANSPARENT_PREFIX_OPERANDS = {"timeout": 1}

_ENV_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

# Bound the walk: a pathological token run must not spin here.
_MAX_PREFIX_PEELS = 8

_BINARY_MAGICS = (
    b"\x7fELF",              # ELF — Linux/BSD executables and shared objects
    b"\xfe\xed\xfa\xce",     # Mach-O 32-bit
    b"\xfe\xed\xfa\xcf",     # Mach-O 64-bit
    b"\xce\xfa\xed\xfe",     # Mach-O 32-bit, byte-swapped
    b"\xcf\xfa\xed\xfe",     # Mach-O 64-bit, byte-swapped
    b"\xca\xfe\xba\xbe",     # Mach-O universal ("fat") binary
    b"MZ",                   # PE/COFF — Windows .exe/.dll
    b"!<arch>",              # static archive (.a)
    b"\x1f\x8b",             # gzip
    b"PK\x03\x04",           # zip (also .jar/.whl/.egg)
)


# --- profile identity -------------------------------------------------------------------------

def _current_profile_name() -> Optional[str]:
    """Profile running the guard (``hermes_cli.profiles.current_profile_name``); ``None`` if none."""
    from hermes_cli.profiles import current_profile_name

    return current_profile_name()


def _named_profile_is_current(named: str) -> bool:
    """True when *named* is the profile executing the guard. Without a profile identity
    self-targeting cannot be proven, so nothing is blocked (sibling restarts stay allowed)."""
    current = _current_profile_name()
    return bool(current) and named.strip().casefold() == current.strip().casefold()


# --- direct string scans ----------------------------------------------------------------------

def _contains_launchctl_gateway_lifecycle(normalized_text: str) -> bool:
    """Order-independent companion to Branch B — see the verbs regex comment."""
    return bool(_LAUNCHCTL_LIFECYCLE_VERBS_RE.search(normalized_text)) and bool(
        _HERMES_GATEWAY_LABEL_RE.search(normalized_text)
    )


def contains_gateway_lifecycle_command(text: str) -> bool:
    """Return True if *text* contains a gateway lifecycle command pattern.

    Passes, in order: raw-text regex (the only pass that fires on inputs shlex cannot tokenize, e.g.
    Python source); profile-flag form; the same regex on each shell-tokenized segment with quotes/
    escapes resolved (closes splice bypasses like ``kick"start"`` / ``kick\\start``);
    order-independent launchctl pass. Single choke point for every recursion level of
    ``ReferenceScanner.scan``.

    That second pass exists because a real shell resolves quote-splicing (``kick"start"``) and
    backslash-escaping (``kick\\start``) into one literal word — ``kickstart`` — before the command ever
    runs. The raw text still has the quote or backslash sitting between the verb's two halves, so the first
    pass alone lets a spliced verb reach ``launchctl``/``systemctl`` untouched while still executing as the
    blocked lifecycle command (#80269, reported against #80260's bootout parity fix). Tokenizing closes that
    gap while keeping the same gateway-label anchoring (``_GATEWAY_LIFECYCLE_PATTERN`` still requires a
    ``hermes``/``gateway`` token) — this function is the single choke point
    ``ReferenceScanner.scan`` calls at every recursion level, so referenced-script and ``sh -c``
    payload scanning inherit the fix automatically.
    """
    if not text:
        return False
    # Provably inert heredoc bodies (quoted delimiter, data-sink consumer like `cat > f <<'EOF'`)
    # are documentation, not commands. The stripper fails open on ANY ambiguity (unquoted delimiter,
    # shell consumer, unterminated body), so executable heredocs are still scanned.
    # Heredoc bodies that are provably inert data (quoted delimiter, data-sink consumer like `cat > file
    # <<'EOF'`) are masked before scanning (#88336): a runbook line "a human can run: hermes gateway
    # restart" inside such a body is documentation, not a command this shell will execute.
    from tools.shell_heredoc import strip_inert_heredoc_bodies

    text = strip_inert_heredoc_bodies(text)
    normalized = _SHELL_LINE_CONTINUATION.sub(" ", text)
    if _GATEWAY_LIFECYCLE_PATTERN.search(normalized):
        return True
    # Profile-flag form: blocked only when the named profile IS the one running the guard.
    # Profile-flag form (#78028): `hermes -p <profile> gateway restart|stop` bypasses Branch A because the
    # selector sits between `hermes` and `gateway`. It is only the same foot-gun when the named profile IS
    # the profile running the guard — sibling-profile restarts are legitimate fleet operations and stay
    # allowed.
    profile_match = _PROFILE_FLAG_LIFECYCLE_PATTERN.search(normalized)
    if profile_match:
        named = profile_match.group(1) or profile_match.group(2)
        # Profile ids cannot contain quotes (`^[a-z0-9][a-z0-9_-]{0,63}$`), so a shell-quoted
        # `-p 'zeus'` compares equal to the bare name.
        if named and _named_profile_is_current(named.strip().strip("\"'")):
            return True
    # Token-aware pass. Tokens are also re-joined with Python argv-list punctuation stripped, since
    # `subprocess.run(["launchctl", "bootout", ...])` separates argv words with commas/brackets.
    # Token-aware second pass (#80269): re-run the pattern on shell-tokenized segments where quotes/escapes
    # are resolved, closing splice bypasses like `kick"start"`. Runs after the profile-flag check so both
    # passes apply independently.
    for segment in _iter_command_segments(normalized):
        joined = " ".join(segment)
        if joined and _GATEWAY_LIFECYCLE_PATTERN.search(joined):
            return True
        stripped = _ARGV_LIST_PUNCTUATION.sub(" ", joined)
        if stripped != joined and _GATEWAY_LIFECYCLE_PATTERN.search(stripped):
            return True
    # The label may be built in an earlier `;`-segment, so no pass above sees verb + label together.
    # Order-independent launchctl pass (#77083): a shell loop can build the gateway label from a variable
    # defined in an earlier `;`-separated segment (`label=${item%%:*}; launchctl bootout
    # "gui/$uid/$label"`), so neither the same-span regex nor same-segment tokenization sees verb and label
    # together. Check "verb anywhere AND label anywhere" instead.
    # Branch E (#113667): killers aimed at the interpreter image itself carry no hermes/gateway token.
    return _contains_launchctl_gateway_lifecycle(normalized) or contains_host_interpreter_kill(normalized)


# Root commands keep their original bounds. Referenced sources have separate work accounting;
# every read/lex batch and cached traversal checks the cooperative deadline. Exhaustion is a
# named refusal, never a clean partial inspection or a lifecycle accusation.
_MAX_LIFECYCLE_SCAN_BYTES = 1024 * 1024  # root command only
_MAX_LIFECYCLE_SOURCE_BYTES = 16 * 1024 * 1024
_MAX_LIFECYCLE_SOURCE_LINES = 262144
_MAX_LIFECYCLE_SCAN_TOKENS = 4_000_000
_LIFECYCLE_SCAN_SECONDS = 5.0
_MAX_LIFECYCLE_SCAN_LINES = 32768
_MAX_LIFECYCLE_SCAN_LINE_BYTES = 64 * 1024
_MAX_LIFECYCLE_SCAN_PATHS = 1024
_MAX_LIFECYCLE_SCAN_REMOTE_READS = 64


class _LifecycleScanBudget:
    """Invocation-local accounting; deadlines are cooperative, not OS I/O deadlines."""

    def __init__(self) -> None:
        import time

        self.bytes_remaining = _MAX_LIFECYCLE_SOURCE_BYTES
        self.lines_remaining = _MAX_LIFECYCLE_SOURCE_LINES
        self.paths_remaining = _MAX_LIFECYCLE_SCAN_PATHS
        self.remote_reads_remaining = _MAX_LIFECYCLE_SCAN_REMOTE_READS
        self.tokens_remaining = _MAX_LIFECYCLE_SCAN_TOKENS
        self.deadline = time.monotonic() + _LIFECYCLE_SCAN_SECONDS
        self.refusal: Optional[str] = None
        self.reached_paths: list[str] = []

    def checkpoint(self) -> None:
        import time
        from cron.lifecycle_guard_references import ScanRefused

        if time.monotonic() >= self.deadline:
            raise ScanRefused("cooperative five-second work deadline exhausted")

    def charge_tokens(self, count: int) -> None:
        from cron.lifecycle_guard_references import ScanRefused

        self.checkpoint()
        self.tokens_remaining -= count
        if self.tokens_remaining < 0:
            raise ScanRefused("token budget exhausted (4000000 tokens)")

    def charge_text(self, text: str, *, root: bool = False, raw_bytes: int = 0) -> bool:
        self.checkpoint()
        encoded = max(raw_bytes, len(text.encode("utf-8", errors="replace")))
        lines = text.count("\n") + 1
        byte_cap = _MAX_LIFECYCLE_SCAN_BYTES if root else self.bytes_remaining
        line_cap = _MAX_LIFECYCLE_SCAN_LINES if root else self.lines_remaining
        if encoded > byte_cap or lines > line_cap:
            return False
        for start in range(0, len(text), 32768):
            self.checkpoint()
        if any(len(line.encode("utf-8", errors="replace")) > _MAX_LIFECYCLE_SCAN_LINE_BYTES
               for line in text.split("\n")):
            return False
        if not root:
            self.bytes_remaining -= encoded
            self.lines_remaining -= lines
        return True

    def charge_path(self) -> bool:
        self.checkpoint()
        if self.paths_remaining <= 0:
            return False
        self.paths_remaining -= 1
        return True

    def charge_remote_read(self) -> bool:
        self.checkpoint()
        if self.remote_reads_remaining <= 0:
            return False
        self.remote_reads_remaining -= 1
        return True


def _capped_read_limit(max_bytes: Optional[int]) -> int:
    """Per-read byte cap: never above the per-file cap, never negative. One definition so local and
    remote reads cannot diverge.

    See #76762, #77703.
    """
    if max_bytes is None:
        return _MAX_REFERENCED_SCRIPT_BYTES
    return min(_MAX_REFERENCED_SCRIPT_BYTES, max(0, int(max_bytes)))


def lifecycle_scan_root_within_budget(text: str) -> bool:
    """Whether *text* may safely enter an optional tokenizer pass (``tools/terminal_tool.py`` gates
    its launchctl pre-scan on this). A FRESH budget, independent of the full guard's walk: the
    pre-scan may pass while the walk later exhausts, still fail-closed — only the friendlier
    launchctl diagnostic is lost. ``False`` is not a verdict: callers must still run the full guard."""
    try:
        return _LifecycleScanBudget().charge_text(text, root=True)
    except Exception:
        return False


# --- shell tokenization -----------------------------------------------------------------------

def _executable_name(token: str) -> str:
    """Command name of an executable token. ``Path(token).name`` is "" for ``.``, ``..`` and ``/``;
    the POSIX dot-source builtin is spelled ``.``, so fall back to the raw token or ``.
    ./helper.sh`` would escape the sourced-script scan."""
    if os.name != "nt" and "/" not in token:
        return token
    return Path(token).name or token


def _peel_transparent_prefixes(segment: list[str], index: int) -> int:
    """Index of the command a wrapper chain actually executes. Unchanged if not a wrapper; may be
    ``len(segment)`` when a wrapper has no operand — callers must bounds-check."""
    for _ in range(_MAX_PREFIX_PEELS):
        if index >= len(segment):
            return index
        name = _executable_name(segment[index])
        if name not in _TRANSPARENT_COMMAND_PREFIXES:
            return index
        value_options = _TRANSPARENT_PREFIX_VALUE_OPTIONS.get(name, frozenset())
        index += 1
        while index < len(segment):
            token = segment[index]
            if token == "--":
                # POSIX end-of-options: the command starts at the next token.
                index += 1
                break
            if token in value_options:
                index += 2
                continue
            if token.startswith("-") or _ENV_ASSIGNMENT.match(token):
                index += 1
                continue
            break
        for _ in range(_TRANSPARENT_PREFIX_OPERANDS.get(name, 0)):
            if index < len(segment) and not segment[index].startswith("-"):
                index += 1
    return index


def _command_token_index(segment: list[str]) -> Optional[int]:
    """Return the executable token index after simple env assignments."""
    return next((i for i, token in enumerate(segment) if not _ENV_ASSIGNMENT.match(token)), None)


def _executed_command_index(segment: list[str]) -> Optional[int]:
    """Index of the command a segment actually executes (env assignments and wrappers peeled)."""
    index = _command_token_index(segment)
    if index is None:
        return None
    index = _peel_transparent_prefixes(segment, index)
    return index if index < len(segment) else None


def contains_launchctl_submit_command(command: str) -> bool:
    """Detect an executed ``launchctl submit``/``bootstrap``, not quoted text.

    Label-independent by design: a NEW job's label is attacker-chosen, so a neutral name defeats any
    label-anchored regex. Both verbs register a persistent launchd job — never safe in the gateway.

    See #62891.
    """
    for segment in _iter_command_segments(command):
        index = _executed_command_index(segment)
        if index is not None and _executable_name(segment[index]) == "launchctl":
            arguments = segment[index + 1 :]
            if arguments and arguments[0].lower() in {"submit", "bootstrap"}:
                return True
    return False


def _mask_data_sink_arguments(text: str) -> str:
    """Replace data-sink executables' arguments with a neutral placeholder.

    The regex cannot tell an EXECUTED lifecycle command from the same characters as *data* (a grep
    pattern, a SQL literal): every argument of a ``_DATA_SINK_EXECUTABLES`` segment becomes ``arg``
    and a match that survives is a real command. Strictly fail-closed: masking is skipped when the
    line pipes into a shell/interpreter, any argument carries an execution marker, or the line
    cannot be tokenized. Masking can only ALLOW what the plain regex would block, never block more.
    """
    lines_out: list[str] = []
    changed = False
    for line in text.splitlines() or [text]:
        if _PIPE_TO_INTERPRETER.search(line):
            lines_out.append(line)
            continue
        try:
            tokens = _shlex_tokens(line)
        except ValueError:
            lines_out.append(line)
            continue

        rebuilt: list[str] = []
        for segment in _split_segments(tokens, keep_controls=True):
            index = _command_token_index(segment)
            if index is not None and Path(segment[index]).name in _DATA_SINK_EXECUTABLES:
                arguments = segment[index + 1 :]
                if not any(
                    _DOT_COMMAND_ARGUMENT.match(argument)
                    or any(marker in argument for marker in _UNSAFE_DATA_ARG_MARKERS)
                    for argument in arguments
                ):
                    changed = True
                    rebuilt.extend(segment[: index + 1])
                    rebuilt.extend("arg" for _ in arguments)
                    continue
            rebuilt.extend(segment)
        lines_out.append(" ".join(rebuilt))
    return "\n".join(lines_out) if changed else text


def _lifecycle_command_scan_with_data_exemption(text: str) -> bool:
    """Lifecycle scan exempting matches inside data arguments: cheap regex first (no-match pays
    nothing), then re-scan with data-sink arguments masked; only a surviving match blocks."""
    if not contains_gateway_lifecycle_command(text):
        return False
    normalized = _SHELL_LINE_CONTINUATION.sub(" ", text)
    return contains_gateway_lifecycle_command(_mask_data_sink_arguments(normalized))


def _direct_lifecycle_scan(command: str) -> bool:
    """Pure-string direct scans: lifecycle regex (data-exempted) + submit."""
    return (
        _lifecycle_command_scan_with_data_exemption(command)
        or contains_launchctl_submit_command(command)
    )


# --- path handling ----------------------------------------------------------------------------

def _resolve_lenient(path: Path) -> Path:
    """``path.resolve(strict=False)``, falling back to *path* on OSError (unreadable/long) or
    ValueError (embedded NUL from decoded binary tokenized as a path) — never crash the guard."""
    try:
        return path.resolve(strict=False)
    except (OSError, ValueError):
        return path


def _is_cloud_placeholder_path(path: Path) -> bool:
    """True for paths inside a macOS FileProvider-backed subtree. ``O_NONBLOCK`` does not make
    regular-file reads non-blocking, so opening an evicted placeholder can wait indefinitely for
    hydration before any command timeout starts: identify the boundary from the path alone."""
    parts = path.parts
    return any(
        parts[index - 1] == "Library" and part in _CLOUD_PLACEHOLDER_MARKERS
        for index, part in enumerate(parts)
        if index
    )


def _on_cloud_path(path: Path) -> bool:
    """Lexical OR resolved cloud check: covers direct cloud paths and local symlinks into one."""
    return _is_cloud_placeholder_path(path) or _is_cloud_placeholder_path(_resolve_lenient(path))


def _expand_candidate_path(candidate: str) -> Optional[Path]:
    """Sanitize a tokenized path candidate at the ingestion boundary. Tokens from shlex-splitting
    arbitrary (possibly binary-decoded) text can carry NUL or junk that each downstream ``Path`` op
    rejects differently (ValueError, RuntimeError when HOME is unset under launchd, OSError); reject
    once here. ``None`` = not a real path, nothing to scan.

    Every OS-facing ``Path`` operation downstream (``expanduser``, ``os.open``, ``resolve``) raises a
    *different* exception for the same junk (``ValueError: embedded null byte``, ``RuntimeError: Could not
    determine home directory`` when HOME is unset under launchd, OSError for over-long paths). Rejecting
    here — once, before any OS call — is the whole-class fix; catching per-syscall was the whack-a-mole that
    produced #76762, #77703, #77780, and #78256.
    """
    if not candidate or "\x00" in candidate:
        return None
    try:
        return Path(candidate).expanduser()
    except (ValueError, RuntimeError, OSError):
        return None


def _resolved_or_nothing(candidate: str, cwd: Optional[str]) -> Iterator[Path]:
    """Yield *candidate* anchored on *cwd* (or the process cwd) when it is a real path."""
    path = _expand_candidate_path(candidate)
    if path is None:
        return
    if not path.is_absolute():
        try:
            path = Path(cwd or Path.cwd()) / path
        except OSError:
            # Path.cwd() can raise when the process cwd was deleted.
            return
    yield path


def _resolve_script_path(script_path: str) -> Optional[Path]:
    """Resolve a cron ``script`` value the way ``cron.scheduler`` does (relative paths live under
    ``<HERMES_HOME>/scripts/``) so the guard scans the file that will actually run."""
    from hermes_constants import get_hermes_home

    raw = _expand_candidate_path(script_path)
    if raw is None:
        return None
    if raw.is_absolute():
        return raw
    try:
        return get_hermes_home() / "scripts" / raw
    except (RuntimeError, OSError):
        # get_hermes_home() falls back to Path.home(), which raises when neither HERMES_HOME nor
        # HOME is resolvable (launchd/systemd) — same ingestion contract: nothing to scan.
        return None


def _resolve_script_directory(script_path: str) -> Optional[str]:
    """Return the directory *script_path* resolves to, handling relative names."""
    try:
        path = _resolve_script_path(script_path)
        if path is not None and path.is_absolute():
            return str(path.parent)
    except Exception:
        pass
    return None


# --- referenced-script discovery --------------------------------------------------------------

def _iter_option_values(segment: list[str], start: int, option: str) -> Iterator[str]:
    """Yield values given to *option*, in both ``--opt v`` and ``--opt=v`` form."""
    prefix = option + "="
    for position in range(start + 1, len(segment)):
        token = segment[position]
        if token == option and position + 1 < len(segment):
            yield segment[position + 1]
        elif token.startswith(prefix):
            yield token[len(prefix):]


def _references_at(segment: list[str], index: int, cwd: Optional[str]) -> Iterator[Path]:
    """Yield the scripts the token at *index* executes, if any."""
    if index >= len(segment):
        return
    executable = segment[index]
    executable_name = _executable_name(executable)

    if executable_name in {".", "source"}:
        if len(segment) > index + 1:
            yield from _resolved_or_nothing(segment[index + 1], cwd)
        return

    if executable_name in _SHELL_EXECUTABLES:
        arguments = segment[index + 1 :]
        arg_index = 0
        while arg_index < len(arguments):
            argument = arguments[arg_index]
            if argument == "--":
                arg_index += 1
                break
            if argument in _SHELL_COMMAND_FLAGS:
                break
            if argument in _SHELL_OPTIONS_WITH_VALUES:
                arg_index += 2
                continue
            if argument.startswith("-"):
                arg_index += 1
                continue
            break
        if arg_index < len(arguments) and arguments[arg_index] not in _SHELL_COMMAND_FLAGS:
            yield from _resolved_or_nothing(arguments[arg_index], cwd)
        return

    # A bare "/" is pathlib's division operator in Python sources, not an executable; resolving it
    # hits the filesystem root and fails the regular-file check, hard-blocking innocent .py scripts.
    if executable.strip("/") and ("/" in executable or executable.endswith((".sh", ".bash", ".zsh"))):
        yield from _resolved_or_nothing(executable, cwd)


def _iter_referenced_shell_scripts(command: str, *, cwd: Optional[str] = None) -> Iterator[Path]:
    """Yield scripts executed directly or through a POSIX shell. Each segment is read at the
    original token AND at the peeled wrapper target — additive on purpose: peeling must never REMOVE
    a reference (a local ``./timeout`` is a script, not the coreutils wrapper)."""
    for segment in _iter_command_segments(command):
        index = _command_token_index(segment)
        if index is None:
            continue
        yield from _references_at(segment, index, cwd)
        peeled = _peel_transparent_prefixes(segment, index)
        if peeled != index:
            yield from _references_at(segment, peeled, cwd)


def _iter_shell_command_payloads(command: str) -> Iterator[str]:
    """Yield code passed through ``sh|bash|... -c`` (and ``su -c`` / ``env -S``) for recursive
    scanning."""
    for segment in _iter_command_segments(command):
        index = _command_token_index(segment)
        if index is None:
            continue
        # Read at the ORIGINAL token: peeling past `su`/`env` would discard the command option.
        for option in _STRING_COMMAND_OPTIONS.get(_executable_name(segment[index]), ()):
            yield from _iter_option_values(segment, index, option)
        index = _executed_command_index(segment)
        if index is None or _executable_name(segment[index]) not in _SHELL_EXECUTABLES:
            continue
        arguments = segment[index + 1 :]
        for arg_index, argument in enumerate(arguments[:-1]):
            if argument in _SHELL_COMMAND_FLAGS:
                yield arguments[arg_index + 1]
                break


# --- referenced-script reading ----------------------------------------------------------------

def _has_binary_magic(data: bytes) -> bool:
    """Require both a known binary signature and a NUL in the read bytes. Signature-prefixed
    text can still be a shell script (for example ``MZ=1``). A NUL alone must not bypass the
    scan either: ``bash`` executes padded text scripts. A shebang always wins. Extensions
    are not consulted: suffixless text must still be scanned and fail closed if oversized."""
    if data.startswith(b"#!"):
        return False
    return data.startswith(_BINARY_MAGICS) and b"\x00" in data


def _read_referenced_script(
    path: Path, *, max_bytes: Optional[int] = None
) -> tuple[Optional[str], bool]:
    """Legacy tuple adapter for the typed, SQLite-safe bounded reader."""
    from cron.lifecycle_guard_references import read_local_source

    result = read_local_source(path, max_bytes=_capped_read_limit(max_bytes))
    return result.text, result.status in {"refused", "truncated"}


def _sanitize_remote_script_text(
    text: Optional[str], *, max_bytes: Optional[int] = None
) -> tuple[Optional[str], bool]:
    """Apply the local-read contract to text from an untrusted ``read_remote_script`` callback: NUL
    means binary (nothing to scan, checked first); oversized fails closed. Size compares re-encoded
    *bytes* (matching the ``head -c`` wire bound): a >1 MiB multibyte file truncated at the byte cap
    decodes to fewer chars, and a char count would scan instead of failing.

    The recursion boundary must not trust its callbacks: any backend (SSH, Modal, Daytona, or a future one)
    can hand back raw binary bytes decoded as text, or arbitrarily large output. Enforced here rather than
    inside each callback so the guarantee holds for every callback, not just the ones we hardened. See
    #76762, #77703.
    """
    if not text or "\x00" in text:
        return None, False
    byte_limit = _capped_read_limit(max_bytes)
    if len(text) > byte_limit:
        return None, True  # chars <= bytes: over the cap without encoding
    if len(text.encode("utf-8", errors="replace")) > byte_limit:
        return None, True
    return text, False


# --- recursive walk ---------------------------------------------------------------------------

def scan_gateway_lifecycle(
    command: str, *, cwd: Optional[str] = None,
    read_remote_script: Optional[_ReadRemoteScriptFn] = None,
    remote_namespace: bool = False,
    execution_options: Optional[str] = "",
    scan_stats: Optional[dict] = None,
) -> tuple[bool, Optional[str]]:
    """Return (unsafe, named refusal); incomplete supported inspection always refuses.

    Legacy one-argument readers remain supported. Explicit remote namespaces never touch
    the host filesystem; metadata-dependent expansion needs a typed backend reader.
    ``execution_options`` is the actual execution context's NODE_OPTIONS, never config.
    """
    from cron.lifecycle_guard_references import ReferenceScanner, ScanRefused

    budget = _LifecycleScanBudget()
    try:
        with lexical_scope(budget):
            if remote_namespace and (not cwd or not os.path.isabs(cwd)):
                raise ScanRefused("remote execution cwd unavailable")
            if remote_namespace and re.search(r"(?:^|[\s'\"])~[/\\]", command):
                raise ScanRefused("remote home-relative selector requires explicit execution home")
            if not budget.charge_text(command, root=True):
                raise ScanRefused("root text budget exhausted (1 MiB / 32768 lines / 64 KiB physical line)")
            scanner = ReferenceScanner(budget, read_remote_script, remote_namespace, execution_options)
            unsafe = scanner.scan(command, cwd or str(Path.cwd()), depth=0)
            return unsafe, budget.refusal if unsafe else None
    except ScanRefused as exc:
        return True, str(exc)
    except Exception as exc:
        logger.warning("lifecycle guard inspection failed (%s); refusing incomplete scan", type(exc).__name__, exc_info=True)
        return True, f"incomplete inspection ({type(exc).__name__})"
    finally:
        if scan_stats is not None:
            scan_stats.update(
                source_bytes=_MAX_LIFECYCLE_SOURCE_BYTES - budget.bytes_remaining,
                source_lines=_MAX_LIFECYCLE_SOURCE_LINES - budget.lines_remaining,
                tokens=_MAX_LIFECYCLE_SCAN_TOKENS - budget.tokens_remaining,
                paths=_MAX_LIFECYCLE_SCAN_PATHS - budget.paths_remaining,
                backend_reads=_MAX_LIFECYCLE_SCAN_REMOTE_READS - budget.remote_reads_remaining,
                reached_paths=budget.reached_paths,
            )


def contains_gateway_lifecycle_command_or_referenced_script(
    command: str, *, cwd: Optional[str] = None,
    read_remote_script: Optional[_ReadRemoteScriptFn] = None,
) -> bool:
    """Detect lifecycle/submit commands, including bounded nested scripts (see
    ``scan_gateway_lifecycle`` for the contract)."""
    return scan_gateway_lifecycle(command, cwd=cwd, read_remote_script=read_remote_script)[0]


def _scan_cron_job(prompt: str, script: Optional[str]) -> tuple[bool, Optional[str]]:
    """Budget the prompt as a command and the job's own script as a referenced source."""
    from cron.lifecycle_guard_references import ReferenceScanner, ScanRefused

    budget = _LifecycleScanBudget()
    try:
        with lexical_scope(budget):
            if not budget.charge_text(prompt, root=True):
                raise ScanRefused("root text budget exhausted (1 MiB / 32768 lines / 64 KiB physical line)")
            scanner = ReferenceScanner(budget, None, False, None)
            resolved = _resolve_script_path(script) if script else None
            text = ""
            if resolved is not None:
                result = scanner.read(str(resolved))
                if result.status in {"refused", "truncated"}:
                    raise ScanRefused(f"{resolved}: {result.reason}")
                text = result.text or ""
            combined = f"{prompt}\n{text}" if text else prompt
            if resolved is not None and resolved.suffix == ".py":
                return _lifecycle_command_scan_with_data_exemption(combined), None
            return scanner.scan(combined, str(resolved.parent) if resolved else str(Path.cwd()), 0), None
    except ScanRefused as exc:
        return True, str(exc)
    except Exception as exc:
        logger.warning("cron lifecycle inspection failed; refusing incomplete scan", exc_info=True)
        return True, f"incomplete cron inspection ({type(exc).__name__})"


def check_gateway_lifecycle(prompt: Optional[str], script: Optional[str] = None) -> None:
    """Raise ``GatewayLifecycleBlocked`` if *prompt* or *script* contains a gateway-lifecycle
    command. The script is read from disk and concatenated with the prompt so a command cannot slip
    through by being split across the two. Callers let the ``ValueError``-shaped exception
    propagate."""
    resolved = _resolve_script_path(script) if script else None
    if resolved is not None and _on_cloud_path(resolved):
        raise GatewayLifecycleBlocked(
            "Blocked: the cron script lives on a cloud-synced path. Opening an evicted "
            "FileProvider placeholder can hang preflight, so it is refused before open."
        )
    unsafe, refusal = _scan_cron_job(prompt or "", script)
    if unsafe and refusal:
        raise GatewayLifecycleBlocked(
            f"Blocked: the lifecycle guard could not scan this cron job or referenced script: {refusal}. "
            "Nothing in the job is known to contain a gateway lifecycle command, but a script "
            "the job executes must be scannable (a regular text file within the reported scan bounds) before it can run."
        )
    if unsafe:
        raise GatewayLifecycleBlocked(
            "Blocked: cron job contains a gateway lifecycle command or persistent "
            "launchctl submit operation. This is blocked to prevent agent-driven "
            "SIGTERM-respawn loops under launchd/systemd supervision "
            "(#30719). Run `hermes gateway restart` from a shell outside "
            "the running gateway instead."
        )
