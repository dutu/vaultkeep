from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from vaultkeep_trigger.models import AttemptOutcome
from vaultkeep_trigger.results import classify_exit, parse_marker_line
from vaultkeep_trigger.systemd import (
    RealSystemdController,
    SubmissionClass,
    UnitState,
    classify_submission,
)


def test_success_created() -> None:
    assert classify_exit(0, "run: created") is AttemptOutcome.CREATED


def test_success_unchanged() -> None:
    assert classify_exit(0, "run: unchanged") is AttemptOutcome.UNCHANGED


def test_success_unknown_marker() -> None:
    assert classify_exit(0, "some noise") is AttemptOutcome.RESULT_UNKNOWN
    assert classify_exit(0, None) is AttemptOutcome.RESULT_UNKNOWN


def test_stderr_marker_ignored() -> None:
    # stdout_tail is stdout only; an unrecognized tail yields result_unknown.
    assert parse_marker_line("run: created") == "run: created"
    assert parse_marker_line("junk") is None


def test_lock_is_deferred() -> None:
    assert classify_exit(10, None) is AttemptOutcome.DEFERRED


def test_invalid_argument_is_integration() -> None:
    assert classify_exit(2, None) is AttemptOutcome.INTEGRATION_ERROR


def test_other_failures() -> None:
    for code in (1, 3, 5, 7, 11, 14, 15, 99):
        assert classify_exit(code, None) is AttemptOutcome.BACKUP_FAILED


def test_submission_accepted() -> None:
    assert classify_submission(spawned=True, exit_code=0) is SubmissionClass.ACCEPTED


def test_submission_rejections() -> None:
    for code in (4, 5, 6):
        assert classify_submission(spawned=True, exit_code=code) is SubmissionClass.REJECTED


def test_invalid_argument_is_integration_error() -> None:
    result = classify_submission(spawned=True, exit_code=2)
    assert result is SubmissionClass.INTEGRATION_ERROR


def test_generic_failure_is_inconclusive() -> None:
    for code in (1, 3, 7, 255):
        assert classify_submission(spawned=True, exit_code=code) is SubmissionClass.INCONCLUSIVE


def test_spawn_failure_is_rejection() -> None:
    assert classify_submission(spawned=False, exit_code=None) is SubmissionClass.REJECTED


def test_unit_state_pending() -> None:
    assert UnitState("loaded", "activating", "start").is_active_or_pending()
    assert UnitState("loaded", "active", "running").is_active_or_pending()
    assert not UnitState("loaded", "inactive", "dead").is_active_or_pending()


def test_real_start_timeout_is_inconclusive(monkeypatch) -> None:
    def timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired("systemctl", 15)

    monkeypatch.setattr("vaultkeep_trigger.systemd.subprocess.run", timeout)
    result = RealSystemdController().start_unit("fake.service")
    assert result.classification is SubmissionClass.INCONCLUSIVE and result.spawned


def test_real_spawn_failure_is_proven_rejection(monkeypatch) -> None:
    def unavailable(*_args, **_kwargs):
        raise FileNotFoundError("systemctl unavailable")

    monkeypatch.setattr("vaultkeep_trigger.systemd.subprocess.run", unavailable)
    result = RealSystemdController().start_unit("fake.service")
    assert result.classification is SubmissionClass.REJECTED and not result.spawned


@pytest.mark.parametrize("job,pending", [("", False), ("0", False), ("123", True)])
def test_real_probe_includes_queued_job(monkeypatch, job: str, pending: bool) -> None:
    def show(argv, **_kwargs):
        assert "--property=LoadState,ActiveState,SubState,Job" in argv
        assert "--all" in argv
        return subprocess.CompletedProcess(
            argv, 0, f"LoadState=loaded\nActiveState=inactive\nSubState=dead\nJob={job}\n", ""
        )

    monkeypatch.setattr("vaultkeep_trigger.systemd.subprocess.run", show)
    state = RealSystemdController().unit_state("fake.service")
    assert state is not None and state.is_active_or_pending() is pending


def test_missing_job_evidence_is_inconclusive(monkeypatch) -> None:
    monkeypatch.setattr(
        "vaultkeep_trigger.systemd.subprocess.run",
        lambda argv, **_kwargs: subprocess.CompletedProcess(
            argv, 0, "ActiveState=inactive\nSubState=dead\n", ""
        ),
    )
    assert RealSystemdController().unit_state("fake.service") is None


@pytest.mark.parametrize("returncode", [0, 4, 5])
def test_explicit_not_found_properties_confirm_absence(monkeypatch, returncode: int) -> None:
    monkeypatch.setattr(
        "vaultkeep_trigger.systemd.subprocess.run",
        lambda argv, **_kwargs: subprocess.CompletedProcess(
            argv, returncode, "LoadState=not-found\nActiveState=inactive\nSubState=dead\nJob=\n", ""
        ),
    )
    state = RealSystemdController().unit_state("fake.service")
    assert (
        state is not None and state.load_state == "not-found" and not state.is_active_or_pending()
    )


@pytest.mark.parametrize(
    "unit_rows,job_rows,listing_code,expected",
    [
        ("", "", 0, "absent"),
        ("", "123 fake.service start waiting\n", 0, "pending"),
        ("fake.service loaded inactive dead\n", "", 0, "unknown"),
        ("", "malformed response\n", 0, "unknown"),
        ("", "", 1, "unknown"),
    ],
)
def test_failed_lookup_needs_positive_unit_and_job_absence(
    monkeypatch,
    unit_rows: str,
    job_rows: str,
    listing_code: int,
    expected: str,
) -> None:
    def query(argv, **_kwargs):
        if "show" in argv:
            return subprocess.CompletedProcess(argv, 1, "", "lookup failed")
        if "list-units" in argv:
            return subprocess.CompletedProcess(argv, listing_code, unit_rows, "")
        assert "list-jobs" in argv
        return subprocess.CompletedProcess(argv, 0, job_rows, "")

    monkeypatch.setattr("vaultkeep_trigger.systemd.subprocess.run", query)
    state = RealSystemdController().unit_state("fake.service")
    if expected == "unknown":
        assert state is None
    else:
        assert state is not None and state.is_active_or_pending() is (expected == "pending")


@pytest.mark.parametrize(
    "unit_state,executable_exists,ready",
    [
        ("static", True, True),
        ("masked", True, False),
        ("bad", True, False),
        ("static", False, False),
    ],
)
def test_template_preflight_inspects_files_and_binary_without_probe_unit(
    tmp_path: Path,
    monkeypatch,
    unit_state: str,
    executable_exists: bool,
    ready: bool,
) -> None:
    binary = tmp_path / "vaultkeep-trigger"
    if executable_exists:
        binary.write_text("#!/bin/sh\nexit 0\n")
        binary.chmod(0o700)

    def query(argv, **_kwargs):
        assert "start" not in argv and "show" not in argv
        if "list-unit-files" in argv:
            stdout = f"vaultkeep-trigger-exec@.service {unit_state} -\n"
        else:
            assert "cat" in argv
            stdout = (
                "[Service]\nType=oneshot\nRestart=no\nTimeoutStartSec=infinity\n"
                f"ExecStart={binary} exec-worker %i\n"
            )
        return subprocess.CompletedProcess(argv, 0, stdout, "")

    monkeypatch.setattr("vaultkeep_trigger.systemd.subprocess.run", query)
    assert RealSystemdController().template_ready() is ready
