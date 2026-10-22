"""Structured reports and terminal-aware diagnostic formatting."""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Literal, TextIO

Level = Literal["DEBUG", "INFO", "WARNING", "ERROR"]
_COLORS = {
    "OK": "32",
    "VALID": "32",
    "INSTALLED": "32",
    "WARNING": "1;33",
    "SETUP INCOMPLETE": "1;33",
    "NOT INSTALLED": "1;33",
    "FAILED": "1;31",
    "ERROR": "1;31",
    "RUNNING": "36",
    "STARTING": "36",
    "SUCCESS": "32",
    "UNCHANGED": "32",
    "UNKNOWN": "1;33",
    "BLOCKED": "1;33",
    "EXECUTION ERROR": "1;31",
}


@dataclass(frozen=True)
class Console:
    no_color: bool = False

    def status(self, value: str, *, file: TextIO | None = None) -> str:
        stream = sys.stdout if file is None else file
        color = _COLORS.get(value)
        if (
            color
            and not self.no_color
            and stream.isatty()
            and os.environ.get("TERM", "dumb") != "dumb"
            and not os.environ.get("NO_COLOR")
        ):
            return f"\033[{color}m{value}\033[0m"
        return value

    def print(
        self, message: object = "", *, level: Level | None = None, file: TextIO | None = None
    ) -> None:
        """Print reports plainly; explicit levels are reserved for diagnostics."""
        stream = sys.stdout if file is None else file
        lines = str(message).splitlines() or [""]
        if level is not None:
            prefix = self.status(level, file=stream)
            lines = [f"{prefix}: {line}" for line in lines]
        print("\n".join(lines), file=stream, flush=True)

    def heading(self, title: str) -> None:
        self.print(f"Vaultkeep — {title}")
        self.print()

    def fields(
        self,
        rows: Sequence[tuple[str, str]],
        *,
        section: str | None = None,
        file: TextIO | None = None,
    ) -> None:
        if not rows:
            return
        if section:
            self.print(f"\n{section}", file=file)
        width = max(len(label) for label, _ in rows)
        for label, value in rows:
            prefix = f"  {label:<{width}}  " if section else f"{label + ':':<{width + 3}}"
            self.print(prefix + self.status(value, file=file), file=file)

    def issue(self, message: object, *, warning: bool = False) -> None:
        stream = sys.stderr
        status = self.status("WARNING" if warning else "FAILED", file=stream)
        self.print(f"{status}: {message}", file=stream)


class _DiagnosticFormatter(logging.Formatter):
    def __init__(self, output: Console) -> None:
        super().__init__("%(asctime)s %(name)s: %(message)s")
        self.output = output

    def format(self, record: logging.LogRecord) -> str:
        prefix = self.output.status(record.levelname, file=sys.stderr)
        return f"{prefix}: {super().format(record)}"


def configure_logging(level: str, *, no_color: bool = False) -> None:
    """Configure only Vaultkeep's existing logging namespace, leaving the host alone."""
    logger = logging.getLogger("vaultkeep")
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(_DiagnosticFormatter(Console(no_color=no_color)))
    logger.handlers[:] = [handler]
    logger.setLevel(level.upper())
    logger.propagate = False


@contextmanager
def command_logging() -> Iterator[None]:
    """Keep CLI handlers scoped to the command when main is embedded or called again."""
    logger = logging.getLogger("vaultkeep")
    handlers, level, propagate = logger.handlers[:], logger.level, logger.propagate
    try:
        yield
    finally:
        for handler in logger.handlers:
            if handler not in handlers:
                handler.close()
        logger.handlers[:] = handlers
        logger.setLevel(level)
        logger.propagate = propagate
