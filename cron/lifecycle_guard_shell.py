"""Bounded shell syntax and execution-fact mutation classification, never evaluation."""

import re

from tools.environments.local_execution_context import (
    FIELD_NAMES,
    NPM_PREFIX,
    SHELL_HOOK_NAMES,
)

_PREFIXES = frozenset(
    {"if", "then", "elif", "else", "do", "while", "until", "!", "{", "time"}
)
_SETTERS = frozenset({"export", "unset", "declare", "readonly", "typeset", "local"})
_UNSUPPORTED = frozenset({"case", "coproc", "[[", "((", "trap"})
_READ_VALUES = frozenset({"-d", "-i", "-n", "-N", "-p", "-t", "-u"})
_MAP_VALUES = frozenset({"-n", "-O", "-s", "-u", "-C", "-c", "-d"})


def peel_shell_prefixes(segment):
    index = 0
    while (
        index < len(segment)
        and segment[index] in _PREFIXES
        and not getattr(segment[index], "quoted", False)
    ):
        keyword = segment[index]
        index += 1
        if keyword == "time" and index < len(segment) and segment[index] == "-p":
            index += 1
    result = segment[index:]
    if (
        result
        and result[0] in {"}", "fi", "done", "esac"}
        and not getattr(result[0], "quoted", False)
    ):
        return result[1:]
    return result


def fact_name(name):
    if "[" in name:
        return True
    name = name.partition("=")[0].rstrip("+")
    return (
        name in FIELD_NAMES
        or name in SHELL_HOOK_NAMES
        or name in {"SHELLOPTS", "BASHOPTS"}
        or name.casefold() == "path"
        or bool(NPM_PREFIX.match(name))
        or bool(re.search(r"[$`*?{}\x00]", name))
    )


def _read_targets(args, value_options):
    index = 0
    while index < len(args):
        token = args[index]
        if token == "--":
            return args[index + 1 :]
        if not token.startswith("-") or token == "-":
            return args[index:]
        if token == "-a":
            return args[index + 1 : index + 2]
        if len(token) > 2 and token[:2] in value_options | {"-a"}:
            if token.startswith("-a"):
                return [token[2:]]
            index += 1
        else:
            index += 2 if token in value_options else 1
    return []


def _printf_targets(args):
    if args and args[0] == "-v":
        return args[1:2]
    if args and args[0].startswith("-v"):
        return [args[0][2:]]
    return []


def _wait_targets(args):
    for index, token in enumerate(args):
        if token == "--" or not token.startswith("-"):
            break
        if "p" in token[1:]:
            target = token.partition("p")[2]
            return [target] if target else args[index + 1 : index + 2]
    return []


def assignment_mutation(segment, argv):
    if any(
        fact_name(name)
        for token in segment
        for name in getattr(token, "assignments", ())
    ):
        return True
    if not argv:
        return any(fact_name(token) for token in segment)
    name, args = argv[0], argv[1:]
    if name in _UNSUPPORTED:
        return True
    if name in _SETTERS:
        if name in {"declare", "typeset", "local"} and any(
            token.startswith("-") and any(flag in token[1:] for flag in "ni") for token in args
        ):
            return True
        targets = [token for token in args if not token.startswith("-")]
    elif name in {"read", "mapfile", "readarray"}:
        targets = _read_targets(args, _READ_VALUES if name == "read" else _MAP_VALUES)
    elif name == "printf":
        targets = _printf_targets(args)
    elif name == "wait":
        targets = _wait_targets(args)
    elif name in {"for", "select"}:
        targets = args[:1]
    elif name == "getopts":
        targets = args[1:2]
    elif name == "let":
        targets = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", " ".join(args))
    else:
        return False
    return any(fact_name(target) for target in targets)


_OPTION_CLASSES = frozenset({"keyword", "allexport"})


def option_mutation(command, args):
    """Modes that change assignment/export placement invalidate dependent facts."""
    if command not in {"set", "bash", "sh", "shopt"}:
        return False
    index = 0
    while index < len(args):
        token = args[index]
        if token == "--" or not token.startswith(("-", "+")):
            break
        if token.startswith("-") and not token.startswith("--"):
            if command != "shopt" and any(flag in token[1:] for flag in "ak"):
                return True
            if "o" in token[1:] and index + 1 < len(args) and args[index + 1] in _OPTION_CLASSES:
                return True
        if "c" in token[1:] and command in {"bash", "sh"}:
            break
        index += 2 if token in {"-o", "+o", "-O", "+O"} else 1

    return False


def arithmetic_mutation(tokens):
    return any(fact_name(name) for token in tokens for name in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", token))
