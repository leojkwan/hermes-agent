"""Bash session-snapshot scripting for ``tools.environments.base``.

Pure string builders: the CWD marker, the ``export -p`` dump that strips
per-session vars, the ``init_session`` bootstrap, and the per-command wrapper.
No module state lives here; ``BaseEnvironment`` supplies quoting hooks.
"""

import re
import shlex
from typing import Iterable

# Bridged per-session vars (gateway.session_context._VAR_MAP) are injected fresh onto every
# command's process env and must NEVER persist in the shared bash snapshot: one long-lived
# backend serves many sessions, so a snapshot carrying the FIRST session's HERMES_SESSION_ID
# would make every LATER session source a foreign identity. Every bridged name starts with
# one of these prefixes (or is HERMES_UI_SESSION_ID); unit tests use this regex as the
# Python-side contract for the exclusion set.
# Per-session variables that the gateway bridges freshly onto every command's process environment (via
# tools/environments/local._inject_session_context_env, reading gateway.session_context._VAR_MAP). They must
# NEVER be persisted into the shared bash session snapshot: a single long-lived backend serves many
# concurrent sessions (the messaging gateway, TUI, desktop/web dashboard all collapse the terminal to one
# "default" environment), so ``export -p`` dumping the FIRST session's HERMES_SESSION_ID into the snapshot
# makes every LATER session ``source`` that stale value and see a FOREIGN session's identity — overriding
# the correct per-command Popen env (issue: cross-session HERMES_SESSION_ID leak via the shared snapshot).
# Stripping them from the snapshot is safe because they are re-injected on every command; a snapshot should
# only carry the user's own shell state (PATH, functions, exports they set), not Hermes' per-turn session
# identity. Used by unit tests as the Python-side contract for the exclusion set; the dump path unsets by
# name/prefix instead of grepping declare lines (see below / issue #71296).
_SNAPSHOT_EXCLUDED_ENV_REGEX = (
    "^declare -x (HERMES_SESSION_|HERMES_UI_SESSION_ID|HERMES_CRON_AUTO_DELIVER_|"
    "HERMES_CRON_SESSION|HERMES_DELEGATED_CHILD_CONTEXT|"
    "HERMES_RPC_|HERMES_KERNEL_DIR)")
_SHELL_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# mktemp template suffix + the shell variable holding the allocated temp path.
_SNAP_TMP_SUFFIX = ".tmp.XXXXXXXXXX"
_SNAP_TMP = '"$__hermes_snap_tmp"'
SNAPSHOT_RESTORE_ERROR = (
    "Hermes session snapshot could not be restored; command was not started. "
    "Run a foreground shell command to retry session recovery."
)


def _cwd_marker(session_id: str) -> str:
    return f"__HERMES_CWD_{session_id}__"


def _pipe_marker(session_id: str) -> str:
    return f"__HERMES_PIPE_{session_id}__"


def _pipe_marker_printf(marker: str) -> str:
    """Emit the pipeline's per-segment exit statuses (``PIPESTATUS``) on their own line.

    ``cmd | tail`` exits with tail's status, so the shell's code cannot prove ``cmd``
    passed. The array holds each segment's own status and is captured before anything
    else runs.
    """
    return f"printf '\\n{marker}%s{marker}\\n' \"${{__hermes_ps[*]-}}\""


def _eval_capturing_pipestatus(escaped: str) -> str:
    """``eval`` the command and keep its last pipeline's ``PIPESTATUS`` as side data.

    The array must be read inside the same ``eval`` (afterwards it holds only ``eval``'s
    status), while ``$?`` stays the exit code: the two differ for ``!``, ``if``/``while``,
    ``pipefail`` and parse errors. The blank line stops the capture joining a command
    continued by a trailing backslash. The restore sits on the left of ``&&`` so its
    non-zero status fires neither a user's ``ERR`` trap a second time nor ``set -e``.
    """
    return f"eval '{escaped}\n\n__hermes_rc=$? __hermes_ps=(\"${{PIPESTATUS[@]}}\"); (exit $__hermes_rc) && :'"


