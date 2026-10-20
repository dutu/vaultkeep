"""Durable, versioned state under the fixed Trigger state root."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from vaultkeep.trigger.errors import StateError
from vaultkeep.trigger.models import (
    InstanceState,
    LaunchRecord,
    Receipt,
    Request,
    utc_now,
)
from vaultkeep.trigger.version import (
    BLOCKED_INSTANCE_SCHEMA_VERSION,
    DIAGNOSTICS_CAP_BYTES,
    INSTANCE_STATE_SCHEMA_VERSION,
    LAUNCH_SCHEMA_VERSION,
    RECEIPT_SCHEMA_VERSION,
    REQUEST_SCHEMA_VERSION,
)

_DIR_MODE = 0o700
_FILE_MODE = 0o600


def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=_DIR_MODE)
    try:
        os.chmod(path, _DIR_MODE)
    except OSError:
        pass


def _atomic_write(path: Path, data: bytes, *, mode: int = _FILE_MODE) -> None:
    """Write bytes atomically with file and directory fsync."""
    _ensure_dir(path.parent)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_path, mode)
        os.replace(tmp_path, path)
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except BaseException:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise


def _read_json(path: Path) -> dict | None:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError) as error:
        raise StateError(f"cannot read state file {path}: {error}") from error
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as error:
        raise StateError(f"state file {path} is corrupt: {error}") from error
    if not isinstance(data, dict):
        raise StateError(f"state file {path} does not contain a JSON object")
    return data


def _check_schema(data: dict, expected: int, *, path: Path) -> None:
    found = data.get("schema_version")
    if found != expected:
        raise StateError(
            f"state file {path} has schema_version {found!r}; "
            f"expected {expected} (refusing to migrate or repair)"
        )


class StateStore:
    """Read and write Trigger state with atomic, permissioned persistence."""

    def __init__(self, root: Path) -> None:
        self.root = root

    # -- layout ---------------------------------------------------------------
    @property
    def instances_dir(self) -> Path:
        return self.root / "instances"

    @property
    def attempts_dir(self) -> Path:
        return self.root / "attempts"

    @property
    def blocked_dir(self) -> Path:
        return self.root / "blocked"

    def blocked_path(self, instance_id: str) -> Path:
        return self.blocked_dir / f"{instance_id}.json"

    def list_blocked_ids(self) -> list[str]:
        return sorted(path.stem for path in self.blocked_dir.glob("*.json"))

    def read_blocked(self, instance_id: str) -> str | None:
        path = self.blocked_path(instance_id)
        data = _read_json(path)
        if data is None:
            return None
        _check_schema(data, BLOCKED_INSTANCE_SCHEMA_VERSION, path=path)
        reason = data.get("reason")
        if data.get("instance_id") != instance_id or not isinstance(reason, str) or not reason:
            raise StateError(f"blocked-instance diagnostic {path} is malformed")
        return reason

    def write_blocked(self, instance_id: str, reason: str) -> None:
        """Preserve the first diagnostic without rewriting corrupt instance evidence."""
        path = self.blocked_path(instance_id)
        if path.exists():
            return
        data = {
            "schema_version": BLOCKED_INSTANCE_SCHEMA_VERSION,
            "instance_id": instance_id,
            "reason": reason,
            "blocked_at": utc_now(),
        }
        _atomic_write(path, json.dumps(data, sort_keys=True, indent=2).encode())

    def instance_path(self, instance_id: str) -> Path:
        return self.instances_dir / f"{instance_id}.json"

    def attempt_dir(self, attempt_id: str) -> Path:
        return self.attempts_dir / attempt_id

    def request_path(self, attempt_id: str) -> Path:
        return self.attempt_dir(attempt_id) / "request.json"

    def launch_path(self, attempt_id: str) -> Path:
        return self.attempt_dir(attempt_id) / "launch.json"

    def receipt_path(self, attempt_id: str) -> Path:
        return self.attempt_dir(attempt_id) / "receipt.json"

    def log_path(self, attempt_id: str) -> Path:
        return self.attempt_dir(attempt_id) / "log.txt"

    def ensure(self) -> None:
        _ensure_dir(self.root)
        _ensure_dir(self.instances_dir)
        _ensure_dir(self.attempts_dir)

    # -- instance state -------------------------------------------------------
    def list_instance_ids(self) -> list[str]:
        if not self.instances_dir.is_dir():
            return []
        return sorted(path.stem for path in self.instances_dir.glob("*.json"))

    def read_instance(self, instance_id: str) -> InstanceState | None:
        path = self.instance_path(instance_id)
        data = _read_json(path)
        if data is None:
            return None
        _check_schema(data, INSTANCE_STATE_SCHEMA_VERSION, path=path)
        if data.get("instance_id") != instance_id:
            raise StateError(
                f"state file {path} records instance_id {data.get('instance_id')!r}; "
                f"expected {instance_id!r}"
            )
        try:
            return InstanceState.from_dict(data)
        except (KeyError, ValueError, TypeError) as error:
            raise StateError(f"state file {path} is malformed: {error}") from error

    def write_instance(self, state: InstanceState) -> None:
        path = self.instance_path(state.instance_id)
        payload = json.dumps(state.to_dict(), sort_keys=True, indent=2).encode()
        _atomic_write(path, payload)

    # -- attempts -------------------------------------------------------------
    def list_attempt_ids(self) -> list[str]:
        if not self.attempts_dir.is_dir():
            return []
        return sorted(path.name for path in self.attempts_dir.iterdir() if path.is_dir())

    def write_request(self, request: Request) -> None:
        path = self.request_path(request.attempt_id)
        if path.exists():
            raise StateError(f"refusing to overwrite immutable request {path}")
        _atomic_write(path, json.dumps(request.to_dict(), sort_keys=True, indent=2).encode())

    def read_request(self, attempt_id: str) -> Request | None:
        path = self.request_path(attempt_id)
        data = _read_json(path)
        if data is None:
            return None
        _check_schema(data, REQUEST_SCHEMA_VERSION, path=path)
        try:
            return Request.from_dict(data)
        except (KeyError, ValueError, TypeError) as error:
            raise StateError(f"request {path} is malformed: {error}") from error

    def write_launch(self, launch: LaunchRecord) -> None:
        path = self.launch_path(launch.attempt_id)
        _atomic_write(path, json.dumps(launch.to_dict(), sort_keys=True, indent=2).encode())

    def read_launch(self, attempt_id: str) -> LaunchRecord | None:
        path = self.launch_path(attempt_id)
        data = _read_json(path)
        if data is None:
            return None
        _check_schema(data, LAUNCH_SCHEMA_VERSION, path=path)
        try:
            return LaunchRecord.from_dict(data)
        except (KeyError, ValueError, TypeError) as error:
            raise StateError(f"launch record {path} is malformed: {error}") from error

    def write_receipt(self, receipt: Receipt) -> None:
        path = self.receipt_path(receipt.attempt_id)
        if path.exists():
            # A receipt is immutable once written.
            return
        _atomic_write(path, json.dumps(receipt.to_dict(), sort_keys=True, indent=2).encode())

    def read_receipt(self, attempt_id: str) -> Receipt | None:
        path = self.receipt_path(attempt_id)
        data = _read_json(path)
        if data is None:
            return None
        _check_schema(data, RECEIPT_SCHEMA_VERSION, path=path)
        try:
            return Receipt.from_dict(data)
        except (KeyError, ValueError, TypeError) as error:
            raise StateError(f"receipt {path} is malformed: {error}") from error

    def write_diagnostics(self, attempt_id: str, data: bytes) -> tuple[str, bool]:
        """Store capped raw diagnostics; returns (reference, truncated)."""
        truncated = len(data) > DIAGNOSTICS_CAP_BYTES
        stored = data[-DIAGNOSTICS_CAP_BYTES:] if truncated else data
        path = self.log_path(attempt_id)
        _atomic_write(path, stored)
        return str(path), truncated

    def remove_attempt(self, attempt_id: str) -> None:
        """Remove an attempt directory that is eligible for cleanup."""
        directory = self.attempt_dir(attempt_id)
        if not directory.is_dir():
            return
        for child in sorted(directory.rglob("*"), reverse=True):
            if child.is_file():
                child.unlink()
            elif child.is_dir():
                child.rmdir()
        directory.rmdir()
