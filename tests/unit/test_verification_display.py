"""Concise verification output from real structured check outcomes."""

from __future__ import annotations

import io
import os
from dataclasses import replace
from pathlib import Path
from threading import Event, Thread

import pytest

from vaultkeep.backup.workflow import CommandResult
from vaultkeep.cli import commands
from vaultkeep.cli import progress as progress_module
from vaultkeep.cli.progress import CliProgress, VerificationDisplay
from vaultkeep.errors import ArchiveVerificationError, DestinationError
from vaultkeep.progress import VerificationProgress


class Output(io.StringIO):
    def __init__(self, tty=False, encoding="utf-8"):
        super().__init__()
        self.tty = tty
        self._encoding = encoding
        self.frame = Event()
        self.heartbeat = Event()

    @property
    def encoding(self):
        return self._encoding

    def isatty(self):
        return self.tty

    def fileno(self):
        return 2

    def write(self, text):
        written = super().write(text)
        if "\033[2K" in text:
            self.frame.set()
        if "still running" in text:
            self.heartbeat.set()
        return written


@pytest.fixture
def workers(monkeypatch):
    threads = []

    def create(**kwargs):
        thread = Thread(**kwargs)
        threads.append(thread)
        return thread

    monkeypatch.setattr(progress_module, "Thread", create)
    monkeypatch.setattr(progress_module, "_REFRESH_SECONDS", 0.01)
    monkeypatch.setenv("TERM", "xterm")
    yield threads
    assert all(not thread.is_alive() for thread in threads)


def discovery(display, count=2):
    directories = tuple(Path(f"/backups/opaque-{n}") for n in range(1, count + 1))
    display.report(
        VerificationProgress("discovery", "Verification destination", path=Path("/backups"))
    )
    display.report(
        VerificationProgress(
            "discovery",
            "Backup discovery",
            "ok",
            Path("/backups"),
            total=count,
            candidates=directories,
        )
    )
    return directories


def check(
    display,
    directory,
    operation,
    status="ok",
    *,
    phase="backup",
    timestamp="2026-10-22T23:57:00+03:00",
    index=1,
    total=2,
    **kwargs,
):
    event = VerificationProgress(
        phase,
        operation,
        status,
        directory
        if operation == "Manifest/directory validation"
        else directory / "archive.tar.zst",
        index=index,
        total=total,
        backup_directory=directory,
        created_at=timestamp,
        backup_id=f"{index:032x}",
        **kwargs,
    )
    display.report(event)
    return event


def phase_complete(display, phase, completed=2, total=2):
    display.report(
        VerificationProgress(
            "discovery" if phase == 1 else "archive",
            "Phase complete",
            "complete",
            completed=completed,
            total=total,
        )
    )


def successful(display, directory, *, index=1, timestamp="2026-10-22T23:57:00+03:00"):
    for operation in ("Manifest/directory validation", "SHA-256"):
        check(display, directory, operation, index=index, timestamp=timestamp)


@pytest.mark.parametrize("tty", [False, True])
def test_phases_compact_updates_and_final_aligned_table(monkeypatch, workers, tty):
    output, final = Output(tty), Output()
    now = [0.0]
    monkeypatch.setattr(progress_module, "monotonic", lambda: now[0])
    with CliProgress(stream=output) as progress:
        display = VerificationDisplay(progress, stream=final)
        first, second = discovery(display)
        for index, directory in enumerate((first, second), 1):
            successful(
                display, directory, index=index, timestamp=f"2026-10-2{index}T23:57:00+03:00"
            )
        phase_complete(display, 1)
        check(display, first, "Zstandard stream test", "running", phase="archive")
        if tty:
            output.frame.clear()
            assert output.frame.wait(2)
            frame = output.getvalue().split("\r\033[2K")[-1]
            assert "[✓] 1/2" in frame and "2/2 passed" in frame
            assert "Phase 2/2" in frame and "Verifying" in frame
            assert "1/2" in frame and "[00:00]" in frame
        for index, directory in enumerate((first, second), 1):
            check(
                display,
                directory,
                "Archive verification",
                phase="archive",
                index=index,
                timestamp=f"2026-10-2{index}T23:57:00+03:00",
            )
        phase_complete(display, 2)
        now[0] = 262
        display.summary(success=True)
    text = final.getvalue()
    assert text == (
        "Vaultkeep — Verify\n\nDestination: /backups\n\n"
        "Backup            Manifest  SHA-256  Archive\n" + "─" * 44 + "\n"
        "2026-10-21 23:57  OK        OK       OK\n"
        "2026-10-22 23:57  OK        OK       OK\n" + "─" * 44 + "\n"
        "Verified:  2/2 backups\nDuration:  04:22\nResult:    VERIFIED\n"
    )
    assert "opaque" not in text + output.getvalue()
    assert "SUCCESS:" not in text + output.getvalue()
    if tty:
        assert output.getvalue().endswith("\r\033[2K")
        assert "Found 2 backups" not in output.getvalue()
    else:
        assert "\033" not in output.getvalue() and "\r" not in output.getvalue()
        assert output.getvalue().count("\n") == 5
        assert "Phase 1/2: Manifests & SHA-256" in output.getvalue()
        assert "Phase 2/2: Archive integrity" in output.getvalue()
    assert len(workers) == 1