def _can_capture_pipestatus(command: str) -> bool:
    """An unterminated heredoc would read the capture line as its body (into the user's
    file), and a trailing ``\\`` would join onto it: such commands run unchanged."""
    if "<<" in command or command.endswith("\\"):
        return False
    from tools.environments.base_shell_input import incomplete_shell_input
    return not incomplete_shell_input(command)


def _command_eval_script(command: str, capture: bool) -> str:
    escaped = command.replace("'", "'\\''")
    # eval drops an odd terminal backslash on Bash 3.2; bash -c retains it at EOF.
    trailing = len(command) - len(command.rstrip("\\"))
    evaluated = escaped + ("\\" if trailing % 2 and "<<" not in command else "")
    run = _eval_capturing_pipestatus(escaped) if capture else f"eval '{evaluated}'"
    from tools.environments.base_shell_input import incomplete_shell_input
    if not capture and incomplete_shell_input(command):
        # Bash 3.2 eval reports incomplete-input errors as 1; bash -c reports 2.
        run += '\n__hermes_eval_rc=$?\nif [ "$__hermes_eval_rc" = 1 ]; then (exit 2); else (exit "$__hermes_eval_rc"); fi'
    return run


def _cwd_marker_printf(marker: str) -> str:
    """Emit the CWD marker on its own line (leading ``\\n`` guards against a
    command whose output lacks a trailing newline; ``_split_cwd_marker`` strips it)."""
    return f"printf '\\n{marker}%s{marker}\\n' \"$(pwd -P)\""


def _export_dump_excluding_session_vars(tmp_path: str, excluded_names: Iterable[str] = ()) -> str:
    """Remove export attributes before dumping, including readonly excluded scalars.

    Filtering serialized lines is unsafe on Bash 3.2 multiline values. Unexporting
    works for readonly variables too; failure prevents publication of the snapshot.
    """
    names = {name for name in excluded_names if isinstance(name, str) and _SHELL_ENV_NAME_RE.fullmatch(name)}
    extra = " ".join(shlex.quote(name) for name in sorted(names))
    return (
        "{ ( for __hermes_excluded in ${!HERMES_SESSION_*} ${!HERMES_CRON_AUTO_DELIVER_*} "
        "${!HERMES_RPC_*} AI_AGENT HERMES_AGENT HERMES_DELEGATED_CHILD_CONTEXT "
        "HERMES_CRON_SESSION HERMES_KERNEL_DIR HERMES_UI_SESSION_ID "
        f"{extra}; do builtin export -n \"$__hermes_excluded\" 2>/dev/null || exit 1; done; "
        f"builtin export -p; ) ; }} > {tmp_path}")


