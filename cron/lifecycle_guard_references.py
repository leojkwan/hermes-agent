"""Invocation-local npm/Node expansion and namespace-safe, bounded source reads.

No package manager, interpreter, or inspected program is executed. Static selectors only;
this does not claim containment of arbitrary computed JavaScript/Python/module loading.
"""
from __future__ import annotations

import errno
import json
import os
import re
import shlex
import stat
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

from cron.lifecycle_guard_lex import _iter_command_segments, _shlex_tokens


class ScanRefused(RuntimeError):
    """A named incomplete-inspection outcome, never a lifecycle accusation."""


@dataclass(frozen=True)
class SourceRead:
    status: Literal["text", "missing", "binary", "refused", "truncated"]
    text: str | None = None
    identity: str | None = None
    reason: str | None = None
    byte_count: int = 0
    had_nul: bool = False


@dataclass(frozen=True)
class Invocation:
    argv: tuple[str, ...]
    cwd: str
    package_root: str | None = None
    literal_commands: bool = False


@dataclass(frozen=True)
class Reference:
    path: str
    required: bool = False
    executed: bool = True


def _decode_source(data: bytes, identity: str | None = None) -> SourceRead:
    from cron import lifecycle_guard as guard

    if guard._has_binary_magic(data):
        return SourceRead("binary", identity=identity, byte_count=len(data), had_nul=b"\x00" in data)
    return SourceRead("text", data.replace(b"\x00", b"").decode("utf-8", errors="replace"), identity, byte_count=len(data), had_nul=b"\x00" in data)


