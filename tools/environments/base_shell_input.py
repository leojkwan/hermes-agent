"""Lexical EOF balance for instrumentation, never shell admission or evaluation."""


def _quoted_step(text, index, quote):
    ch = text[index]
    if ch == quote:
        return index + 1, ""
    if ch == "\\" and quote != "'" and index + 1 < len(text):
        return index + 2, quote
    return index + 1, quote


def incomplete_shell_input(text):
    """Recognize unclosed quotes/substitutions without executing or parsing a script.

    This only chooses whether Bash's eval syntax status needs normalization on
    older Bash. Complete input still runs sequentially with its original options.
    """
    stack = []
    quote = ""
    active = False
    index = 0
    while index < len(text):
        ch = text[index]
        if ch == "\\" and quote != "'":
            index += 2
            continue
        if quote == "'":
            index, quote = _quoted_step(text, index, quote)
            continue
        if ch == "#" and not quote and not active:
            end = text.find("\n", index)
            index = len(text) if end == -1 else end + 1
            continue
        opener = text[index : index + 2]
        if opener in {"$(", "${", "$[", "<(", ">("}:
            stack.append(
                ({"$(": ")", "${": "}", "$[": "]", "<(": ")"}.get(opener, ")"), quote)
            )
            quote = ""
            index += 2
            continue
        if not quote and stack and ch == stack[-1][0]:
            _, quote = stack.pop()
        elif not quote and stack and ch == "(" and stack[-1][0] == ")":
            stack.append((")", ""))
        elif ch in "'\"`" and (not quote or ch == quote):
            quote = "" if quote else ch
        active = not (ch.isspace() or ch in ";&|()")
        index += 1
    return bool(stack or quote)