def _execution_context_trailer(tmp_path: str, excluded_names: Iterable[str]) -> str:
    """Describe only exported scalars, without exposing excluded values to encoders."""
    excluded = {name for name in excluded_names if isinstance(name, str) and _SHELL_ENV_NAME_RE.fullmatch(name)}
    lines = ["( set -o pipefail || exit 1; LC_ALL=C || exit 1; export LC_ALL",
             "__hermes_exports=$(builtin compgen -e) || exit 1", "printf '# hermes-exec-context v1'"]
    for name in ("NODE_OPTIONS", "HOME", "PATH", "PREFIX", "DESTDIR"):
        limit = 16384 if name == "PATH" else 4096
        lines.append(f"printf ' {name}='")
        if name in excluded:
            lines.append("printf inherit")
            continue
        lines += [
            f"if [[ $'\\n'$__hermes_exports$'\\n' != *$'\\n'{name}$'\\n'* ]]; then printf inherit;",
            f'elif [ "${{{name}+x}}" != x ]; then printf inherit;',
            f'elif [ "${{#{name}}}" -gt {limit} ]; then printf oversize;',
            f'else __hermes_decl=$(builtin declare -p {name} 2>/dev/null);',
            '__hermes_flags=${__hermes_decl#declare -}; __hermes_flags=${__hermes_flags%% *};',
            'case "$__hermes_flags" in',
            f" x) printf a:; printf %s \"${name}\" | command -p od -An -v -tx1 | command -p tr -d ' \\n' || exit 1;;",
            ' *x*) printf unsupported;;',
            ' *) printf inherit;; esac; fi',
        ]
    patterns = "|".join(shlex.quote(name) for name in sorted(excluded)) or "''"
    lines += [
        "printf ' NPMENV='",
        "__hermes_names=; __hermes_count=0; __hermes_bad=0",
        "while IFS= read -r __hermes_name; do",
        f'case "$__hermes_name" in {patterns}) continue;; esac',
        'case "$__hermes_name" in [nN][pP][mM]_[cC][oO][nN][fF][iI][gG]_*) ;; *) continue;; esac',
        '__hermes_decl=$(builtin declare -p "$__hermes_name" 2>/dev/null) || exit 1',
        '__hermes_flags=${__hermes_decl#declare -}; __hermes_flags=${__hermes_flags%% *}',
        'case "$__hermes_flags" in *r*) __hermes_bad=1; break;; esac',
        '__hermes_count=$((__hermes_count + 1))',
        'if [ "$__hermes_count" -gt 64 ] || [ "${#__hermes_name}" -gt 128 ]; then __hermes_bad=1; break; fi',
        '__hermes_names="${__hermes_names}${__hermes_name}\n"',
        'done <<< "$__hermes_exports"',
        'if [ "$__hermes_bad" = 1 ]; then printf unsupported;',
        'elif [ -z "$__hermes_names" ]; then printf none;',
        "else printf %b \"$__hermes_names\" | command -p od -An -v -tx1 | command -p tr -d ' \\n' || exit 1; fi",
        "__hermes_path_alias=absent",
        'while IFS= read -r __hermes_name; do case "$__hermes_name" in PATH) ;; [pP][aA][tT][hH]) __hermes_path_alias=present;; esac; done <<< "$__hermes_exports"',
        "printf ' PATHALIASES=%s' \"$__hermes_path_alias\"",
        "__hermes_hooks=absent",
        'while IFS= read -r __hermes_name; do case "$__hermes_name" in BASH_ENV) __hermes_hooks=present;; esac; done <<< "$__hermes_exports"',
        "printf ' SHELLHOOKS=%s' \"$__hermes_hooks\"",
        "__hermes_modes=absent",
        'case ":${SHELLOPTS-}:${BASHOPTS-}:" in *:keyword:*|*:allexport:*) __hermes_modes=present;; esac',
        "printf ' SHELLMODES=%s' \"$__hermes_modes\"",
        "printf '\n'",
        f") >> {tmp_path}",
    ]
    return "\n".join(lines)


