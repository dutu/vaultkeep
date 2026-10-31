"""The internal worker: perform a final recheck and invoke one VaultKeep job."""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass

from vaultkeep.application import (
    Observation,
    ProcessProvider,
    PsutilProcessProvider,
    detect_spec,
    matcher_from_canonical,
)
from vaultkeep.backup.results import classify_command
from vaultkeep.trigger.childproc import ChildRunner, RealChildRunner
from vaultkeep.trigger.errors import (
    EXIT_BACKUP_FAILED,
    EXIT_BLOCKED,
    EXIT_INTEGRATION,
    EXIT_OK,
    IntegrationError,
)
from vaultkeep.trigger.models import AttemptOutcome, Receipt, Request, utc_now
from vaultkeep.trigger.state import StateStore
from vaultkeep.trigger.version import DEFAULT_PREFLIGHT_TIMEOUT_SECONDS
from vaultkeep.version import version_report


@dataclass(frozen=True, slots=True)
class WorkerResult:
    exit_code: int
    outcome: AttemptOutcome
    message: str


def _validate_request(request: Request, attempt_id: str) -> None:
    if request.attempt_id != attempt_id:
        raise IntegrationError(
            f"request {request.request_id} is bound to attempt {request.attempt_id!r}, "
            f"not {attempt_id!r}"
        )
    try:
        parsed = uuid.UUID(request.request_id)
    except ValueError as error:
        raise IntegrationError(f"request_id {request.request_id!r} is not a UUID") from error
    if parsed.version != 4:
        raise IntegrationError("request_id is not a UUIDv4")
    if not request.job_config:
        raise IntegrationError("request has no VaultKeep job configuration")
    if not request.vaultkeep:
        raise IntegrationError("request has no VaultKeep executable")
    policy = request.execution_policy
    app = policy.get("application")
    if (
        set(policy) != {"consistency", "application", "owner_uid"}
        or policy["consistency"] not in ("files", "application_offline")
        or not isinstance(app, dict)
        or app.get("name") != request.app
        or app.get("data_dir") != request.data_dir
        or app.get("matcher") != request.matcher
        or type(request.owner_uid) is not int
        or request.owner_uid < 0
        or policy["owner_uid"] != request.owner_uid
    ):
        raise IntegrationError("Frozen application identity and execution policy disagree")


def _vaultkeep_argv(request: Request, command: str) -> list[str]:
    argv = [request.vaultkeep, "--user", "--config", request.job_config]
    for key in sorted(request.job_params):
        argv.extend(["--param", f"{key}={request.job_params[key]}"])
    argv.append(command)
    return argv


def _write_receipt(store: StateStore, receipt: Receipt) -> None:
    store.write_receipt(receipt)


def exec_worker(
    attempt_id: str,
    *,
    store: StateStore,
    provider: ProcessProvider | None = None,
    runner: ChildRunner | None = None,
) -> WorkerResult:
    """Run one worker attempt and write its durable receipt."""
    runner = runner or RealChildRunner()

    request = store.read_request(attempt_id)
    if request is None:
        raise IntegrationError(f"no request found for attempt {attempt_id}")
    _validate_request(request, attempt_id)
    provider = provider or PsutilProcessProvider(owner_uid=request.owner_uid)

    existing = store.read_receipt(attempt_id)
    if existing is not None:
        return WorkerResult(
            exit_code=EXIT_OK,
            outcome=existing.outcome,
            message=f"attempt {attempt_id} already has a receipt; refusing to replay",
        )

    started_at = utc_now()

    # Authoritative final process/identity recheck from the frozen request snapshot.
    detection = detect_spec(
        app=request.app,
        data_dir=request.data_dir,
        matcher=matcher_from_canonical(request.matcher),
        provider=provider,
    )
    if detection.observation is not Observation.STOPPED:
        receipt = Receipt(
            attempt_id=attempt_id,
            request_id=request.request_id,
            instance_id=request.instance_id,
            outcome=AttemptOutcome.BLOCKED,
            started_at=started_at,
            ended_at=utc_now(),
            duration_seconds=0.0,
            blocked_reason=(
                f"final recheck returned {detection.observation.value}"
                + (f": {detection.reason}" if detection.reason else "")
            ),
        )
        _write_receipt(store, receipt)
        return WorkerResult(
            exit_code=EXIT_BLOCKED, outcome=receipt.outcome, message=receipt.blocked_reason or ""
        )

    # Capture the actual VaultKeep version immediately before invocation.
    version_result = runner.run(
        [request.vaultkeep, "--version"],
        env=request.environment,
        timeout=DEFAULT_PREFLIGHT_TIMEOUT_SECONDS,
    )
    actual_version = (
        (version_result.stdout_tail or "").strip() if version_result.exit_code == 0 else None
    )
    if actual_version != (request.preflight_version or version_report()):
        logging.getLogger(__name__).warning(
            "VaultKeep execution version differs from preflight/contract"
        )

    environment = dict(request.environment)
    environment["VAULTKEEP_EXPECTED_POLICY"] = json.dumps(request.execution_policy, sort_keys=True)
    child = runner.run(_vaultkeep_argv(request, "run"), env=environment)

    outcome = {
        "SUCCESS": AttemptOutcome.CREATED,
        "UNCHANGED": AttemptOutcome.UNCHANGED,
        "UNKNOWN": AttemptOutcome.RESULT_UNKNOWN,
        "DEFERRED": AttemptOutcome.DEFERRED,
        "EXECUTION ERROR": AttemptOutcome.INTEGRATION_ERROR,
        "FAILED": AttemptOutcome.BACKUP_FAILED,
    }[classify_command(child.exit_code, child.stdout_tail)]
    diagnostics_ref: str | None = None
    diagnostics_truncated = child.diagnostics_truncated
    if child.diagnostics:
        diagnostics_ref, stored_truncated = store.write_diagnostics(attempt_id, child.diagnostics)
        diagnostics_truncated = diagnostics_truncated or stored_truncated

    integration_error: str | None = None
    if outcome is AttemptOutcome.INTEGRATION_ERROR:
        integration_error = "VaultKeep rejected the constructed arguments (exit 2)"
    if child.spawn_error:
        outcome = AttemptOutcome.INTEGRATION_ERROR
        integration_error = f"failed to execute VaultKeep: {child.spawn_error}"

    receipt = Receipt(
        attempt_id=attempt_id,
        request_id=request.request_id,
        instance_id=request.instance_id,
        outcome=outcome,
        started_at=child.started_at if not child.spawn_error else started_at,
        ended_at=child.ended_at,
        duration_seconds=child.duration_seconds,
        exit_code=child.exit_code,
        signal=child.signal_number,
        vaultkeep_version=actual_version,
        result=child.stdout_tail,
        marker_line=child.stdout_tail,
        diagnostics_ref=diagnostics_ref,
        diagnostics_truncated=diagnostics_truncated,
        integration_error=integration_error,
    )
    _write_receipt(store, receipt)

    exit_code = EXIT_OK
    if outcome is AttemptOutcome.INTEGRATION_ERROR:
        exit_code = EXIT_INTEGRATION
    elif outcome in (AttemptOutcome.BACKUP_FAILED, AttemptOutcome.DEFERRED):
        exit_code = EXIT_BACKUP_FAILED
    return WorkerResult(exit_code=exit_code, outcome=outcome, message=outcome.value)
