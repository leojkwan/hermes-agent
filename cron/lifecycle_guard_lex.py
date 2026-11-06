"""Linear POSIX shell views shared by every lifecycle-policy pass.

This is a lexical inspection, not a shell evaluator. Buffers grow as lists; a scope caches
views only for one admission. Malformed logical lines retain the physical-line fallback.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import re
from typing import Iterator

_CONTROL_CHARS = frozenset(";&|()")
_PARAMETER_ASSIGNMENT = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*):?=")
_PARAMETER_OFFSET = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*:(?![-=?+])")
_PARAMETER_ARRAY = re.compile(r"\$\{[^${}\n]*\[")
_DESCRIPTOR_ASSIGNMENT = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}(?=[<>])")


class ShellToken(str):
    """Retain syntax needed for shell classification without reinterpreting literals."""

    def __new__(cls, value, quoted=False, assignments=()):
        token = super().__new__(cls, value)
        token.quoted = quoted
        token.assignments = tuple(assignments)
        return token


def _remember_parameter_assignment(assignments, line, index, quote):
    if quote != "'" and line[index] == "$":
        match = _PARAMETER_ASSIGNMENT.match(line, index)
        if match:
            assignments.append(match.group(1))
        if line.startswith(("$((", "$["), index) or _PARAMETER_ARRAY.match(line, index) or _PARAMETER_OFFSET.match(line, index):
            assignments.append("*")
    elif not quote and line[index] == "{":
        match = _DESCRIPTOR_ASSIGNMENT.match(line, index)
        if match:
            assignments.append(match.group(1))


@dataclass
class LexicalWork:
    budget: object
    tokens: dict[str, list[str] | None] = field(default_factory=dict)
    segments: dict[str, list[list[str]]] = field(default_factory=dict)


_WORK: ContextVar[LexicalWork | None] = ContextVar("lifecycle_lexical_work", default=None)


@contextmanager
def lexical_scope(budget):
    token = _WORK.set(LexicalWork(budget))
    try:
        yield
    finally:
        _WORK.reset(token)


def _split_logical_lines(text: str) -> list[str]:
    lines, current = [], []
    quote = ""
    escape = comment = active = False
    work = _WORK.get()
    for index, ch in enumerate(text):
        if work and index % 4096 == 0:
            work.budget.checkpoint()
        if comment:
            if ch == "\n":
                comment = False
            else:
                current.append(ch)
                continue
        if escape:
            escape = False
        elif ch == "\\" and quote != "'":
            escape = True
        elif ch in "'\"" and (not quote or ch == quote):
            quote = "" if quote else ch
            active = True
        elif not quote:
            if ch == "#" and not active:
                comment = True
            elif ch == "\n":
                lines.append("".join(current))
                current = []
                active = False
                continue
            else:
                active = not (ch.isspace() or ch in _CONTROL_CHARS)
        current.append(ch)
    if current:
        lines.append("".join(current))
    return lines


def _double_quoted_escape(following):
    if following == "\n":
        return []
    return [following] if following in ('"', "\\") else ["\\", following]


def _unquoted_escape(following):
    return [] if following == "\n" else [following]


def _lex(line: str) -> list[str]:
    tokens: list[str] = []
    word: list[str] = []
    quoted, assignments = False, []
    quote, active, index, charged = "", False, 0, 0
    work = _WORK.get()
    while index < len(line):
        if work and index % 4096 == 0:
            work.budget.charge_tokens(len(tokens) - charged)
            charged = len(tokens)
        ch = line[index]
        _remember_parameter_assignment(assignments, line, index, quote)
        index += 1
        if quote:
            if ch == quote:
                quote = ""
            elif ch == "\\" and quote == '"':
                if index == len(line):
                    raise ValueError("No escaped character")
                following = line[index]
                index += 1
                word.extend(_double_quoted_escape(following))
            else:
                word.append(ch)
            continue
        if ch in "'\"":
            quote, active = ch, True
            quoted = True
        elif ch == "\\":
            quoted = True
            if index == len(line):
                raise ValueError("No escaped character")
            following = line[index]
            index += 1
            word.extend(_unquoted_escape(following))
            active = active or following != "\n"
        elif ch == "#" and not active:
            end = line.find("\n", index)
            index = len(line) if end == -1 else end
        elif ch.isspace() or ch in _CONTROL_CHARS:
            if active:
                tokens.append(ShellToken("".join(word), quoted, assignments))
                word, active = [], False
                quoted, assignments = False, []
            if ch in _CONTROL_CHARS:
                start = index - 1
                while index < len(line) and line[index] in _CONTROL_CHARS:
                    if work and index % 4096 == 0:
                        work.budget.checkpoint()
                    index += 1
                tokens.append(line[start:index])
        else:
            word.append(ch)
            active = True
    if quote:
        raise ValueError("No closing quotation")
    if active:
        tokens.append(ShellToken("".join(word), quoted, assignments))
    if work:
        work.budget.charge_tokens(len(tokens) - charged)
    return tokens


def _shlex_tokens(line: str) -> list[str]:
    work = _WORK.get()
    if work and line in work.tokens:
        result = work.tokens[line]
        if result is None:
            raise ValueError("Malformed shell view")
        work.budget.checkpoint()
        return result
    try:
        result = _lex(line)
    except ValueError:
        if work:
            work.tokens[line] = None
        raise
    if work:
        work.tokens[line] = result
    return result


def _split_segments(tokens: list[str], *, keep_controls: bool = False) -> Iterator[list[str]]:
    segment: list[str] = []
    for token in tokens:
        if token and not getattr(token, "quoted", False) and set(token) <= _CONTROL_CHARS:
            if segment:
                yield segment
                segment = []
            if keep_controls:
                yield [token]
        else:
            segment.append(token)
    if segment:
        yield segment


def _iter_command_segments(command: str, *, keep_controls: bool = False) -> Iterator[list[str]]:
    work = _WORK.get()
    if work and command in work.segments:
        yield from _bounded_segments(work.segments[command], work, keep_controls)
        return
    segments: list[list[str]] = []
    for line in _split_logical_lines(command):
        try:
            segments.extend(_split_segments(_shlex_tokens(line), keep_controls=True))
        except ValueError:
            for physical in line.splitlines():
                try:
                    segments.extend(_split_segments(_shlex_tokens(physical), keep_controls=True))
                except ValueError:
                    continue
    if work:
        work.segments[command] = segments
    yield from _bounded_segments(segments, work, keep_controls)


def _bounded_segments(segments, work, keep_controls):
    for index, segment in enumerate(segments):
        if work and index % 256 == 0:
            work.budget.checkpoint()
        if keep_controls or not (len(segment) == 1 and set(segment[0]) <= _CONTROL_CHARS):
            yield segment