@pytest.mark.parametrize("failure", ["manifest", "checksum", "archive"])
def test_failures_are_immediate_and_checks_are_independent(workers, failure):
    output, final = Output(), Output()
    with CliProgress(stream=output) as progress:
        display = VerificationDisplay(progress, stream=final)
        first, second = discovery(display)
        if failure == "manifest":
            check(
                display,
                first,
                "Manifest/directory validation",
                "failed",
                timestamp=None,
                error="invalid manifest",
            )
            expected = ["FAILED", "SKIPPED", "SKIPPED"]
        else:
            check(display, first, "Manifest/directory validation")
            check(
                display,
                first,
                "SHA-256",
                "failed" if failure == "checksum" else "ok",
                error="checksum mismatch" if failure == "checksum" else None,
            )
            expected = (
                ["OK", "FAILED", "SKIPPED"] if failure == "checksum" else ["OK", "OK", "FAILED"]
            )
        successful(display, second, index=2, timestamp="2026-10-23T00:50:00Z")
        phase_complete(display, 1, completed=1 if failure != "archive" else 2)
        if failure == "archive":
            check(display, first, "Zstandard stream test", phase="archive")
            check(
                display,
                first,
                "TAR inspection",
                "failed",
                phase="archive",
                error="unsafe member path",
            )
        assert "FAILED:" in output.getvalue()  # Before the final table exists.
        assert "/backups/opaque-1" in output.getvalue()
        check(
            display,
            second,
            "Archive verification",
            phase="archive",
            index=2,
            timestamp="2026-10-23T00:50:00Z",
        )
        phase_complete(display, 2, completed=1, total=2 if failure == "archive" else 1)
        display.summary(success=False)
    text = final.getvalue()
    row = next(
        line
        for line in text.splitlines()
        if line.startswith("Unknown #1" if failure == "manifest" else "2026-10-22")
    )
    assert row.split()[2:] == expected
    assert "Verified:  1/2 backups" in text and "Result:    FAILED" in text
    assert "Failure:" in text and "Path:   /backups/opaque-1" in text


def test_interruption_does_not_mark_unfinished_checks_as_successful(workers):
    output, final = Output(), Output()
    with CliProgress(stream=output) as progress:
        display = VerificationDisplay(progress, stream=final)
        first, _ = discovery(display)
        successful(display, first)
        phase_complete(display, 1, completed=1)
        check(display, first, "Zstandard stream test", phase="archive")
        check(display, first, "TAR inspection", "running", phase="archive")
        display.summary(success=False, error=KeyboardInterrupt())
    text = final.getvalue()
    assert "2026-10-22 23:57  OK           OK           NOT CHECKED" in text
    assert "Unknown #2        NOT CHECKED  NOT CHECKED  NOT CHECKED" in text
    assert "Verified:  0/2 backups" in text and "Error: KeyboardInterrupt" in text
    assert "FAILED" not in text.split("Result:")[0]


def test_duplicate_timestamps_have_short_distinct_labels(workers):
    output, final = Output(), Output()
    with CliProgress(stream=output) as progress:
        display = VerificationDisplay(progress, stream=final)
        directories = discovery(display)
        for index, directory in enumerate(directories, 1):
            successful(display, directory, index=index)
            check(display, directory, "Archive verification", phase="archive", index=index)
        display.summary(success=True)
    assert "2026-10-22 23:57 [00000001]" in final.getvalue()
    assert "2026-10-22 23:57 [00000002]" in final.getvalue()
    assert "Verified:  2/2" in final.getvalue()


