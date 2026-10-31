"""Small shared formatting helpers for operational diagnostics."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def parse_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.astimezone(UTC) if parsed.tzinfo is not None else None
    except (ValueError, OverflowError):
        return None


def elapsed_seconds(timestamp: str | None) -> float | None:
    parsed = parse_utc(timestamp)
    if parsed is None:
        return None
    now = parse_utc(utc_now())
    if now is None:
        return None
    seconds = (now - parsed).total_seconds()
    return seconds if seconds >= 0 else None


def duration(seconds: float) -> str:
    minutes, seconds = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"


def timestamp(value: str | None) -> str:
    parsed = parse_utc(value)
    return parsed.astimezone().strftime("%Y-%m-%d %H:%M %Z") if parsed else "unknown"


def concise(reason: str) -> str:
    text = "".join(c if c.isprintable() else " " for c in reason).strip()
    return text if len(text) <= 160 else text[:157] + "..."


def diagnostic_tail(path: Path) -> list[str]:
    """Bound and sanitize optional private log output for a verbose status report."""
    try:
        with path.open("rb") as stream:
            stream.seek(0, 2)
            stream.seek(max(0, stream.tell() - 65536))
            data = stream.read(65536)
    except OSError as error:
        return [f"Diagnostics unavailable: {concise(str(error))}"]
    return [
        "".join(c if c.isprintable() else " " for c in line)[:500]
        for line in data.decode("utf-8", "replace").splitlines()
        if line.strip()
    ][-10:]