def _snapshot_bootstrap_script(
    *, quoted_cwd: str, quoted_snap: str, snap_tmp_template: str, excluded_names: Iterable[str], cwd_marker: str,
) -> str:
    """Login-shell bootstrap that captures env/functions/aliases into the snapshot. Atomic publish:
    assemble in a ``mktemp`` file, then ``mv`` over the final path so a concurrent ``source`` never
    reads a half-written snapshot (``$$`` is the parent PID in ``&``-launched subshells and macOS
    bash 3.2 lacks ``$BASHPID``, so only ``mktemp`` is portable). Functions are filtered by NAME via
    ``declare -F`` (a line-based ``declare -f | grep -v`` strips the header and leaves an orphaned
    body that breaks every sourced command); the non-empty guard matters because bare ``declare -f``
    dumps ALL functions. The trailing ``cd`` restores the configured cwd after profile scripts (e.g.
    ``cd ~``) so ``pwd -P`` reports terminal.cwd, not the profile's directory."""
    return (
        "umask 077\n"
        f"__hermes_snap_tmp=$(mktemp {snap_tmp_template}) || exit 1\n"
        f"{_export_dump_excluding_session_vars(_SNAP_TMP, excluded_names)} || {{ rm -f {_SNAP_TMP}; exit 1; }}\n"
        "__hermes_fns=$(declare -F | awk '{print $3}' | grep -vE '^_[^_]') || true\n"
        f"[ -n \"$__hermes_fns\" ] && declare -f $__hermes_fns >> {_SNAP_TMP} 2>/dev/null || true\n"
        f"alias -p >> {_SNAP_TMP}\n"
        f"echo 'shopt -s expand_aliases' >> {_SNAP_TMP}\n"
        f"echo 'set +e' >> {_SNAP_TMP}\n"
        f"echo 'set +u' >> {_SNAP_TMP}\n"
        # Publish only if assembly succeeded; otherwise drop the partial temp.
        f"{_execution_context_trailer(_SNAP_TMP, excluded_names)} && "
        f"mv -f {_SNAP_TMP} {quoted_snap} || {{ rm -f {_SNAP_TMP}; exit 1; }}\n"
        f"builtin cd -- {quoted_cwd} 2>/dev/null || true\n"
        f"{_cwd_marker_printf(cwd_marker)}\n")


def _passthrough_save_restore(names: Iterable[str]) -> tuple[list[str], list[str]]:
    """Shell lines that save profile-scoped passthrough vars before the snapshot is sourced
    and restore (or unset) them afterwards — a shared snapshot may hold the previous
    profile's value. Values stay in environment memory and never enter the command string."""
    save: list[str] = []
    restore: list[str] = []
    for name in names:
        marker = f"_HERMES_RUNTIME_PASSTHROUGH_{name}"
        present, value = f"{marker}_PRESENT", f"{marker}_VALUE"
        save += [f"{present}=${{{name}+x}}", f"{value}=${{{name}-}}"]
        restore += [
            f'if [ "${present}" = x ]; then export {name}="${value}"; else unset {name}; fi',
            f"unset {present} {value}"]
    return save, restore


def _restore_failure_script(marker: str, reason: str) -> str:
    notice = f"builtin printf '\\n{marker}%s{marker}\\n' {reason}; " if marker else ""
    return notice + f"builtin printf '%s\\n' {shlex.quote(SNAPSHOT_RESTORE_ERROR)} >&2; exit 126"


def _source_snapshot_excluding(
    quoted_snap: str, snap_tmp_template: str, names: Iterable[str], restore_marker: str,
) -> str:
    """Re-serialize in a subshell before readonly old exports reach the command shell.

    Filter by variable name in shell state, never by serialized lines: Bash 3.2
    declarations can contain literal newlines. The private copy retains exported
    attributes, functions and aliases, including unrelated readonly exports.
    """
    temporary = '"$__hermes_restore_tmp"'
    dump = _export_dump_excluding_session_vars(temporary, names)
    missing = _restore_failure_script(restore_marker, "missing")
    unavailable = _restore_failure_script(restore_marker, "unavailable")
    return (
        f"[ -f {quoted_snap} ] || {{ {missing}; }}\n"
        f"__hermes_restore_tmp=$(mktemp {snap_tmp_template} 2>/dev/null) || {{ {unavailable}; }}\n"
        f"( builtin source {quoted_snap} >/dev/null 2>&1 || exit 1; "
        f"{dump} && builtin declare -f >> {temporary} "
        f"&& builtin alias -p >> {temporary} "
        f"&& builtin printf '%s\\n' 'shopt -s expand_aliases' 'set +e' 'set +u' >> {temporary} "
        f") 2>/dev/null || {{ rm -f {temporary}; {unavailable}; }}\n"
        f"builtin source {temporary} >/dev/null 2>&1\n"
        "__hermes_restore_rc=$?\n"
        f"rm -f {temporary} || {{ {unavailable}; }}\n"
        f'[ "$__hermes_restore_rc" = 0 ] || {{ {unavailable}; }}'
    )


