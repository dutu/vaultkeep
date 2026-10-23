"""Live phases are bounded, private telemetry, never completion evidence."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from vaultkeep.backup.progress import ProgressReporter, read_progress


def test_phase_changes_heartbeat_and_finish(tmp_path, monkeypatch):
    path = tmp_path / "progress.json"
    times = iter(
        [
            "2026-10-22T10:00:00Z",
            "2026-10-22T10:00:10Z",
            "2026-10-22T10:00:15Z",
            "2026-10-22T10:00:20Z",
            "2026-10-22T10:00:30Z",
        ]
    )
    monkeypatch.setattr("vaultkeep.backup.progress._now", times.__next__)
    reporter = ProgressReporter(path, execution_id=tmp_path.name)
    reporter.report("Hashing source files")
    reporter.heartbeat()
    reporter.report("Hashing source files")
    record = read_progress(path, tmp_path.name)
    assert record is not None
    assert record.started_at == record.phase_started_at == "2026-10-22T10:00:00Z"
    assert record.updated_at == "2026-10-22T10:00:15Z"
    reporter.report("Creating and compressing archive")
    reporter.finish()
    record = read_progress(path, tmp_path.name)
    assert record is not None and record.state == "finished"
    assert record.phase == "Creating and compressing archive"
    assert record.started_at == "2026-10-22T10:00:00Z"
    assert record.phase_started_at == "2026-10-22T10:00:20Z"
    assert record.updated_at == "2026-10-22T10:00:30Z"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert list(tmp_path.iterdir()) == [path]


def test_progress_failure_is_best_effort_and_leaves_no_partial_record(
    tmp_path, monkeypatch, caplog
):
    def fail(*_args):
        raise OSError("reporting storage unavailable")

    monkeypatch.setattr("vaultkeep.backup.progress.os.replace", fail)
    reporter = ProgressReporter(tmp_path / "progress.json", execution_id=tmp_path.name)
    reporter.report("Hashing source files")
    reporter.report("Verifying archive")
    reporter.heartbeat()
    reporter.finish()
    assert list(tmp_path.iterdir()) == []
    assert caplog.text.count("Live backup progress unavailable") == 1


def test_progress_phase_is_printable_and_bounded(tmp_path):
    path = tmp_path / "progress.json"
    ProgressReporter(path, execution_id=tmp_path.name).report("phase\n\x1b" + "x" * 500)
    record = read_progress(path, tmp_path.name)
    assert record is not None and record.phase.isprintable()
    assert len(record.phase) == 160


@pytest.mark.parametrize(
    "change",
    [
        {"schema_version": 2},
        {"schema_version": True},
        {"execution_id": "another-attempt"},
        {"state": "success"},
        {"phase": "\x1b[31mspoofed phase"},
        {"phase": "x" * 161},
        {"pid": True},
        {"pid": -1},
        {"create_time": float("nan")},
        {"started_at": None},
    ],
)
def test_invalid_progress_is_ignored(tmp_path, change):
    path = tmp_path / "progress.json"
    ProgressReporter(path, execution_id=tmp_path.name).report("Hashing source files")
    data = json.loads(path.read_text())
    data.update(change)
    path.write_text(json.dumps(data))
    assert read_progress(path, tmp_path.name) is None


@pytest.mark.parametrize("contents", ["{", "[]", "{}", "x" * 4097])
def test_damaged_progress_is_ignored(tmp_path, contents):
    path = tmp_path / "progress.json"
    path.write_text(contents)
    assert read_progress(path, tmp_path.name) is None


def test_missing_and_unreadable_progress_is_ignored(tmp_path, monkeypatch):
    path = tmp_path / "progress.json"
    assert read_progress(path, tmp_path.name) is None

    def deny(*_args):
        raise PermissionError("cannot read telemetry")

    monkeypatch.setattr(Path, "open", deny)
    assert read_progress(path, tmp_path.name) is None
