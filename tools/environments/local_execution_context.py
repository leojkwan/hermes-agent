"""Private, bounded foreground snapshot facts. Reading facts never runs shell code."""

from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass, field, replace
from typing import Literal

FIELD_NAMES = {
    "NODE_OPTIONS": "node_options",
    "HOME": "home",
    "PATH": "path",
    "PREFIX": "prefix",
    "DESTDIR": "destdir",
}
FIELD_LIMITS = {name: 16384 if name == "PATH" else 4096 for name in FIELD_NAMES}
TRAILER_PREFIX = b"# hermes-exec-context v1 "
MAX_TRAILER_BYTES = 98304
MAX_SNAPSHOT_BYTES = 8 * 1024 * 1024
MAX_NPM_NAMES = 64
NPM_PREFIX = re.compile(r"npm_config_", re.IGNORECASE | re.ASCII)
NPM_NAME = re.compile(r"npm_config_[A-Za-z0-9_-]*", re.IGNORECASE | re.ASCII)
SHELL_HOOK_NAMES = frozenset({"BASH_ENV", "ENV", "ZDOTDIR"})


@dataclass(frozen=True)
class EnvFact:
    status: Literal["absent", "value", "unknown"]
    value: str = field(default="", repr=False)
    reason: str = ""


@dataclass(frozen=True)
class ExecutionContext:
    node_options: EnvFact
    home: EnvFact
    path: EnvFact
    prefix: EnvFact
    destdir: EnvFact
    npm_env_names: frozenset[str] | None = field(repr=False)
    npm_path_safe: bool = False
    shell_startup_safe: bool = False
    shell_modes_safe: bool = True

    @classmethod
    def unknown(cls, reason: str = "execution-context-unavailable") -> ExecutionContext:
        fact = EnvFact("unknown", reason=reason)
        return cls(fact, fact, fact, fact, fact, None)

    def with_fact(self, name: str, fact: EnvFact) -> ExecutionContext:
        if name in SHELL_HOOK_NAMES:
            return replace(
                self,
                shell_startup_safe=self.shell_startup_safe and fact.status == "absent",
            )
        if name in FIELD_NAMES:
            return replace(self, **{FIELD_NAMES[name]: fact})
        if name.casefold() == "path":
            return replace(self, npm_path_safe=False)
        if NPM_PREFIX.match(name):
            if not NPM_NAME.fullmatch(name):
                return replace(self, npm_env_names=None)
            if self.npm_env_names is None:
                return self
            names = set(self.npm_env_names)
            if fact.status == "unknown":
                return replace(self, npm_env_names=None)
            elif fact.status == "value":
                names.add(name.lower())
            return replace(self, npm_env_names=frozenset(names))
        return self


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _snapshot_bytes(path: str) -> bytes:
    effective_uid = getattr(os, "geteuid", None)
    if effective_uid is None:
        raise ValueError("snapshot-platform-unsupported")
    descriptor = os.open(
        path, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != effective_uid()
            or before.st_mode & 0o077
        ):
            raise ValueError("snapshot-not-private-regular")
        if before.st_size > MAX_SNAPSHOT_BYTES:
            raise ValueError("snapshot-oversize")
        chunks, size = [], 0
        while True:
            chunk = os.read(descriptor, min(65536, MAX_SNAPSHOT_BYTES + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > MAX_SNAPSHOT_BYTES:
                raise ValueError("snapshot-oversize")
        snapshot = b"".join(chunks)
        if _identity(before) != _identity(os.fstat(descriptor)) or _identity(
            before
        ) != _identity(os.stat(path, follow_symlinks=False)):
            raise ValueError("snapshot-identity-changed")
        return snapshot
    finally:
        os.close(descriptor)


def _trailer_bytes(snapshot: bytes) -> bytes:
    tail = snapshot[-MAX_TRAILER_BYTES - 1:]
    if not tail.endswith(b"\n"):
        raise ValueError("snapshot-trailer-malformed")
    line = tail[:-1].rsplit(b"\n", 1)[-1]
    if len(line) >= MAX_TRAILER_BYTES:
        raise ValueError("snapshot-trailer-oversize")
    if (
        tail.count(b"\n" + TRAILER_PREFIX) + int(tail.startswith(TRAILER_PREFIX))
        > 1
    ):
        raise ValueError("snapshot-trailer-malformed")
    if not line.startswith(TRAILER_PREFIX):
        raise ValueError("snapshot-trailer-missing")
    return line[len(TRAILER_PREFIX) :]

def _snapshot_trailer(path: str) -> bytes:
    return _trailer_bytes(_snapshot_bytes(path))


def _decode_hex(raw: str, limit: int) -> str:
    if (
        len(raw) > limit * 2
        or len(raw) % 2
        or re.fullmatch(r"[0-9a-f]*", raw, re.ASCII) is None
    ):
        raise ValueError("snapshot-trailer-malformed")
    data = bytes.fromhex(raw)
    if b"\0" in data:
        raise ValueError("snapshot-trailer-malformed")
    try:
        return data.decode("utf-8", errors="strict")
    except UnicodeError:
        raise ValueError("snapshot-field-unsupported") from None


def _spawn_fact(spawn, name):
    if name not in spawn:
        return EnvFact("absent")
    value = spawn[name]
    if not isinstance(value, str) or "\0" in value:
        return EnvFact("unknown", reason="spawn-field-unsupported")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeError:
        return EnvFact("unknown", reason="spawn-field-unsupported")
    if len(value) > FIELD_LIMITS[name] or size > FIELD_LIMITS[name]:
        return EnvFact("unknown", reason="spawn-field-oversize")
    return EnvFact("value", value)


def _decode_trailer(trailer: bytes, spawn, excluded) -> ExecutionContext:
    try:
        parts = trailer.decode("ascii").split(" ")
    except UnicodeError:
        raise ValueError("snapshot-trailer-malformed") from None
    names = [part.partition("=")[0] for part in parts]
    expected = [
        *FIELD_NAMES,
        "NPMENV",
        "PATHALIASES",
        "SHELLHOOKS",
        "SHELLMODES",
    ]
    if names not in (expected, expected[:-1]):
        raise ValueError("snapshot-trailer-malformed")
    fields = {}
    for name, part in zip(FIELD_NAMES, parts):
        token = part.partition("=")[2]
        if name in excluded and token == "unsupported":
            fact = EnvFact("unknown", reason="snapshot-excluded-state-unsupported")
        elif name in excluded or token == "inherit":
            fact = _spawn_fact(spawn, name)
        elif token.startswith("a:"):
            fact = EnvFact("value", _decode_hex(token[2:], FIELD_LIMITS[name]))
        elif token in {"oversize", "unsupported"}:
            fact = EnvFact("unknown", reason="snapshot-field-" + token)
        else:
            raise ValueError("snapshot-trailer-malformed")
        fields[FIELD_NAMES[name]] = fact
    token = parts[5].partition("=")[2]
    names = None
    if token != "unsupported":
        if not token:
            raise ValueError("snapshot-trailer-malformed")
        decoded = "" if token == "none" else _decode_hex(token, MAX_NPM_NAMES * 129)
        raw_names = decoded.splitlines()
        if (
            len(raw_names) > MAX_NPM_NAMES
            or len(set(raw_names)) != len(raw_names)
            or (decoded and not decoded.endswith("\n"))
            or any(
                len(name) > 128 or not NPM_NAME.fullmatch(name) for name in raw_names
            )
        ):
            raise ValueError("snapshot-trailer-malformed")
        # Compare exact exported names before normalization. Exclusions are case-sensitive.
        names = {name.lower() for name in raw_names if name not in excluded}
        spawn_names = [
            name for name in spawn if isinstance(name, str) and NPM_PREFIX.match(name)
        ]
        names.update(name.lower() for name in spawn_names)
        if (
            len(names) > MAX_NPM_NAMES
            or any(len(name) > 128 for name in names)
            or any(not NPM_NAME.fullmatch(name) for name in spawn_names)
        ):
            names = None
    aliases = parts[6].partition("=")[2]
    if aliases not in {"absent", "present"}:
        raise ValueError("snapshot-trailer-malformed")
    path_safe = aliases == "absent" and not any(
        isinstance(name, str) and name.casefold() == "path" and name != "PATH"
        for name in spawn
    )
    hooks = parts[7].partition("=")[2]
    if hooks not in {"absent", "present"}:
        raise ValueError("snapshot-trailer-malformed")
    modes = parts[8].partition("=")[2] if len(parts) == 9 else "unknown"
    if modes not in {"absent", "present", "unknown"}:
        raise ValueError("snapshot-trailer-malformed")
    return ExecutionContext(
        **fields,
        npm_env_names=None if names is None else frozenset(names),
        npm_path_safe=path_safe,
        shell_startup_safe=hooks == "absent",
        shell_modes_safe=modes == "absent",
    )


def execution_context(env) -> ExecutionContext:
    """Read the snapshot owner and current scoped spawn map under its reader lock.

    Atomic publication gives a coherent snapshot. This lock does not synchronize
    Bash writers or eliminate the guard-to-spawn race (SECURITY §3.2).
    """
    from tools.environments import local

    with env._execution_context_lock:
        if not env._snapshot_ready:
            return ExecutionContext.unknown("snapshot-not-ready")
        try:
            excluded = set(env._snapshot_excluded_passthrough_names(strict=True))
            spawn = local._make_run_env(env.env)
        except (RuntimeError, OSError, ValueError, ImportError, TypeError, LookupError):
            return ExecutionContext.unknown("execution-scope-unavailable")
        # Foreground bash reads BASH_ENV before the wrapper can restore its snapshot.
        # ENV/ZDOTDIR do not initialize this noninteractive Bash spawn.
        if "BASH_ENV" in spawn:
            return ExecutionContext.unknown("shell-initialization-context-unavailable")
        try:
            return prepared_execution_context(_snapshot_bytes(env._snapshot_path), spawn, excluded)
        except FileNotFoundError:
            from tools.environments.base_session_recovery import invalidate_snapshot
            invalidate_snapshot(env, "missing")
            return ExecutionContext.unknown("snapshot-missing")
        except (OSError, AttributeError):
            return ExecutionContext.unknown("snapshot-unreadable-or-unsupported")
        except ValueError as exc:
            return ExecutionContext.unknown(str(exc))


def prepared_execution_context(snapshot: bytes, spawn, excluded) -> ExecutionContext:
    if "BASH_ENV" in spawn or any(
        set(spawn.get(name, "").split(":")) & {"keyword", "allexport"}
        for name in ("SHELLOPTS", "BASHOPTS")
    ):
        return ExecutionContext.unknown("shell-initialization-context-unavailable")
    context = _decode_trailer(_trailer_bytes(snapshot), spawn, excluded)
    if not context.shell_modes_safe:
        return ExecutionContext.unknown("shell-initialization-context-unavailable")
    return context
