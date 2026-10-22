"""Levelled human-readable output with terminal-aware colors."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from typing import Literal, TextIO

Level = Literal["INFO", "WARNING", "ERROR"]
_COLORS = {"INFO": "36", "WARNING": "1;33", "ERROR": "1;31"}


@dataclass(frozen=True)
class Console:
    no_color: bool = False

    def print(self, message: object, *, level: Level = "INFO", file: TextIO | None = None) -> None:
        """Prefix every line, preserving the caller's choice of output stream."""
        stream = sys.stdout if file is None else file
        prefix = f"{level}:"
        if (
            not self.no_color
            and stream.isatty()
            and os.environ.get("TERM", "dumb") != "dumb"
            and not os.environ.get("NO_COLOR")
        ):
            prefix = f"\033[{_COLORS[level]}m{prefix}\033[0m"
        lines = str(message).splitlines() or [""]
        print("\n".join(f"{prefix} {line}" for line in lines), file=stream, flush=True)
