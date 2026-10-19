"""Interaction with the systemd user manager.

Only the monitor submits worker starts, via ``systemctl --user start --no-block`` with
an argv array and no shell. Submission success is not proof that a worker started.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Protocol

# systemctl start-command exit codes (systemd v257). This is NOT the service-status table.
EXIT_INVALIDARGUMENT = 2  # a Trigger argument-construction defect
EXIT_NOPERMISSION = 4  # policy denial — a pre-queue refusal
EXIT_NOTINSTALLED = 5  # absent unit or template — a pre-queue refusal
EXIT_NOTCONFIGURED = 6  # unit load failure — a pre-queue refusal

#: Start-command codes that prove the start was rejected before queueing.
REJECTION_CODES = frozenset({EXIT_NOPERMISSION, EXIT_NOTINSTALLED, EXIT_NOTCONFIGURED})

_SYSTEMCTL = "systemctl"
_DEFAULT_TIMEOUT = 15.0


class SubmissionClass(str, Enum):
    """Classification of a worker-start submission attempt."""

    ACCEPTED = "accepted"
    REJECTED = "rejected"
    INTEGRATION_ERROR = "integration_error"
    INCONCLUSIVE = "inconclusive"


@dataclass(frozen=True, slots=True)
class SubmissionResult:
    classification: SubmissionClass
    exit_code: int | None
    stderr: str
    spawned: bool


@dataclass(frozen=True, slots=True)
class UnitState:
    load_state: str | None
    active_state: str | None
    sub_state: str | None
    job_pending: bool = False

    def is_active_or_pending(self) -> bool:
        """Return whether the unit is running or has a start job in flight."""
        if self.job_pending or self.active_state not in ("inactive", "failed"):
            return True
        return self.sub_state in ("start", "start-pre", "start-post", "run", "stop")


class SystemdController(Protocol):
    def start_unit(self, unit: str) -> SubmissionResult:
        """Submit a non-blocking start for ``unit``."""

    def unit_state(self, unit: str) -> UnitState | None:
        """Return the unit's state, or ``None`` when the manager is unavailable."""

    def manager_reachable(self) -> bool:
        """Return whether the user manager is reachable."""

    def template_ready(self) -> bool:
        """Return whether the installed worker template is loadable and unmasked."""

    def reset_failed(self, unit: str) -> None:
        """Clear a failed unit state after its evidence has been reconciled."""


def classify_submission(*, spawned: bool, exit_code: int | None) -> SubmissionClass:
    """Classify a start submission without treating a numeric code as proof alone."""
    if not spawned:
        # Failure to spawn the local systemctl process proves the start was not accepted.
        return SubmissionClass.REJECTED
    if exit_code == 0:
        return SubmissionClass.ACCEPTED
    if exit_code == EXIT_INVALIDARGUMENT:
        return SubmissionClass.INTEGRATION_ERROR
    if exit_code in REJECTION_CODES:
        return SubmissionClass.REJECTED
    # Generic failure, timeout, or lost bus response: execution may still have queued.
    return SubmissionClass.INCONCLUSIVE


def _run(argv: list[str], *, timeout: float = _DEFAULT_TIMEOUT) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)


def _probe(argv: list[str]) -> subprocess.CompletedProcess | None:
    try:
        return _run(argv)
    except (OSError, subprocess.TimeoutExpired):
        return None


def _confirm_unit_absence(unit: str) -> UnitState | None:
    """Require successful unit and job listings; a failed lookup/code alone proves nothing."""
    units = _probe(
        [
            _SYSTEMCTL,
            "--user",
            "list-units",
            "--all",
            "--plain",
            "--no-legend",
            "--no-pager",
            "--full",
            unit,
        ]
    )
    if units is None or units.returncode != 0 or units.stdout.strip():
        return None
    jobs = _probe([_SYSTEMCTL, "--user", "list-jobs", "--no-legend", "--no-pager", "--full", unit])
    if jobs is None or jobs.returncode != 0:
        return None
    rows = [line.split() for line in jobs.stdout.splitlines() if line.strip()]
    if any(len(row) != 4 or not row[0].isdecimal() or row[1] != unit for row in rows):
        return None
    return UnitState("not-found", "inactive", "dead", job_pending=bool(rows))


