"""Recognize literal notify invocations without interpreting shell code."""

from __future__ import annotations

import re
import shlex
from collections.abc import Iterator
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from vaultkeep.errors import ValidationIssue

if TYPE_CHECKING:
    from vaultkeep.config.models import JobConfig

_CONTROL_WORDS = frozenset({"if", "then", "elif", "do", "else", "!", "{"})


def invokes_notify(command: str) -> bool:
    """Recognize literal invocations; never expand or execute shell code."""
    return any(_is_notify(words) for words in _command_words(command))


def _command_words(command: str) -> Iterator[list[str]]:
    """Split simple commands while preserving quoted words and escaped operators.

    This is only lexical scanning. Here-documents stop detection; shell expansion,
    scripts, aliases, and indirect invocations remain runtime responsibilities.
    """
    words: list[str] = []
    word = ""
    quote = ""
    index = 0
    while index < len(command):
        character = command[index]
        if character == "\\" and quote != "'":
            pair = command[index : index + 2]
            if pair != "\\\n":
                word += pair
            index += len(pair)
            continue
        if quote:
            word += character
            if character == quote:
                quote = ""
        elif character in ("'", '"'):
            quote = character
            word += character
        elif character == "#" and not word:
            end = command.find("\n", index)
            index = len(command) if end < 0 else end
            continue
        elif character.isspace() or character in ";&|()" or command.startswith("<<", index):
            if word:
                words.append(word)
                word = ""
            if command.startswith("<<", index):
                yield words
                return
            if character in ";&|()\n":
                yield words
                words = []
        else:
            word += character
        index += 1
    if not quote:
        if word:
            words.append(word)
        yield words


def _literal(word: str) -> str:
    try:
        parts = shlex.split(word)
    except ValueError:
        return ""
    return parts[0] if len(parts) == 1 else ""


def _is_notify(words: list[str]) -> bool:
    index = 0
    while index < len(words) and (
        words[index] in _CONTROL_WORDS or re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", words[index])
    ):
        index += 1
    if index == len(words):
        return False
    executable = _literal(words[index])
    if executable != "vaultkeep" and not (
        executable.startswith("/") and PurePosixPath(executable).name == "vaultkeep"
    ):
        return False
    index += 1
    while index < len(words):
        word = _literal(words[index])
        if word == "notify":
            return True
        if word in ("--user", "--no-color"):
            index += 1
        elif word in ("--config", "--param"):
            index += 2
        elif word.startswith(("--config=", "--param=")):
            index += 1
        else:
            return False
    return False


def external_notifier_issues(config: JobConfig) -> tuple[ValidationIssue, ...]:
    if config.external_notifier is not None:
        return ()
    return tuple(
        ValidationIssue(
            ("hooks", phase, "command"),
            "Uses 'vaultkeep notify', but external_notifier is not configured.",
            "external_notifier_required",
        )
        for phase, hook in config.hooks
        if hook is not None and invokes_notify(hook.command)
    )