@pytest.mark.parametrize("tty", [False, True])
def test_byte_updates_are_coalesced_and_elapsed_time_remains_visible(monkeypatch, workers, tty):
    monkeypatch.setattr(progress_module, "_HEARTBEAT_SECONDS", 0.01)
    output = Output(tty)
    now = [0.0]
    monkeypatch.setattr(progress_module, "monotonic", lambda: now[0])
    with CliProgress(stream=output) as progress:
        display = VerificationDisplay(progress, stream=Output())
        first, _ = discovery(display)
        event = check(
            display, first, "SHA-256", "running", bytes_processed=0, bytes_total=4 * 1024**3
        )
        before = output.getvalue().count("\n")
        for _ in range(500):
            display.report(replace(event, bytes_processed=1288490189))
        now[0] = 62
        if tty:
            output.frame.clear()
            assert output.frame.wait(2)
        else:
            assert output.heartbeat.wait(2)
            assert output.getvalue().count("\n") - before < 5
    text = output.getvalue()
    assert "1.2 GiB / 4.0 GiB (30%)" in text
    assert "[01:02]" in text


@pytest.mark.parametrize("columns", [1, 8, 25, 50, 80])
def test_panel_fits_width_and_clears_all_lines_on_resize(monkeypatch, workers, columns):
    width = [80]
    monkeypatch.setattr(
        progress_module.os, "get_terminal_size", lambda fd: os.terminal_size((width[0], 24))
    )
    output, final = Output(True), Output(True)
    with CliProgress(stream=output) as progress:
        display = VerificationDisplay(progress, stream=final)
        first, _ = discovery(display)
        successful(display, first)
        phase_complete(display, 1, completed=1)
        check(display, first, "Archive verification", "running", phase="archive")
        width[0] = columns
        output.seek(0)
        output.truncate()
        check(display, first, "Archive verification", phase="archive")
        frame = output.getvalue().split("\r\033[2K")[-1]
        assert all(CliProgress._cells(line) <= columns - 1 for line in frame.splitlines())
        assert "\033[1A" in output.getvalue()
        display.summary(success=False)
    if columns < 60:
        assert "  Manifest:" in final.getvalue()
    assert "NOT CHECKED" in final.getvalue()


def test_unknown_discovery_totals_and_unknown_byte_totals(workers):
    output, final = Output(), Output()
    with CliProgress(stream=output) as progress:
        display = VerificationDisplay(progress, stream=final)
        discovery(display)
        display.report(
            VerificationProgress(
                "discovery",
                "Backup discovery",
                "failed",
                Path("/backups/history"),
                error="cannot list history",
            )
        )
        display.summary(success=False, error=OSError("additional failure"))
    assert "total unknown" in final.getvalue()
    assert "additional failure" in final.getvalue()
    assert "cannot list history" in final.getvalue()
    assert "None" not in final.getvalue()


@pytest.mark.parametrize("verbose", [False, True])
def test_verbose_retains_paths_operations_and_checksum_diagnostics(workers, verbose):
    output, final = Output(), Output()
    with CliProgress(stream=output) as progress:
        display = VerificationDisplay(progress, stream=final, verbose=verbose)
        first, _ = discovery(display)
        successful(display, first)
        check(
            display,
            first,
            "SHA-256",
            "running",
            detail="Sidecar SHA-256: abc; calculated SHA-256: abc",
        )
        check(display, first, "Zstandard stream test", "running", phase="archive")
        display.summary(success=False)
    assert ("/backups/opaque-1/archive.tar.zst" in output.getvalue()) is verbose
    assert ("Sidecar SHA-256:" in output.getvalue()) is verbose
    assert ("Zstandard stream test" in output.getvalue()) is verbose


