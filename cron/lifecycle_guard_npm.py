"""Private npm 11.19.0/11.19.1 projection, without running npm or decoding secrets.

Only a neutral configuration plus a literal builtin/CLI prefix is supported.
The generic source reader must continue denying credential-bearing files.
"""

from __future__ import annotations

import errno
import os
import re
import stat
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Literal

from hermes_platform.resolver.core import LookupContext, locate_command
from tools.environments.local_execution_context import ExecutionContext

# Qualified against npm/cli v11.19.0...v11.19.1: config and run-script unchanged.
SUPPORTED_NPM_VERSIONS = ("11.19.0", "11.19.1")
_KEY = re.compile(rb"[A-Za-z0-9_:/@.-]+", re.ASCII)
_PATH = re.compile(r"/[A-Za-z0-9_./+@ -]+", re.ASCII)
_RELEVANT = frozenset(
    {
        "node-options",
        "script-shell",
        "userconfig",
        "globalconfig",
        "prefix",
        "global",
        "location",
        "workspace",
        "workspaces",
        "include-workspace-root",
        "onload-script",
        "node-gyp",
        "shell",
    }
)
_ROUTING_EXPORTS = frozenset(
    {"npm_config_prefix", "npm_config_userconfig", "npm_config_globalconfig"}
)
_INJECTED_EXPORTS = frozenset(
    {
        "npm_config_global_prefix",
        "npm_config_local_prefix",
        "npm_config_node_gyp",
        "npm_config_npm_version",
    }
)
_FLAGS = frozenset(
    {
        "-s",
        "--silent",
        "--ignore-scripts",
        "--no-ignore-scripts",
        "--if-present",
        "--foreground-scripts",
        "--no-progress",
        "--color",
        "--no-color",
    }
)
_VALUES = frozenset({"--prefix", "--loglevel", "--cache"})
_SHORTCUTS = frozenset({"test", "start", "stop", "restart"})
# npm 11.19 cmd-list.js direct aliases and the unique test abbreviation.
# Every spelling outside this modeled subset refuses, including lifecycle commands.
_SCRIPT_COMMANDS = {
    **{name: name for name in _SHORTCUTS},
    "t": "test",
    "tst": "test",
    "tes": "test",
    "run": "run",
    "run-script": "run",
    "rum": "run",
    "urn": "run",
}
_MAX_CONFIG_BYTES = 65536
_MAX_CONFIG_LINES = 4096


@dataclass(frozen=True)
class NpmExecutionFacts:
    status: Literal["known", "refused"]
    local_prefix: str | None = None
    reason: str = ""


@dataclass(frozen=True)
class NpmSelection:
    script: str | None
    forwarded: tuple[str, ...]
    prefix: str | None = None


@dataclass(frozen=True)
class _ChildExports:
    # Identity binds generated routing facts to the exact unmodified child context.
    context: ExecutionContext = field(repr=False)
    prefix: str = field(repr=False)
    userconfig: str = field(repr=False)
    globalconfig: str = field(repr=False)
    names: frozenset[str] = field(repr=False)


def normalize_key(key: str) -> str:
    return key.lower().replace("_", "-")


def parse_selection(argv: tuple[str, ...], cwd: str) -> NpmSelection:
    from cron.lifecycle_guard_references import ScanRefused, anchored, static_selector

    if argv[1:] in {("-v",), ("--version",)}:
        return NpmSelection(None, ())
    args, selected, prefix, forwarded, index = argv[1:], None, None, [], 0
    while index < len(args):
        token = args[index]
        index += 1
        if token == "--":
            if not selected:
                raise ScanRefused("npm script selector missing")
            forwarded.extend(args[index:])
            break
        option, equals, value = token.partition("=")
        if option in _FLAGS:
            if equals and value not in {"true", "false"}:
                raise ScanRefused("unsupported npm boolean selection")
            continue
        if option in _VALUES:
            if not equals:
                if index == len(args):
                    raise ScanRefused("npm option value missing")
                value, index = args[index], index + 1
            static_selector(value, "npm option value")
            if option == "--prefix":
                if not value.isascii():
                    _fail("unsupported literal prefix")
                prefix = _absolute_literal(anchored(value.strip(), cwd))
            continue
        if token.startswith("-"):
            raise ScanRefused("unsupported npm workspace/shell/config selection")
        if selected is None:
            command = _SCRIPT_COMMANDS.get(token)
            if command is None:
                raise ScanRefused("unsupported npm subcommand selection")
            selected = "" if command == "run" else command
        elif selected == "":
            selected = static_selector(token, "npm script name")
        else:
            forwarded.append(token)
    if not selected:
        raise ScanRefused("npm script selector missing or unresolved")
    return NpmSelection(selected, tuple(forwarded), prefix)


