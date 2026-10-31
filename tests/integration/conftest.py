"""Shared integration test fixtures."""

from __future__ import annotations

import signal
from collections.abc import Iterator
from types import FrameType

import pytest


@pytest.fixture
def archive_tool_deadline() -> Iterator[None]:
    """Keep small real-tool tests bounded, including waits after password delivery."""

    def timed_out(signum: int, frame: FrameType | None) -> None:
        raise TimeoutError("Archive integration test exceeded 60 seconds")

    previous_handler = signal.signal(signal.SIGALRM, timed_out)
    signal.alarm(60)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous_handler)