def _read_regular(path: Path, max_bytes: int, checkpoint) -> SourceRead:
    from cron import lifecycle_guard as guard

    checkpoint()
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
    except OSError as exc:
        missing = exc.errno in (errno.ENOENT, errno.ENOTDIR, errno.ENAMETOOLONG)
        return SourceRead("missing" if missing else "refused", reason="source could not be opened")
    try:
        metadata = os.fstat(descriptor)
        identity = f"{metadata.st_dev}:{metadata.st_ino}"
        if not stat.S_ISREG(metadata.st_mode):
            return SourceRead("missing" if stat.S_ISDIR(metadata.st_mode) else "refused",
                              identity=identity, reason="not a regular file")
        prefix = os.read(descriptor, min(guard._BINARY_SNIFF_BYTES, max_bytes + 1))
        if guard._has_binary_magic(prefix):
            return SourceRead("binary", identity=identity, byte_count=len(prefix))
        if metadata.st_size > max_bytes:
            return SourceRead("truncated", identity=identity, reason=f"source exceeds {max_bytes} byte read allowance", byte_count=len(prefix))
        chunks, size = [prefix], len(prefix)
        while size <= max_bytes:
            checkpoint()
            chunk = os.read(descriptor, min(65536, max_bytes + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
        if size > max_bytes:
            return SourceRead("truncated", identity=identity, reason=f"source exceeds {max_bytes} byte read allowance", byte_count=size)
        return _decode_source(b"".join(chunks), identity)
    finally:
        os.close(descriptor)


def credential_source(path: Path) -> bool:
    name = path.name.casefold()
    if name in {".npmrc", ".netrc", ".pypirc", ".git-credentials", ".env"} or name.startswith(".env."):
        return True
    return bool({".ssh", ".aws", ".gnupg", ".kube"}.intersection(path.parts))


def read_local_source(path: Path, *, max_bytes: int, checkpoint=lambda: None) -> SourceRead:
    from cron import lifecycle_guard as guard
    from hermes_cli.sqlite_safe_read import LiveConnectionError, offline_file_access

    if "\x00" in str(path):
        return SourceRead("missing")
    if credential_source(path) or credential_source(guard._resolve_lenient(path)):
        return SourceRead("refused", reason="credential-bearing configuration source refused before open")
    if guard._on_cloud_path(path):
        return SourceRead("refused", reason="cloud-synced path refused before open")
    try:
        with offline_file_access(path, what="read referenced source"):
            return _read_regular(path, max_bytes, checkpoint)
    except LiveConnectionError:
        return SourceRead("refused", reason="SQLite database open in this gateway process")
    except (OSError, ValueError) as exc:
        return SourceRead("refused", reason=f"source read failed ({type(exc).__name__})")


_DYNAMIC = re.compile(r"[$`*?{}\x00]")
_NPM_SHORTCUTS = frozenset({"test", "start", "stop", "restart"})
_NPM_FLAGS = frozenset({"-s", "--silent", "--ignore-scripts", "--no-ignore-scripts",
                        "--if-present", "--foreground-scripts", "--no-progress", "--color", "--no-color"})
_NPM_VALUES = frozenset({"--prefix", "--loglevel", "--cache", "--userconfig", "--globalconfig"})
_NODE_VALUES = frozenset({"--conditions", "-C", "--inspect-port", "--title", "--stack-trace-limit",
                         "--max-old-space-size", "--max-semi-space-size", "--input-type"})
_NODE_FLAGS = frozenset({"--no-warnings", "--trace-warnings", "--enable-source-maps", "--disable-warning",
                        "--experimental-default-type=module", "--experimental-default-type=commonjs",
                        "--unhandled-rejections=strict", "--unhandled-rejections=warn",
                        "--inspect", "--inspect-brk", "--no-deprecation", "--trace-deprecation"})
_NODE_BUILTINS = frozenset({"assert", "async_hooks", "buffer", "child_process", "cluster", "console",
    "constants", "crypto", "dgram", "diagnostics_channel", "dns", "domain", "events", "fs", "http",
    "http2", "https", "inspector", "module", "net", "os", "path", "perf_hooks", "process", "punycode",
    "querystring", "readline", "repl", "stream", "string_decoder", "sys", "timers", "tls", "trace_events",
    "tty", "url", "util", "v8", "vm", "wasi", "worker_threads", "zlib",
    "fs/promises", "dns/promises", "timers/promises", "readline/promises", "stream/promises",
    "stream/consumers", "stream/web", "util/types", "assert/strict", "inspector/promises", "path/posix", "path/win32"})


def static_selector(value: str, kind: str) -> str:
    if not value or _DYNAMIC.search(value):
        raise ScanRefused(f"unresolved dynamic {kind}")
    return value


def anchored(value: str, cwd: str) -> str:
    static_selector(value, "path selector")
    if value.startswith("~"):
        raise ScanRefused("home-relative selector requires explicit execution home")
    return os.path.normpath(value if os.path.isabs(value) else os.path.join(cwd, value))


def validate_backend_source(result: SourceRead) -> None:
    if result.status not in {"text", "missing", "binary", "refused", "truncated"}:
        raise ScanRefused("invalid backend source status")
    if not isinstance(result.byte_count, int) or result.byte_count < 0:
        raise ScanRefused("invalid backend source byte count")
    if result.status == "text" and not isinstance(result.text, str):
        raise ScanRefused("invalid backend source text")


def node_option_parts(token: str) -> tuple[str, str, str]:
    option, equals, value = token.partition("=")
    if not equals and token.startswith(("-r", "-e", "-p")) and len(token) > 2 and not token.startswith("--"):
        return token[:2], "=", token[2:]
    return option, equals, value


class ReferenceScanner:
    def __init__(self, budget, remote_reader, remote_namespace: bool, execution_options: str | None):
        self.budget = budget
        self.remote_reader = remote_reader
        self.remote_namespace = remote_namespace
        self.execution_options = execution_options
        self.reads: dict[str, SourceRead] = {}
        self.remote_paths: set[str] = set()
        self.completed: set[tuple] = set()
        self.active_files: set[tuple] = set()
        self.active_aliases: set[tuple[str, str]] = set()

    def read(self, path: str, *, metadata: bool = False) -> SourceRead:
        from cron import lifecycle_guard as guard

        if credential_source(Path(path)):
            return SourceRead("refused", reason="credential-bearing configuration source refused before backend read")
        # Remote lexical identity must never resolve a coincident host symlink.
        key = os.path.normpath(path) if self.remote_namespace else str(guard._resolve_lenient(Path(path)))
        if metadata and (self.remote_namespace or key in self.remote_paths):
            if not callable(getattr(self.remote_reader, "read_source", None)) or not getattr(self.remote_reader, "supports_metadata", True):
                raise ScanRefused("remote metadata/source capability unavailable for selected expansion")
        if key in self.reads:
            return self.reads[key]
        if not self.budget.charge_path():
            raise ScanRefused("paths budget exhausted (1024 paths)")
        self.budget.reached_paths.append(path if len(path) <= 512 else path[:509] + "...")
        limit = min(guard._MAX_REFERENCED_SCRIPT_BYTES, self.budget.bytes_remaining)
        if limit <= 0:
            raise ScanRefused("source byte budget exhausted (16 MiB scan work)")
        if self.remote_namespace:
            self.remote_paths.add(key)
            result = self.read_remote(path, limit, metadata)
        else:
            result = read_local_source(Path(path), max_bytes=limit, checkpoint=self.budget.checkpoint)
            if result.status == "missing" and self.remote_reader:
                self.remote_paths.add(key)
                result = self.read_remote(path, limit, metadata)
        if result.status == "text":
            if not self.budget.charge_text(result.text or "", raw_bytes=result.byte_count):
                result = SourceRead("refused", identity=result.identity,
                                    reason="source text budget exhausted (16 MiB / 262144 lines / 64 KiB physical line)")
        elif result.byte_count:
            if result.byte_count > self.budget.bytes_remaining:
                raise ScanRefused("source byte budget exhausted (16 MiB scan work)")
            self.budget.bytes_remaining -= result.byte_count
        self.reads[key] = result
        return result

    def read_remote(self, path: str, limit: int, metadata: bool) -> SourceRead:
        if not self.budget.charge_remote_read():
            raise ScanRefused("remote reads budget exhausted (64 backend reads)")
        reader = self.remote_reader
        if reader is None:
            raise ScanRefused("remote source reader unavailable")
        typed = getattr(reader, "read_source", None)
        if metadata and (typed is None or not getattr(reader, "supports_metadata", True)):
            raise ScanRefused("remote metadata capability unavailable for selected expansion")
        try:
            if typed:
                result = typed(path, max_bytes=limit, timeout=max(0, self.budget.deadline - time.monotonic()))
                if not isinstance(result, SourceRead):
                    raise ScanRefused("invalid typed backend source result")
            else:
                text = reader(path)
                result = SourceRead("missing") if text is None else SourceRead("text", text)
        except (TimeoutError, OSError) as exc:
            raise ScanRefused(f"backend source read failed ({type(exc).__name__})") from exc
        self.budget.checkpoint()
        validate_backend_source(result)
        if result.status == "text":
            data = (result.text or "").encode("utf-8", errors="replace")
            if max(len(data), result.byte_count) > limit:
                return SourceRead("truncated", reason=f"backend source exceeds {limit} byte read allowance")
            decoded = _decode_source(data, result.identity)
            result = replace(decoded, byte_count=max(result.byte_count, decoded.byte_count),
                             had_nul=result.had_nul or decoded.had_nul)
        return result

    def file(self, reference: Reference, cwd: str, depth: int, package_root=None) -> bool:
        from cron import lifecycle_guard as guard

        if depth > guard._MAX_REFERENCED_SCRIPT_DEPTH:
            raise ScanRefused("reference depth budget exhausted (depth 8)")
        try:
            result = self.read(reference.path, metadata=reference.required)
            if result.status in {"refused", "truncated"}:
                raise ScanRefused(f"{reference.path}: {result.reason or result.status}")
            if result.status == "missing" and reference.required:
                raise ScanRefused(f"selected source missing or dangling: {reference.path}")
            if result.status != "text":
                return False
            identity = result.identity or reference.path
            # A completed mention is not a completed executed inspection.
            key = (identity, cwd, package_root, reference.executed)
            if key in self.completed or key in self.active_files:
                return False
            self.active_files.add(key)
            try:
                actual = reference.path if self.remote_namespace else str(guard._resolve_lenient(Path(reference.path)))
                source = Path(actual).suffix in {".js", ".cjs", ".mjs", ".ts", ".py"}
                first_line = (result.text or "").partition("\n")[0]
                source = source or (first_line.startswith("#!") and bool(re.search(r"\b(?:node|python[0-9.]*)\b", first_line)))
                if first_line.startswith("#!") and re.search(r"\bnode\b", first_line):
                    shebang = _shlex_tokens(first_line[2:])
                    node_index = next(i for i, token in enumerate(shebang) if guard._executable_name(token) == "node")
                    if self.node(Invocation(("node", *shebang[node_index + 1:], "-e", "1"), cwd, package_root), depth):
                        return True
                unsafe = self.scan(result.text or "", str(Path(actual).parent), depth, package_root,
                                   source=source, execution_cwd=cwd)
            finally:
                self.active_files.remove(key)
            self.completed.add(key)
            return unsafe
        except ScanRefused:
            if reference.executed:
                raise
            return False

    def metadata(self, path: str) -> dict | None:
        result = self.read(path, metadata=True)
        if result.status == "missing":
            return None
        if result.had_nul:
            raise ScanRefused(f"malformed package metadata (NUL byte): {path}")
        if result.status != "text":
            raise ScanRefused(f"required package metadata unreadable: {path} ({result.reason or result.status})")
        try:
            data = json.loads(result.text or "")
        except (ValueError, RecursionError) as exc:
            raise ScanRefused(f"malformed package metadata: {path}") from exc
        if not isinstance(data, dict):
            raise ScanRefused(f"malformed package metadata: {path}")
        return data

    def package(self, cwd: str) -> tuple[str, dict]:
        for parent in (Path(cwd), *Path(cwd).parents):
            data = self.metadata(str(parent / "package.json"))
            if data is not None:
                return str(parent), data
        raise ScanRefused("required package.json not found by bounded upward lookup")

    def npm(self, invocation: Invocation, depth: int) -> bool:
        args = list(invocation.argv[1:])
        cwd, selected, forwarded, index = invocation.cwd, None, [], 0
        while index < len(args):
            token = args[index]
            index += 1
            if token == "--":
                if selected is None:
                    raise ScanRefused("npm script selector missing")
                forwarded = args[index:]
                break
            option, equals, value = token.partition("=")
            if option in _NPM_FLAGS:
                if equals and value not in {"true", "false"}:
                    raise ScanRefused("unsupported npm boolean option selection")
                continue
            if option in _NPM_VALUES:
                if not equals:
                    if index == len(args):
                        raise ScanRefused("npm option value missing")
                    value, index = args[index], index + 1
                if option == "--prefix":
                    cwd = anchored(value, invocation.cwd)
                elif option in {"--userconfig", "--globalconfig"}:
                    raise ScanRefused("unsupported npm config selection")
                continue
            if token.startswith("-"):
                raise ScanRefused(f"unsupported npm workspace/shell/config selection: {option}")
            if token in {"run", "run-script"} and selected is None:
                selected = ""
            elif selected == "" or (selected is None and token in _NPM_SHORTCUTS):
                selected = static_selector(token, "npm script name")
            elif selected is None:
                # Other npm operations are not statically selected script invocations.
                return False
            else:
                forwarded.append(token)
        if not selected:
            raise ScanRefused("npm script selector missing or unresolved")
        if self.argument_references(forwarded, invocation, depth):
            return True
        return self.npm_script(cwd, selected, forwarded, depth)

    def npm_script(self, cwd: str, selected: str, forwarded: list[str], depth: int) -> bool:
        root, data = self.package(cwd)
        scripts = data.get("scripts")
        if not isinstance(scripts, dict) or not isinstance(scripts.get(selected), str) or not scripts[selected]:
            raise ScanRefused(f"required npm script missing or malformed: {selected}")
        alias = (root, selected)
        if alias in self.active_aliases:
            raise ScanRefused(f"npm package-alias cycle: {selected}")
        self.active_aliases.add(alias)
        try:
            for name in ("pre" + selected, selected, "post" + selected):
                if name not in scripts:
                    continue
                payload = scripts[name]
                if not isinstance(payload, str):
                    raise ScanRefused(f"malformed npm hook: {name}")
                if name == selected and forwarded:
                    payload += " " + " ".join(shlex.quote(arg) for arg in forwarded)
                if self.nested(payload, root, depth, root):
                    return True
            return False
        finally:
            self.active_aliases.remove(alias)

    def nested(self, text: str, cwd: str, depth: int, package_root=None, *, source=False) -> bool:
        if not self.budget.charge_text(text):
            raise ScanRefused("expanded source text budget exhausted")
        return self.scan(text, cwd, depth + 1, package_root, source=source)

    def preload(self, value: str, cwd: str, depth: int, package_root=None) -> bool:
        value = static_selector(value, "Node preload selector")
        if value.removeprefix("node:") in _NODE_BUILTINS:
            return False
        if value.startswith("node:"):
            raise ScanRefused("unsupported Node builtin selector")
        if value.startswith("file:"):
            from urllib.parse import unquote, urlparse
            parsed = urlparse(value)
            if parsed.netloc not in ("", "localhost") or parsed.query or parsed.fragment:
                raise ScanRefused("unsupported Node preload URL routing")
            value = unquote(parsed.path)
        if value.startswith((".", "/")):
            target = anchored(value, cwd)
        else:
            target = self.module(value, cwd)
        for suffix in ("", ".js", ".cjs", ".json", "/index.js"):
            path = target + suffix
            if self.read(path).status != "missing":
                return self.file(Reference(path, required=True), cwd, depth + 1, package_root)
        raise ScanRefused(f"selected Node preload missing: {value}")

    def module(self, value: str, cwd: str) -> str:
        for parent in (Path(cwd), *Path(cwd).parents):
            target = str(parent / "node_modules" / value)
            for suffix in ("", ".js", ".cjs", ".json", ".node"):
                result = self.read(target + suffix)
                if result.status != "missing":
                    return target + suffix
            data = self.metadata(str(Path(target) / "package.json"))
            if data is not None:
                main = data.get("main", "index.js")
                if "exports" in data or not isinstance(main, str):
                    raise ScanRefused("unsupported Node package exports routing")
                return anchored(main, target)
            if self.read(target + "/index.js").status != "missing":
                return target + "/index.js"
        raise ScanRefused(f"selected Node module unresolved: {value}")

    def node(self, invocation: Invocation, depth: int, *, injected=False) -> bool:
        args, index, inline = list(invocation.argv[1:]), 0, False
        if not injected and self.execution_options is None:
            raise ScanRefused("execution-affecting Node options unavailable from actual execution context")
        if not injected and self.execution_options:
            if not self.budget.charge_text(self.execution_options):
                raise ScanRefused("Node execution options source budget exhausted")
            options = _shlex_tokens(self.execution_options)
            if self.node(Invocation(("node", *options), invocation.cwd, invocation.package_root), depth, injected=True):
                return True
        while index < len(args):
            token = args[index]
            index += 1
            if token == "--":
                break
            if not token.startswith("-") or token == "-":
                index -= 1
                break
            option, equals, value = node_option_parts(token)
            if option in {"-r", "--require", "--import", "-e", "--eval", "-p", "--print"}:
                if not equals:
                    if index >= len(args):
                        raise ScanRefused("Node option value missing")
                    value, index = args[index], index + 1
                if self.node_payload(option, value, invocation, depth, injected):
                    return True
                inline = inline or option in {"-e", "--eval", "-p", "--print"}
            elif option in _NODE_VALUES:
                if not equals:
                    if index >= len(args):
                        raise ScanRefused("Node option value missing")
                    index += 1
            elif token in _NODE_FLAGS or option in {"--inspect", "--inspect-brk"}:
                continue
            else:
                raise ScanRefused(f"unsupported Node execution routing: {option}")
        return self.node_entry(args[index:], invocation, depth, inline=inline, injected=injected)

    def node_entry(self, args, invocation: Invocation, depth: int, *, inline: bool, injected: bool) -> bool:
        if args:
            if inline:
                return self.argument_references(args, invocation, depth)
            if injected:
                raise ScanRefused("unresolved execution-affecting NODE_OPTIONS")
            entry = args[0]
            if entry == "-":
                raise ScanRefused("Node stdin source unavailable")
            path = anchored(entry, invocation.cwd)
            if self.file(Reference(path, required=True), invocation.cwd, depth + 1, invocation.package_root):
                return True
            return self.argument_references(args[1:], invocation, depth)
        if not injected and not inline:
            raise ScanRefused("Node entry selector unavailable (stdin/REPL)")
        return False

    def node_payload(self, option: str, value: str, invocation: Invocation, depth: int, injected: bool) -> bool:
        if option in {"-r", "--require", "--import"}:
            return self.preload(value, invocation.cwd, depth, invocation.package_root)
        if injected:
            raise ScanRefused("unsupported inline execution in NODE_OPTIONS")
        return self.nested(value, invocation.cwd, depth, invocation.package_root, source=True)

    def argument_references(self, args, invocation: Invocation, depth: int) -> bool:
        # Arguments may be shell/code strings. Scan their text without interpreting them as
        # a new command invocation ("npm" in a data argument must not select a package).
        from cron import lifecycle_guard as guard

        for arg in args:
            if guard._direct_lifecycle_scan(arg):
                return True
            for path in guard._iter_referenced_shell_scripts(arg, cwd=invocation.cwd):
                if self.file(Reference(str(path)), str(path.parent), depth + 1, invocation.package_root):
                    return True
        return False

    def package_bin(self, invocation: Invocation, depth: int) -> bool:
        name = invocation.argv[0]
        if not invocation.package_root or "/" in name or name.startswith("-"):
            return False
        for parent in (Path(invocation.cwd), *Path(invocation.cwd).parents):
            path = str(parent / "node_modules" / ".bin" / name)
            result = self.read(path)
            dangling = not self.remote_namespace and os.path.lexists(path)
            if result.status != "missing" or dangling:
                return self.file(Reference(path, required=True), invocation.cwd, depth + 1, invocation.package_root)
        return False

    def scan(self, text: str, cwd: str, depth: int, package_root=None, *, source=False, execution_cwd=None) -> bool:
        from cron import lifecycle_guard as guard
        from tools.shell_heredoc import strip_inert_heredoc_bodies

        self.budget.checkpoint()
        if depth > guard._MAX_REFERENCED_SCRIPT_DEPTH:
            raise ScanRefused("reference depth budget exhausted (depth 8)")
        if guard._direct_lifecycle_scan(text):
            return True
        walk = strip_inert_heredoc_bodies(text)
        for payload in guard._iter_shell_command_payloads(walk):
            if self.nested(payload, cwd, depth, package_root):
                return True
        # Expand only actual command positions. Source literal reference discovery below is
        # deliberately additive and retains dynamicImport/require wrappers without eval.
        previous = ""
        for segment in _iter_command_segments(walk):
            literal_commands = source and bool(re.search(
                r"(?:^|\.)(?:exec|execSync|spawn|spawnSync)$|^os\.(?:system|popen)$|^subprocess\.(?:run|call|check_call|check_output|Popen)$",
                previous,
            ))
            if self.scan_segment(segment, cwd, depth, package_root, source, execution_cwd or cwd, literal_commands):
                return True
            previous = segment[-1]
        if walk != text:
            for path in guard._iter_referenced_shell_scripts(text, cwd=cwd):
                if self.file(Reference(str(path), executed=False), execution_cwd or cwd, depth + 1, package_root):
                    return True
        return False

    def scan_segment(self, segment, cwd: str, depth: int, package_root, source: bool, execution_cwd: str, literal_commands: bool) -> bool:
        from cron import lifecycle_guard as guard

        original = guard._command_token_index(segment)
        index = guard._executed_command_index(segment)
        if original is None:
            return False
        previous = self.execution_options
        options = self.invocation_options(segment[:index] if index is not None else segment)
        if options is not None:
            self.execution_options = options
        try:
            if index is not None:
                invocation = Invocation(tuple(segment[index:]), execution_cwd if source else cwd, package_root, literal_commands)
                if self.scan_invocation(invocation, segment, index, depth, source):
                    return True
            for position in dict.fromkeys((original, index if index is not None else original)):
                for path in guard._references_at(segment, position, cwd):
                    required = "/node_modules/.bin/" in str(path)
                    if self.file(Reference(str(path), required=required), execution_cwd if source else cwd, depth + 1, package_root):
                        return True
            return False
        finally:
            self.execution_options = previous

    def scan_invocation(self, invocation: Invocation, segment, index: int, depth: int, source: bool) -> bool:
        from cron import lifecycle_guard as guard

        name = guard._executable_name(invocation.argv[0])
        if source:
            if not invocation.literal_commands:
                return False
            for literal in segment:
                if self.source_literal(literal, invocation, depth):
                    return True
            return False
        if name in {"node", "nodejs"}:
            if self.node(invocation, depth):
                return True
        elif name in {"npm", "npm.cmd"}:
            if self.npm(invocation, depth):
                return True
        elif name.startswith("python") and len(invocation.argv) > 1:
            arg = invocation.argv[1]
            if not arg.startswith("-"):
                path = anchored(arg, invocation.cwd)
                if self.file(Reference(path), str(Path(path).parent), depth + 1, invocation.package_root):
                    return True
        if not source and self.package_bin(invocation, depth):
            return True
        return False

    def source_literal(self, literal: str, invocation: Invocation, depth: int) -> bool:
        from cron import lifecycle_guard as guard

        head = re.match(r"^(\S+)\s+", literal)
        if head is None:
            return False
        token = head.group(1)
        name = guard._executable_name(token)
        supported = (name in guard._SHELL_EXECUTABLES or name in guard._TRANSPARENT_COMMAND_PREFIXES
                     or name in {"npm", "node", "nodejs", "source", "."}
                     or re.fullmatch(r"python[0-9.]*", name) or "/" in token)
        return bool(supported) and self.nested(literal, invocation.cwd, depth, invocation.package_root)

    def invocation_options(self, prefixes: list[str]) -> str | None:
        selected = None
        for token in prefixes:
            if token.startswith("NODE_OPTIONS="):
                selected = token.partition("=")[2]
                if _DYNAMIC.search(selected):
                    raise ScanRefused("unresolved execution-affecting NODE_OPTIONS")
            if token.startswith(("npm_config_script_shell=", "NPM_CONFIG_SCRIPT_SHELL=")):
                raise ScanRefused("unsupported npm shell selection")
        return selected