@pytest.mark.parametrize(
    "failure,code", [(None, 0), ("manifest", 5), ("checksum", 5), ("archive", 7)]
)
@pytest.mark.parametrize("tty", [False, True])
def test_cli_table_exit_codes_and_stdout_tty_gate(monkeypatch, workers, failure, code, tty):
    output, final = Output(True), Output(tty)
    monkeypatch.setattr(commands.sys, "stdout", final)
    monkeypatch.setattr(commands.sys, "stderr", output)

    def verify(config, **kwargs):
        assert config == Path("app.yaml")
        kwargs["progress"]("Checking destination")
        display = kwargs["on_progress"].__self__
        first = discovery(display, count=1)[0]
        if failure == "manifest":
            check(
                display, first, "Manifest/directory validation", "failed", error="invalid manifest"
            )
        else:
            check(display, first, "Manifest/directory validation")
            check(
                display,
                first,
                "SHA-256",
                "failed" if failure == "checksum" else "ok",
                error="checksum mismatch" if failure == "checksum" else None,
            )
        phase_complete(
            display, 1, completed=0 if failure in ("manifest", "checksum") else 1, total=1
        )
        if failure not in ("manifest", "checksum"):
            check(
                display,
                first,
                "Archive verification",
                "failed" if failure else "ok",
                phase="archive",
                error="corrupt archive" if failure else None,
            )
        phase_complete(display, 2, completed=0 if failure else 1, total=1)
        if failure:
            raise (ArchiveVerificationError if failure == "archive" else DestinationError)(
                "verification failure details"
            )
        return CommandResult("verify", "verified", backups=1)

    monkeypatch.setattr(commands, "verify_backups", verify)
    assert commands.main(["--config", "app.yaml", "verify"]) == code
    assert "Backup" in final.getvalue() and "Duration:" in final.getvalue()
    assert "verify: verified" not in final.getvalue()
    assert ("\033" in output.getvalue()) is tty
    assert "SUCCESS:" not in output.getvalue()
    assert final.getvalue().count("Result:") == 1
    assert "Result:    " + ("FAILED" if failure else "VERIFIED") in final.getvalue()


def test_panel_is_fully_replaced_by_final_table_on_shared_terminal(workers):
    import re

    output = Output(True)
    with CliProgress(stream=output) as progress:
        display = VerificationDisplay(progress, stream=output)
        first = discovery(display, count=1)[0]
        successful(display, first)
        phase_complete(display, 1, completed=1, total=1)
        check(display, first, "TAR inspection", "running", phase="archive", total=1)
        check(display, first, "Archive verification", phase="archive", total=1)
        phase_complete(display, 2, completed=1, total=1)
        display.summary(success=True)

    # Replay the renderer's cursor controls, rather than just checking for ANSI.
    screen = [[]]
    row = column = 0
    for token in re.split(r"(\033\[2K|\033\[1A|\r|\n)", output.getvalue()):
        if token == "\033[2K":
            screen[row] = []
        elif token == "\033[1A":
            row = max(0, row - 1)
        elif token == "\r":
            column = 0
        elif token == "\n":
            row += 1
            column = 0
            if row == len(screen):
                screen.append([])
        else:
            start_column = column
            for column, char in enumerate(token, start_column):
                while len(screen[row]) <= column:
                    screen[row].append(" ")
                screen[row][column] = char
            if token:
                column += 1
    visible = "\n".join("".join(line) for line in screen).rstrip()
    assert visible.count("Vaultkeep — Verify") == 1
    assert "Manifests & SHA-256" not in visible and "Phase" not in visible
    assert "1/1 passed" not in visible
    assert "2026-10-22 23:57  OK        OK       OK" in visible
    assert visible.endswith("Result:    VERIFIED")


def test_unknown_byte_total_ascii_output_and_control_characters(workers):
    output, final = Output(encoding="ascii"), Output(encoding="ascii")
    with CliProgress(stream=output) as progress:
        display = VerificationDisplay(progress, stream=final, verbose=True)
        path = discovery(display, count=1)[0]
        check(display, path, "SHA-256", "running", bytes_processed=500)
        check(display, path, "SHA-256", "failed", error="bad\n\033[2Jchecksum")
        display.summary(success=False)
    text = output.getvalue() + final.getvalue()
    text.encode("ascii")
    assert "\033" not in text and "\r" not in text
    assert "%" not in text and "/None" not in text
    assert "bad  [2Jchecksum" in text


@pytest.mark.parametrize("failure", [False, True])
def test_broken_summary_stream_preserves_verification_exit_code(monkeypatch, workers, failure):
    class BrokenOutput(Output):
        def write(self, text):
            raise BrokenPipeError("closed stdout")

    output = Output()
    monkeypatch.setattr(commands.sys, "stdout", BrokenOutput())
    monkeypatch.setattr(commands.sys, "stderr", output)

    def verify(*args, **kwargs):
        kwargs["progress"]("Checking destination")
        if failure:
            raise ArchiveVerificationError("original archive error")
        return CommandResult("verify", "verified")

    monkeypatch.setattr(commands, "verify_backups", verify)
    assert commands.main(["--config", "app.yaml", "verify"]) == (7 if failure else 0)
    if failure:
        assert "original archive error" in output.getvalue()
