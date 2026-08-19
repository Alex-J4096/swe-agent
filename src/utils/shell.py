"""Conservative shell classification shared by permissions and team policy."""

from __future__ import annotations

import shlex


READONLY_PREFIXES = (
    ("pwd",),
    ("ls",),
    ("find",),
    ("cat",),
    ("rg",),
    ("grep",),
    ("head",),
    ("tail",),
    ("wc",),
    ("sort",),
    ("git", "status"),
    ("git", "diff"),
    ("git", "log"),
)
READONLY_CONNECTORS = frozenset({"|", "&&", "||"})
BLOCKED_FIND_FLAGS = frozenset({"-delete", "-exec", "-execdir", "-ok", "-okdir"})
SAFE_STDERR_REDIRECT = ("2", ">", "/dev/null")


def shell_tokens(command: str) -> list[str] | None:
    """Tokenize shell syntax while keeping quotes and operators distinguishable."""
    try:
        lexer = shlex.shlex(
            command,
            posix=True,
            punctuation_chars="|&;><",
        )
        lexer.whitespace_split = True
        lexer.commenters = ""
        return list(lexer)
    except (TypeError, ValueError):
        return None


def is_readonly_command(command: str) -> bool:
    """Return whether every command in a safe read-only composition is allowed."""
    if not isinstance(command, str) or not command.strip():
        return False
    if any(marker in command for marker in ("`", "$", "\n", "\r", "{", "}")):
        return False
    tokens = shell_tokens(command)
    if not tokens:
        return False

    segments: list[list[str]] = []
    current: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if tuple(tokens[index:index + 3]) == SAFE_STDERR_REDIRECT:
            index += 3
            continue
        if token in READONLY_CONNECTORS:
            if not current:
                return False
            segments.append(current)
            current = []
            index += 1
            continue
        if token in {";", "&", "|&", ">", ">>", "<", "<<", "<<<", "&>"}:
            return False
        current.append(token)
        index += 1
    if not current:
        return False
    segments.append(current)

    for segment in segments:
        if segment[0] == "find" and any(
            flag in segment for flag in BLOCKED_FIND_FLAGS
        ):
            return False
        if not any(
            tuple(segment[: len(prefix)]) == prefix
            for prefix in READONLY_PREFIXES
        ):
            return False
    return True