class RealSystemdController:
    """Systemd controller using the local ``systemctl --user``."""

    @staticmethod
    def start_unit(unit: str) -> SubmissionResult:
        argv = [_SYSTEMCTL, "--user", "start", "--no-block", unit]
        try:
            completed = _run(argv)
        except subprocess.TimeoutExpired:
            return SubmissionResult(
                SubmissionClass.INCONCLUSIVE, None, "systemctl start timed out after spawn", True
            )
        except OSError:
            return SubmissionResult(
                classification=SubmissionClass.REJECTED,
                exit_code=None,
                stderr="failed to spawn systemctl",
                spawned=False,
            )
        classification = classify_submission(spawned=True, exit_code=completed.returncode)
        return SubmissionResult(
            classification=classification,
            exit_code=completed.returncode,
            stderr=(completed.stderr or "").strip()[:2048],
            spawned=True,
        )

    @staticmethod
    def unit_state(unit: str) -> UnitState | None:
        argv = [
            _SYSTEMCTL,
            "--user",
            "show",
            "--all",
            "--property=LoadState,ActiveState,SubState,Job",
            unit,
        ]
        completed = _probe(argv)
        if completed is None:
            return None
        values: dict[str, str] = {}
        for line in completed.stdout.splitlines():
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()
        if completed.returncode != 0 and values.get("LoadState") != "not-found":
            return _confirm_unit_absence(unit)
        # Missing/malformed job evidence cannot establish absence.
        job = values.get("Job")
        if (
            job is None
            or (job != "" and not job.isdecimal())
            or not values.get("ActiveState")
            or not values.get("SubState")
        ):
            return None
        return UnitState(
            load_state=values.get("LoadState"),
            active_state=values.get("ActiveState"),
            sub_state=values.get("SubState"),
            job_pending=job not in ("", "0"),
        )

    @staticmethod
    def manager_reachable() -> bool:
        completed = _probe([_SYSTEMCTL, "--user", "show-environment"])
        return completed is not None and completed.returncode == 0

    @staticmethod
    def reset_failed(unit: str) -> None:
        _probe([_SYSTEMCTL, "--user", "reset-failed", unit])

    @staticmethod
    def template_ready() -> bool:
        template = "vaultkeep-trigger-exec@.service"
        # A bare template has no runtime unit object. Inspect its installed files
        # without inventing/starting a probe instance.
        completed = _probe(
            [
                _SYSTEMCTL,
                "--user",
                "list-unit-files",
                "--no-legend",
                "--no-pager",
                "--full",
                template,
            ]
        )
        if completed is None or completed.returncode != 0:
            return False
        states = [
            line.split()[1]
            for line in completed.stdout.splitlines()
            if len(line.split()) >= 2 and line.split()[0] == template
        ]
        if len(states) != 1 or states[0] in ("masked", "masked-runtime", "bad"):
            return False
        content = _probe([_SYSTEMCTL, "--user", "cat", "--no-pager", template])
        if content is None or content.returncode != 0:
            return False
        section = ""
        settings: dict[str, str] = {}
        commands: list[str] = []
        for line in content.stdout.replace("\\\n", " ").splitlines():
            line = line.strip()
            if not line or line.startswith(("#", ";")):
                continue
            if line.startswith("["):
                section = line
            elif section == "[Service]" and "=" in line:
                key, value = line.split("=", 1)
                key, value = key.strip(), value.strip()
                settings[key] = value
                if key == "ExecStart":
                    if not value:
                        commands.clear()
                    else:
                        commands.append(value)
        if (
            settings.get("Type") != "oneshot"
            or len(commands) != 1
            or settings.get("Restart", "no") != "no"
            or settings.get("TimeoutStartSec") != "infinity"
        ):
            return False
        try:
            argv = shlex.split(commands[0])
        except ValueError:
            return False
        if len(argv) != 3 or argv[1:] != ["exec-worker", "%i"]:
            return False
        executable = Path(argv[0])
        return (
            executable.is_absolute()
            and executable.is_file()
            and not any(token in argv[0] for token in ("$", "%"))
            and os.access(executable, os.X_OK)
        )
