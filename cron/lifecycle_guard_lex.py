"""Linear POSIX shell views shared by every lifecycle-policy pass.

This is a lexical inspection, not a shell evaluator. Buffers grow as lists; a scope caches
views only for one admission. Malformed logical lines retain the physical-line fallback.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Iterator

_CONTROL_CHARS = frozenset(";&|()")


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
    escape = False
    work = _WORK.get()
    for index, ch in enumerate(text):
        if work and index % 4096 == 0:
            work.budget.checkpoint()
        if escape:
            escape = False
        elif ch == "\\":
            escape = True
        elif ch in "'\"" and (not quote or ch == quote):
            quote = "" if quote else ch
        elif ch == "\n" and not quote:
            lines.append("".join(current))
            current = []
            continue
        current.append(ch)
    if current:
        lines.append("".join(current))
    return lines


def _lex(line: str) -> list[str]:
    tokens: list[str] = []
    word: list[str] = []
    quote, active, index, charged = "", False, 0, 0
    work = _WORK.get()
    while index < len(line):
        if work and index % 4096 == 0:
            work.budget.charge_tokens(len(tokens) - charged)
            charged = len(tokens)
        ch = line[index]
        index += 1
        if quote:
            if ch == quote:
                quote = ""
            elif ch == "\\" and quote == '"':
                if index == len(line):
                    raise ValueError("No escaped character")
                following = line[index]
                index += 1
                if following not in ('"', "\\"):
                    word.append("\\")
                word.append(following)
            else:
                word.append(ch)
            continue
        if ch in "'\"":
            quote, active = ch, True
        elif ch == "\\":
            if index == len(line):
                raise ValueError("No escaped character")
            word.append(line[index])
            index += 1
            active = True
        elif ch == "#":
            end = line.find("\n", index)
            index = len(line) if end == -1 else end
        elif ch.isspace() or ch in _CONTROL_CHARS:
            if active:
                tokens.append("".join(word))
                word, active = [], False
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
        tokens.append("".join(word))
    if work:
        work.budget.charge_tokens(len(tokens) - charged)
    return tokens


def _shlex_tokens(line: str) -> list[str]:
    work = _WORK.get()
    if work and line in work.tokens:
        result = work.tokens[line]
        if result is None:
            raise ValueError("Malformed shell view")
        work.budget.charge_tokens(len(result))
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
        if token and set(token) <= _CONTROL_CHARS:
            if segment:
                yield segment
                segment = []
            if keep_controls:
                yield [token]
        else:
            segment.append(token)
    if segment:
        yield segment


def _iter_command_segments(command: str) -> Iterator[list[str]]:
    work = _WORK.get()
    if work and command in work.segments:
        yield from _bounded_segments(work.segments[command], work)
        return
    segments = []
    for line in _split_logical_lines(command.replace("\\\n", "")):
        try:
            segments.extend(_split_segments(_shlex_tokens(line)))
        except ValueError:
            for physical in line.splitlines():
                try:
                    segments.extend(_split_segments(_shlex_tokens(physical)))
                except ValueError:
                    continue
    if work:
        work.segments[command] = segments
    yield from _bounded_segments(segments, work)


def _bounded_segments(segments, work):
    for index, segment in enumerate(segments):
        if work and index % 256 == 0:
            work.budget.checkpoint()
        yield segment
