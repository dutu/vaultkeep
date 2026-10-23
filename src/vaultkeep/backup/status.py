"""Read-only interpretation of backup-owned execution evidence."""

from __future__ import annotations

from typing import Any

import psutil  # type: ignore[import-untyped]

from vaultkeep.backup.paths import WorkflowPaths
from vaultkeep.backup.progress import STALE_SECONDS, read_progress
from vaultkeep.backup.records import read_history
from vaultkeep.diagnostics import diagnostic_tail, elapsed_seconds, parse_utc
from vaultkeep.errors import StateError
from vaultkeep.locking import job_lock_execution, job_lock_owner, job_lock_path


def process_alive(pid: int, create_time: float | None) -> bool | None:
    if create_time is None:
        return None
    try:
        process = psutil.Process(pid)
        return bool(
            abs(process.create_time() - create_time) < 0.001
            and process.status() != psutil.STATUS_ZOMBIE
        )
    except psutil.NoSuchProcess:
        return False
    except psutil.Error:
        return None


def backup_status(
    paths: WorkflowPaths, job_id: str, identity: str, *, verbose: bool = False
) -> dict[str, Any]:
    directory = paths.state_root / f"{job_id}-{identity}"
    lock = job_lock_path(root=paths.lock_root, job_id=job_id, identity_hash=identity)
    diagnostics: list[str] = []
    result: dict[str, Any] = {
        "activity": "UNKNOWN",
        "current": None,
        "previous": None,
        "diagnostics": diagnostics,
    }
    try:
        # Retry a changing snapshot once; a status reader never synchronizes writers.
        for _ in range(2):
            before = job_lock_execution(lock)
            history = read_history(directory / "execution.json", job_id, identity)
            owner = job_lock_owner(lock)
            after = job_lock_execution(lock)
            if before == after:
                break
        else:
            diagnostics.append("Execution changed during inspection; retry status")
            return result
        if history is None:
            diagnostics.append("No execution evidence recorded")
            return result
        previous = history.previous
        if previous:
            result["previous"] = previous.model_dump(exclude={"pid", "create_time"})
        current = history.current
        if current is None:
            # Completion evidence wins over stale progress. A different lock stamp
            # means another attempt could not publish evidence; never reuse its result.
            if owner == 0 and previous and after in ("", previous.execution_id):
                result["activity"] = "IDLE"
            else:
                diagnostics.append("Job ownership or current execution evidence is unavailable")
            return result
        if owner == 0:
            result["previous"] = current.model_copy(
                update={"result": "UNKNOWN", "reason": "Backup completion could not be verified"}
            ).model_dump(exclude={"pid", "create_time"})
            diagnostics.append("Execution ended without reliable completion evidence")
            return result
        if (
            after != current.execution_id
            or owner != current.pid
            or process_alive(current.pid, current.create_time) is not True
        ):
            diagnostics.append("Backup process identity or job ownership could not be verified")
            return result
        progress = read_progress(directory / "progress.json", current.execution_id)
        if progress is None:
            diagnostics.append("Backup progress unavailable")
            return result
        started = parse_utc(progress.started_at)
        phase_started = parse_utc(progress.phase_started_at)
        updated = parse_utc(progress.updated_at)
        age = elapsed_seconds(progress.updated_at)
        execution_started = parse_utc(current.started_at)
        if (
            started is None
            or phase_started is None
            or updated is None
            or execution_started is None
            or (started is not None and execution_started > started)
            or not started <= phase_started <= updated
            or age is None
            or age > STALE_SECONDS
            or progress.pid != current.pid
            or current.create_time != progress.create_time
        ):
            diagnostics.append("Backup progress stale or invalid")
            return result
        result["activity"] = "FINISHING" if progress.state == "finished" else "RUNNING"
        result["current"] = {
            "execution_id": current.execution_id,
            "phase": progress.phase,
            "phase_started_at": progress.phase_started_at,
            "started_at": current.started_at,
            "phase_elapsed_seconds": elapsed_seconds(progress.phase_started_at),
            "elapsed_seconds": elapsed_seconds(current.started_at),
        }
        return result
    except (OSError, StateError) as error:
        diagnostics.append(str(error))
        result["read_error"] = True
        return result
    finally:
        if result["previous"] is not None:
            # Only advertise a real fixed log path, never a persisted caller-supplied path.
            try:
                if (directory / "execution.log").is_file():
                    result["previous"]["log_path"] = str(directory / "execution.log")
                    if verbose and result["previous"]["result"] not in ("SUCCESS", "UNCHANGED"):
                        result["diagnostic_tail"] = diagnostic_tail(directory / "execution.log")
            except OSError:
                pass