def _fail(reason: str):
    from cron.lifecycle_guard_references import ScanRefused

    raise ScanRefused("npm projection: " + reason)


def _absolute_literal(value: str) -> str:
    if not _PATH.fullmatch(value) or value != value.strip():
        _fail("unsupported literal prefix")
    return os.path.normpath(value)


def _identity(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _charge_config(data: bytes, budget):
    lines = re.split(rb"\r\n|\r|\n", data)
    if (
        len(lines) > _MAX_CONFIG_LINES
        or len(lines) > budget.lines_remaining
        or any(len(line) > 65536 for line in lines)
    ):
        _fail("config line budget exhausted")
    budget.lines_remaining -= len(lines)
    budget.charge_tokens(len(lines))
    return lines


def _parse_config(data: bytes, builtin: bool, budget) -> str | None:
    prefix = None
    for index, line in enumerate(_charge_config(data, budget)):
        if index % 128 == 0:
            budget.checkpoint()
        line = line.strip()
        if not line or line.startswith((b"#", b";")):
            continue
        key, equals, value = line.partition(b"=")
        key = key.strip()
        if not equals or _KEY.fullmatch(key) is None:
            _fail("unsupported config key syntax")
        name = normalize_key(key.decode("ascii"))
        if name == "prefix" and builtin and key == b"prefix":
            if prefix is not None:
                _fail("ambiguous builtin prefix")
            try:
                prefix = _absolute_literal(value.strip().decode("ascii"))
            except UnicodeError:
                _fail("unsupported literal prefix")
        elif name in _RELEVANT:
            _fail("execution-affecting config key")
    return prefix


def _read_config(path: Path, *, builtin: bool, budget) -> str | None:
    from cron import lifecycle_guard as guard
    from hermes_cli.sqlite_safe_read import LiveConnectionError, offline_file_access

    budget.checkpoint()
    if guard._on_cloud_path(path):
        _fail("cloud config path")
    if not budget.charge_path():
        _fail("config path budget exhausted")
    try:
        with offline_file_access(path, what="project npm execution configuration"):
            return _open_config(path, builtin, budget)
    except LiveConnectionError:
        _fail("live SQLite config path")
    except (OSError, ValueError):
        _fail("config unavailable or unsupported")


def _open_config(path: Path, builtin: bool, budget) -> str | None:
    try:
        descriptor = os.open(
            path, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0)
        )
    except OSError as exc:
        if exc.errno == errno.ENOENT and not path.is_symlink():
            return None
        _fail("config unavailable or symlink")
    try:
        before = os.fstat(descriptor)
        limit = min(_MAX_CONFIG_BYTES, budget.bytes_remaining)
        if not stat.S_ISREG(before.st_mode):
            _fail("config is not regular")
        if before.st_size > limit:
            _fail("config byte budget exhausted")
        chunks, size = [], 0
        while size <= limit:
            budget.checkpoint()
            chunk = os.read(descriptor, min(16384, limit + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            budget.bytes_remaining -= len(chunk)
        if size > limit:
            _fail("config byte budget exhausted")
        if _identity(before) != _identity(os.fstat(descriptor)) or _identity(
            before
        ) != _identity(os.stat(path, follow_symlinks=False)):
            _fail("config changed during read")
        data = b"".join(chunks)
        if b"\0" in data or data.startswith(b"SQLite format 3"):
            _fail("binary or SQLite config")
        return _parse_config(data, builtin, budget)
    finally:
        os.close(descriptor)


class NpmProjection:
    def __init__(
        self,
        scanner,
        selection: NpmSelection,
        argv: tuple[str, ...],
        cwd: str,
        inherited: _ChildExports | None,
    ):
        self.scanner = scanner
        self.selection = selection
        self.argv = argv
        self.cwd = cwd
        self.context = scanner.options
        self.inherited = (
            inherited
            if inherited is not None and inherited.context is self.context
            else None
        )
        self.children: _ChildExports | None = None

    def project(self) -> NpmExecutionFacts:
        from cron.lifecycle_guard_references import ScanRefused

        try:
            return self._project()
        except ScanRefused as exc:
            # Keep metadata classification while discarding private routing paths.
            reason = str(exc)
            if reason.startswith(
                (
                    "malformed package metadata: ",
                    "malformed package metadata (NUL byte): ",
                )
            ):
                reason = "npm projection: malformed package metadata"
            elif not reason.startswith("npm projection: "):
                reason = "npm projection: package metadata or work budget unavailable"
            return NpmExecutionFacts("refused", reason=reason)

    def _environment(self):
        from hermes_platform.host.facts import os_family

        context = self.context
        if self.scanner.remote_namespace:
            _fail("remote execution config capability unavailable")
        if os_family() not in {"darwin", "linux"}:
            _fail("unsupported npm platform")
        if (
            context.home.status != "value"
            or not context.home.value
            or not os.path.isabs(context.home.value)
        ):
            _fail("HOME context unavailable")
        if context.path.status != "value" or not context.path.value:
            _fail("PATH context unavailable")
        if not context.npm_path_safe:
            _fail("npm PATH aliases unavailable or unsupported")
        paths = context.path.value.split(os.pathsep)
        if any(not os.path.isabs(path) or "\0" in path for path in paths):
            _fail("relative or empty PATH entry")
        if context.prefix.status != "absent" or context.destdir.status != "absent":
            _fail("PREFIX or DESTDIR context unsupported")
        if context.npm_env_names is None:
            _fail("npm environment names unavailable")
        generated = self.inherited.names if self.inherited else frozenset()
        for name in context.npm_env_names:
            if name in generated:
                continue
            key = name[len("npm_config_") :]
            if not re.fullmatch(r"[A-Za-z0-9_-]+", key):
                _fail("unsupported environment config name")
            if normalize_key(key) in _RELEVANT:
                _fail("execution-affecting environment config")

    def _implementation(self) -> tuple[Path, Path]:
        from cron import lifecycle_guard as guard

        context = LookupContext(path=self.context.path.value)
        if self.argv[0] not in {"npm", "npm.cmd"}:
            _absolute_literal(self.argv[0])
        npm = locate_command(self.argv[0], context)
        node = locate_command("node", context)
        if not npm.found or not node.found:
            _fail("npm or node executable unavailable")
        npm_path, node_path = (
            Path(npm.command[0]).resolve(),
            Path(node.command[0]).resolve(),
        )
        if npm_path.name != "npm-cli.js" or npm_path.parent.name != "bin":
            _fail("unsupported npm implementation")
        if node_path.name != "node":
            _fail("unsupported node executable")
        root = npm_path.parent.parent
        metadata = self.scanner.metadata(str(root / "package.json"))
        if (
            not metadata
            or metadata.get("name") != "npm"
            or metadata.get("version") not in SUPPORTED_NPM_VERSIONS
        ):
            _fail(
                "unsupported npm version; reviewed versions: 11.19.0, 11.19.1; review this version before use"
            )
        if guard._on_cloud_path(node_path):
            _fail("cloud node executable")
        # Scripts and version-manager shims cannot attest process.execPath.
        if not self.scanner.budget.charge_path():
            _fail("executable path budget exhausted")
        self.scanner.budget.checkpoint()
        try:
            descriptor = os.open(
                node_path, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0)
            )
            try:
                info = os.fstat(descriptor)
                if not stat.S_ISREG(info.st_mode):
                    _fail("node executable is not regular")
                magic = os.read(descriptor, 4)
            finally:
                os.close(descriptor)
        except OSError:
            _fail("node executable unreadable")
        self.scanner.budget.bytes_remaining -= len(magic)
        if self.scanner.budget.bytes_remaining < 0:
            _fail("executable byte budget exhausted")
        if magic not in {
            b"\x7fELF",
            b"\xcf\xfa\xed\xfe",
            b"\xce\xfa\xed\xfe",
            b"\xfe\xed\xfa\xcf",
            b"\xfe\xed\xfa\xce",
            b"\xca\xfe\xba\xbe",
            b"\xbe\xba\xfe\xca",
        }:
            _fail("unsupported node executable")
        return root, node_path

    def _local_prefix(self) -> str:
        explicit = self.selection.prefix
        candidate = Path(explicit or self.cwd).resolve()
        found = None
        for parent in (candidate, *candidate.parents):
            data = self.scanner.metadata(str(parent / "package.json"))
            if data is not None and "workspaces" in data:
                _fail("workspace ancestry unsupported")
            if found is not None:
                continue
            if explicit is not None:
                if data is None:
                    _fail("required package metadata missing")
                found = parent
            elif data is not None or self._node_modules(parent):
                if data is None:
                    _fail("node_modules boundary lacks package metadata")
                found = parent
        if found is None:
            if self.selection.script is None:
                return str(candidate)
            _fail("required package metadata missing")
        return str(found)

    def _node_modules(self, parent: Path) -> bool:
        from cron import lifecycle_guard as guard

        path = parent / "node_modules"
        self.scanner.budget.checkpoint()
        if guard._on_cloud_path(path):
            _fail("cloud package boundary")
        if not self.scanner.budget.charge_path():
            _fail("package path budget exhausted")
        try:
            info = path.stat()
        except FileNotFoundError:
            return False
        except OSError:
            _fail("node_modules boundary unavailable")
        return stat.S_ISDIR(info.st_mode)

    def _project(self) -> NpmExecutionFacts:
        self._environment()
        root, node = self._implementation()
        budget = self.scanner.budget
        builtin_prefix = _read_config(root / "npmrc", builtin=True, budget=budget)
        local = self._local_prefix()
        inherited = self.inherited
        prefix = (
            self.selection.prefix
            or (inherited.prefix if inherited else None)
            or builtin_prefix
            or str(node.parent.parent)
        )
        user = (
            inherited.userconfig
            if inherited
            else str(Path(self.context.home.value) / ".npmrc")
        )
        # npm exports globalconfig after evaluating its prefix getter. A nested CLI
        # prefix does not override this generated environment setting.
        global_config = (
            inherited.globalconfig if inherited else str(Path(prefix) / "etc/npmrc")
        )
        for path in (Path(local) / ".npmrc", Path(user), Path(global_config)):
            _read_config(path, builtin=False, budget=budget)
        generated = _INJECTED_EXPORTS | _ROUTING_EXPORTS
        names = self.context.npm_env_names | generated
        bins = [
            str(parent / "node_modules/.bin")
            for parent in (Path(local), *Path(local).parents)
        ]
        # @npmcli/run-script also injects its node-gyp-bin before the inherited PATH.
        bins.append(str(root / "node_modules/@npmcli/run-script/lib/node-gyp-bin"))
        from tools.environments.local_execution_context import EnvFact

        child_context = replace(
            self.context,
            path=EnvFact("value", os.pathsep.join([*bins, self.context.path.value])),
            npm_env_names=names,
        )
        self.children = _ChildExports(
            child_context, prefix, user, global_config, generated
        )
        return NpmExecutionFacts("known", local)