def _wrap_command_script(
    command: str, *, quoted_cwd: str, quoted_snap: str, snap_tmp_template: str,
    passthrough_names: Iterable[str], snapshot_ready: bool, cwd_marker: str,
    pipe_marker: str = "", persist_env: bool = True, restore_marker: str = "",
    quoted_publish_snap: str | None = None) -> str:
    """Per-command bash script: source snapshot, cd, run, re-dump env, emit CWD marker.
    ``persist_env=False`` skips the re-dump, so every command starts from the login-time
    snapshot and exports from one command never reach the next (the CWD marker still runs).
    ``source`` stdout goes to /dev/null because macOS bash 3.2 / some Homebrew builds echo
    ``declare -x`` lines when sourcing. AI_AGENT/HERMES_AGENT advertise the harness to remote
    backends (whose env is not inherited); ``${VAR:-default}`` never clobbers an outer harness.
    GIT_PAGER/PAGER=cat stop pager-happy tools hanging a PTY-backed command. The env re-dump
    uses the same mktemp+mv atomic publish as the bootstrap and chains ``mv`` on the dump
    succeeding so a failed dump never replaces a good snapshot. ``umask 077`` is applied after
    the user's command so snapshot files (which may carry secrets) are private without
    changing the command's umask.
    """
    capture = bool(pipe_marker) and _can_capture_pipestatus(command)
    save, restore = _passthrough_save_restore(passthrough_names)
    parts = list(save)
    if snapshot_ready:
        parts.append(_source_snapshot_excluding(quoted_snap, snap_tmp_template, passthrough_names, restore_marker))
    parts += restore
    parts += [
        'export AI_AGENT="${AI_AGENT:-hermes-agent}" HERMES_AGENT="${HERMES_AGENT:-true}"',
        'export GIT_PAGER="${GIT_PAGER:-cat}" PAGER="${PAGER:-cat}"',
        # ``--`` keeps hyphen-prefixed directory names from being parsed as options.
        f"builtin cd -- {quoted_cwd} || exit 126",
        _command_eval_script(command, capture),
        "__hermes_ec=$?",
        "umask 077"]
    if snapshot_ready and persist_env:
        parts.append(
            f"__hermes_snap_tmp=$(mktemp {snap_tmp_template}) && "
            f"{{ {_export_dump_excluding_session_vars(_SNAP_TMP, passthrough_names)} "
            f"&& {_execution_context_trailer(_SNAP_TMP, passthrough_names)} "
            f"&& mv -f {_SNAP_TMP} {quoted_publish_snap or quoted_snap}; }} "
            f"2>/dev/null || rm -f {_SNAP_TMP} 2>/dev/null || true")
    if capture:
        parts.append(_pipe_marker_printf(pipe_marker))
    if cwd_marker:
        parts.append(_cwd_marker_printf(cwd_marker))
    parts.append("exit $__hermes_ec")
    return "\n".join(parts)


def _split_cwd_marker(output: str, marker: str) -> tuple[str | None, str] | None:
    """Locate the last ``marker<path>marker`` pair in *output*. Returns
    ``(cwd_path_or_None, output_without_marker_line)``, or ``None`` when no complete pair
    exists. The stripped span runs from the ``\\n`` the wrapper injected before the marker
    through the end of the marker line."""
    last = output.rfind(marker)
    if last == -1:
        return None
    search_start = max(0, last - 4096)  # CWD path won't be >4KB
    first = output.rfind(marker, search_start, last)
    if first == -1 or first == last:
        return None
    cwd_path = output[first + len(marker) : last].strip() or None
    line_start = output.rfind("\n", 0, first)
    if line_start == -1:
        line_start = first
    line_end = output.find("\n", last + len(marker))
    line_end = line_end + 1 if line_end != -1 else len(output)
    return cwd_path, output[:line_start] + output[line_end:]
