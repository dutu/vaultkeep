"""Every initiator observes one lock-owning execution and one result history."""

from __future__ import annotations

import json
import os
import signal
from datetime import datetime, timedelta
from pathlib import Path
from threading import Event

import pytest

from vaultkeep.archive import builder
from vaultkeep.archive.base import ArchiveArtifact
from vaultkeep.archive.checksums import calculate_archive_sha256, write_checksum_sidecar
from vaultkeep.backup import workflow
from vaultkeep.backup.execution import Execution
from vaultkeep.backup.paths import user_workflow_paths
from vaultkeep.backup.results import BackupInterrupted, classify_command
from vaultkeep.backup.status import backup_status
from vaultkeep.cli import main
from vaultkeep.cli.status import collect_status
from vaultkeep.errors import DestinationError, LockError, StateError
from vaultkeep.locking import JobLock, job_lock_owner, job_lock_path
from vaultkeep.state.identity import job_identity_hash


@pytest.fixture
def local_job(tmp_path, valid_config, monkeypatch):
    for key, name in (
        ("XDG_STATE_HOME", "state"),
        ("XDG_CACHE_HOME", "cache"),
        ("XDG_RUNTIME_DIR", "runtime"),
    ):
        monkeypatch.setenv(key, str(tmp_path / name))
    source, destination = tmp_path / "source", tmp_path / "backups"
    source.mkdir()
    (source / "data").write_text("source data")
    destination.mkdir()
    valid_config["sources"][0].update(path=str(source))
    valid_config["destination"].update(root=str(destination))
    valid_config["schedule"] = {"enabled": False}
    job = tmp_path / "app.yaml"
    job.write_text(json.dumps(valid_config))

    def build(request, **kwargs):
        if kwargs.get("progress"):
            kwargs["progress"]("Verifying archive")
        request.archive_path.write_bytes(b"archive")
        digest = calculate_archive_sha256(request.archive_path)
        write_checksum_sidecar(request.archive_path, request.checksum_path, digest)
        return ArchiveArtifact(
            request.archive_format,
            request.archive_path,
            request.checksum_path,
            digest,
            request.archive_path.stat().st_size,
            request.expected_source_digest,
        )

    monkeypatch.setattr(workflow, "build_archive", build)
    return job


def status(job):
    return backup_status(user_workflow_paths(), "app", job_identity_hash(job, "app"))


def execution(job):
    return Execution(user_workflow_paths(), "app", job_identity_hash(job, "app"))


def test_manual_execution_records_success_then_unchanged_without_new_archive(local_job):
    first = workflow.run_backup(local_job, user_mode=True)
    created = status(local_job)
    assert created["activity"] == "IDLE"
    assert created["previous"]["result"] == "SUCCESS"
    assert created["previous"]["completed_at"]
    assert created["previous"]["duration_seconds"] >= 0
    assert first.archive_path.is_file()
    assert workflow.run_backup(local_job, user_mode=True).result == "unchanged"
    assert status(local_job)["previous"]["result"] == "UNCHANGED"
    assert len([path for path in (local_job.parent / "backups").iterdir() if path.is_dir()]) == 1


def test_running_execution_keeps_previous_result_and_reports_both_elapsed_times(
    local_job, monkeypatch
):
    workflow.run_backup(local_job, user_mode=True)
    before = status(local_job)["previous"]
    with execution(local_job) as active:
        active.report("Verifying archive")
        snapshot = json.loads(active._reporter._path.read_text())
        started = datetime.fromisoformat(snapshot["started_at"])
        now = started + timedelta(seconds=50)
        snapshot.update(
            phase_started_at=(now - timedelta(seconds=8)).isoformat(),
            updated_at=now.isoformat(),
        )
        monkeypatch.setattr("vaultkeep.diagnostics.utc_now", lambda: now.isoformat())
        active._reporter._path.write_text(json.dumps(snapshot))
        current = status(local_job)
        assert current["activity"] == "RUNNING"
        assert 8 <= current["current"]["phase_elapsed_seconds"] < 10
        assert 50 <= current["current"]["elapsed_seconds"] < 52
        assert current["previous"] == before
        active.complete("unchanged")


def test_lock_rejection_cannot_overwrite_any_execution_evidence(local_job):
    with execution(local_job) as active:
        active.report("Creating and compressing archive")
        records = {p: p.read_bytes() for p in active.directory.iterdir()}
        with pytest.raises(LockError):
            workflow.run_backup(local_job, user_mode=True)
        assert {p: p.read_bytes() for p in active.directory.iterdir()} == records
        assert status(local_job)["current"]["phase"] == "Creating and compressing archive"
        active.complete("created")


@pytest.mark.parametrize(
    "change",
    [
        {"updated_at": "2000-01-01T00:00:00Z"},
        {"updated_at": "2999-01-01T00:00:00Z"},
        {"started_at": "invalid"},
        {"phase_started_at": "2000-01-01T00:00:00Z"},
        {"pid": 1},
        {"create_time": 1.0},
        {"execution_id": "other"},
        {"phase": "\x1b[31mspoofed"},
        {"schema_version": True},
    ],
)
def test_invalid_progress_never_proves_running(local_job, change):
    with execution(local_job) as active:
        path = active._reporter._path
        path.write_text(json.dumps(json.loads(path.read_text()) | change))
        assert status(local_job)["activity"] == "UNKNOWN"
        active.complete("created")


@pytest.mark.parametrize("evidence", ["missing", "corrupt", "unreadable"])
def test_unavailable_progress_is_safe_and_completion_wins(local_job, monkeypatch, evidence):
    with execution(local_job) as active:
        path = active._reporter._path
        if evidence == "missing":
            path.unlink()
        elif evidence == "corrupt":
            path.write_text("{bad")
        else:
            original = Path.open

            def open_file(self, *args, **kwargs):
                if self == path:
                    raise PermissionError("telemetry unreadable")
                return original(self, *args, **kwargs)

            monkeypatch.setattr(Path, "open", open_file)
        assert status(local_job)["activity"] == "UNKNOWN"
        active.complete("created")
    assert status(local_job)["activity"] == "IDLE"
    assert status(local_job)["previous"]["result"] == "SUCCESS"


@pytest.mark.parametrize("alive", [False, None])
def test_process_identity_must_be_verified_even_with_a_held_lock(local_job, monkeypatch, alive):
    with execution(local_job) as active:
        monkeypatch.setattr("vaultkeep.backup.status.process_alive", lambda *_args: alive)
        assert status(local_job)["activity"] == "UNKNOWN"
        active.complete("created")


def test_finished_progress_does_not_establish_success(local_job):
    with execution(local_job) as active:
        active._reporter.finish()
        assert status(local_job)["activity"] == "FINISHING"
        assert status(local_job)["previous"] is None
        active.complete("created")


def test_runtime_validation_failure_is_recorded_before_archive_state_exists(local_job, monkeypatch):
    def fail(*_args, **_kwargs):
        raise DestinationError("Destination unavailable")

    monkeypatch.setattr(workflow, "_validate_runtime", fail)
    with pytest.raises(DestinationError):
        workflow.run_backup(local_job, user_mode=True)
    current = status(local_job)
    assert current["activity"] == "IDLE"
    assert current["previous"]["result"] == "FAILED"
    assert current["previous"]["exit_code"] == 5
    assert current["previous"]["reason"] == "Destination unavailable"
    assert "Destination unavailable" in Path(current["previous"]["log_path"]).read_text()


def test_sigterm_records_interruption_and_restores_the_previous_signal_handler(local_job):
    original = signal.getsignal(signal.SIGTERM)
    with pytest.raises(BackupInterrupted), execution(local_job):
        os.kill(os.getpid(), signal.SIGTERM)
    assert signal.getsignal(signal.SIGTERM) == original
    previous = status(local_job)["previous"]
    assert previous["result"] == "INTERRUPTED"
    assert previous["exit_code"] == 143 and previous["signal"] == signal.SIGTERM


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT])
def test_archive_cleanup_failure_preserves_cli_interrupt_and_history(
    local_job, monkeypatch, signum
):
    def create_archive(snapshot, path, **kwargs):
        path.write_bytes(b"archive")

    def interrupt(*args, **kwargs):
        raise BackupInterrupted(signum)

    unlink = Path.unlink

    def remove(path, *args, **kwargs):
        if path.name.endswith(".tar.zst"):
            raise PermissionError("forced archive cleanup failure")
        unlink(path, *args, **kwargs)

    monkeypatch.setattr(workflow, "build_archive", builder.build_archive)
    monkeypatch.setattr(builder, "create_tar_zstd", create_archive)
    monkeypatch.setattr(builder, "verify_tar_zstd", interrupt)
    monkeypatch.setattr(Path, "unlink", remove)

    assert main(["--user", "--config", str(local_job), "run"]) == 128 + signum
    previous = status(local_job)["previous"]
    assert previous["result"] == "INTERRUPTED"
    assert previous["exit_code"] == 128 + signum
    assert previous["signal"] == signum
    assert "forced archive cleanup failure" in Path(previous["log_path"]).read_text()


@pytest.mark.parametrize("outcome", ["FAILED", "INTERRUPTED", "UNKNOWN"])
def test_unsuccessful_previous_backup_shows_log_without_verbose(local_job, capsys, outcome):
    if outcome == "UNKNOWN":
        with execution(local_job) as active:
            started = (active.directory / "execution.json").read_bytes()
            active.complete("created")
        (active.directory / "execution.json").write_bytes(started)
    else:
        error = (
            DestinationError("Destination unavailable")
            if outcome == "FAILED"
            else BackupInterrupted(signal.SIGTERM)
        )
        with pytest.raises(type(error)), execution(local_job):
            raise error
    expected_log = status(local_job)["previous"]["log_path"]
    assert Path(expected_log).is_file()
    args = ["--user", "--config", str(local_job), "status"]
    assert main(args) == 0
    human = " ".join(capsys.readouterr().out.split())
    assert f"Previous backup Result {outcome}" in human
    assert f"Log {expected_log}" in human
    assert "Backup diagnostics" not in human
    assert main([*args, "--json"]) == 0
    previous = json.loads(capsys.readouterr().out)["backup"]["previous"]
    assert previous["result"] == outcome
    assert previous["log_path"] == expected_log


def test_no_completion_record_after_process_exit_is_idle_with_unknown_outcome(local_job):
    with execution(local_job) as active:
        saved = (active.directory / "execution.json").read_bytes()
        active.complete("created")
    (active.directory / "execution.json").write_bytes(saved)
    observed = status(local_job)
    assert observed["activity"] == "IDLE"
    assert observed["previous"]["result"] == "UNKNOWN"
    assert observed["previous"]["completed_at"] is None
    with execution(local_job) as active:
        assert status(local_job)["previous"]["result"] == "UNKNOWN"
        active.complete("created")


def test_lost_completion_persistence_is_not_reported_as_success(local_job, monkeypatch):
    with execution(local_job) as active:

        def fail(*_args, **_kwargs):
            raise StateError("disk full")

        monkeypatch.setattr("vaultkeep.backup.execution.write_json", fail)
        active.complete("created")
    assert status(local_job)["previous"]["result"] == "UNKNOWN"


def test_all_telemetry_writes_failing_cannot_reuse_an_old_success(local_job, monkeypatch):
    workflow.run_backup(local_job, user_mode=True)

    def fail(*_args, **_kwargs):
        raise StateError("disk full")

    monkeypatch.setattr("vaultkeep.backup.execution.write_json", fail)
    with execution(local_job) as active:
        active.complete("created")
    observed = status(local_job)
    assert observed["activity"] == "IDLE"
    assert observed["previous"]["result"] == "UNKNOWN"
    assert observed["previous"]["execution_id"] == active.execution_id
    assert observed["previous"]["completed_at"] is None


def test_heartbeat_runs_without_a_terminal_and_is_joined(local_job, monkeypatch):
    beat = Event()
    monkeypatch.setattr("vaultkeep.backup.execution.HEARTBEAT_SECONDS", 0.01)
    with execution(local_job) as active:
        original = active._reporter.heartbeat

        def heartbeat():
            original()
            beat.set()

        monkeypatch.setattr(active._reporter, "heartbeat", heartbeat)
        assert beat.wait(2)
        active.complete("created")
    assert not active._thread.is_alive()


def test_progress_failure_and_broken_presentation_do_not_fail_backup(local_job, monkeypatch):
    def fail(*_args):
        raise OSError("telemetry unavailable")

    monkeypatch.setattr("vaultkeep.backup.progress.ProgressReporter._publish", fail)
    assert workflow.run_backup(local_job, user_mode=True, progress=fail).result == "created"
    assert status(local_job)["previous"]["result"] == "SUCCESS"


@pytest.mark.parametrize("payload", ["{", "{}", "[]", "x" * 16385])
def test_corrupt_history_does_not_obscure_idle_activity_and_is_never_repaired(local_job, payload):
    with execution(local_job) as active:
        active.complete("created")
    path = active.directory / "execution.json"
    path.write_text(payload)
    observed = status(local_job)
    assert observed["activity"] == "IDLE"
    assert observed["previous"]["result"] == "UNKNOWN"
    assert observed["read_error"]
    assert path.read_text() == payload


def test_status_needs_no_sources_destination_or_execution_lock(local_job, monkeypatch, capsys):
    workflow.run_backup(local_job, user_mode=True)
    local_job.parent.joinpath("source").rename(local_job.parent / "offline-source")
    local_job.parent.joinpath("backups").rename(local_job.parent / "offline-backups")
    before = {
        p: (p.read_bytes(), p.stat().st_mtime_ns)
        for p in local_job.parent.rglob("*")
        if p.is_file()
    }
    monkeypatch.setattr(
        JobLock, "acquire", lambda *_: pytest.fail("status cannot acquire an execution lock")
    )
    for json_output in (False, True):
        assert (
            main(
                [
                    "--user",
                    "--config",
                    str(local_job),
                    "status",
                    *(["--json"] if json_output else []),
                ]
            )
            == 0
        )
        result = capsys.readouterr()
        assert result.err == ""
        if json_output:
            data = json.loads(result.out)
            assert data["backup"]["previous"]["result"] == "SUCCESS"
            assert "trigger" not in data and "schedule" not in data
        else:
            assert "Vaultkeep — Job status" in result.out
            assert "Previous backup" in result.out and "SUCCESS" in result.out
    assert {
        p: (p.read_bytes(), p.stat().st_mtime_ns)
        for p in local_job.parent.rglob("*")
        if p.is_file()
    } == before


def test_never_executed_job_is_idle_without_creating_files(local_job, capsys):
    data = collect_status(local_job, user_mode=True, runtime_params={})
    assert data["backup"]["activity"] == "IDLE"
    assert data["backup"]["previous"] is None
    assert not data["backup"]["diagnostics"]
    assert main(["--user", "--config", str(local_job), "status"]) == 0
    human = " ".join(capsys.readouterr().out.split())
    assert "Activity IDLE" in human and "Result NONE" in human
    assert "No execution evidence" not in human
    assert not user_workflow_paths().state_root.exists()
    assert not user_workflow_paths().lock_root.exists()


def test_root_and_parameter_namespaces_are_independent(local_job, monkeypatch):
    paths = user_workflow_paths()
    monkeypatch.setattr("vaultkeep.cli.status.WorkflowPaths", lambda: paths)
    with execution(local_job) as active:
        root = collect_status(local_job, user_mode=False, runtime_params={})
        assert root["backup"]["activity"] == "RUNNING"
        other = backup_status(
            paths, "app", job_identity_hash(local_job, "app", runtime_params={"repo": "other"})
        )
        assert other["activity"] == "IDLE" and other["previous"] is None
        active.complete("created")


@pytest.mark.parametrize("history", ["missing", "completed", "corrupt"])
def test_unavailable_lock_ownership_cannot_claim_idle(local_job, monkeypatch, history):
    if history != "missing":
        workflow.run_backup(local_job, user_mode=True)
    if history == "corrupt":
        next(user_workflow_paths().state_root.rglob("execution.json")).write_text("{")
    monkeypatch.setattr("vaultkeep.backup.status.job_lock_owner", lambda _path: None)
    assert status(local_job)["activity"] == "UNKNOWN"


def test_held_lock_without_execution_history_is_unknown(local_job):
    active = execution(local_job)
    active.lock.acquire()
    try:
        assert status(local_job)["activity"] == "UNKNOWN"
    finally:
        active.lock.release()


def test_lost_execution_history_retains_unknown_previous_outcome(local_job):
    with execution(local_job) as active:
        active.complete("created")
    (active.directory / "execution.json").unlink()
    observed = status(local_job)
    assert observed["activity"] == "IDLE"
    assert observed["previous"]["result"] == "UNKNOWN"
    assert observed["previous"]["execution_id"] == active.execution_id
    assert observed["previous"]["completed_at"] is None


def test_changing_execution_during_inspection_is_unknown(local_job, monkeypatch):
    stamps = iter(char * 32 for char in "abcd")
    monkeypatch.setattr("vaultkeep.backup.status.job_lock_execution", lambda _path: next(stamps))
    assert status(local_job)["activity"] == "UNKNOWN"


def test_per_file_job_has_one_progress_owner_and_one_aggregate_result(local_job):
    data = json.loads(local_job.read_text())
    data["backup_mode"] = "per_file"
    local_job.write_text(json.dumps(data))
    (local_job.parent / "source" / "second").write_text("another wallet")
    result = workflow.run_backup(local_job, user_mode=True)
    assert result.backups == 2
    paths = user_workflow_paths()
    assert len(list(paths.state_root.rglob("execution.json"))) == 1
    assert len(list(paths.state_root.rglob("progress.json"))) == 1
    assert len(list(paths.state_root.rglob("state.json"))) == 2
    assert status(local_job)["previous"]["result"] == "SUCCESS"


def test_lock_owner_probe_never_changes_the_lock(local_job):
    path = job_lock_path(root=user_workflow_paths().lock_root, job_id="app", identity_hash="a" * 16)
    assert job_lock_owner(path) == 0 and not path.exists()
    lock = JobLock(path)
    lock.acquire()
    try:
        before = path.stat().st_mtime_ns
        assert job_lock_owner(path) == os.getpid()
        assert path.stat().st_mtime_ns == before
    finally:
        lock.release()
    assert job_lock_owner(path) == 0


@pytest.mark.parametrize(
    "code,marker,outcome",
    [
        (0, "run: created", "SUCCESS"),
        (0, "run: unchanged", "UNCHANGED"),
        (0, None, "UNKNOWN"),
        (10, None, "DEFERRED"),
        (2, None, "EXECUTION ERROR"),
        (5, None, "FAILED"),
    ],
)
def test_common_child_outcomes(code, marker, outcome):
    assert classify_command(code, marker) == outcome


@pytest.mark.parametrize("component", ["trigger", "schedule"])
def test_obsolete_status_commands_are_removed(component):
    with pytest.raises(SystemExit) as error:
        main(["--user", "--config", "job.yaml", component, "status"])
    assert error.value.code == 2


@pytest.mark.parametrize(
    "change",
    [
        {"schema_version": True},
        {"identity_hash": "other"},
        {"job_id": "other"},
        {"previous.exit_code": 5},
        {"previous.completed_at": None},
    ],
)
def test_contradictory_completion_cannot_establish_success(local_job, change):
    workflow.run_backup(local_job, user_mode=True)
    path = next(user_workflow_paths().state_root.rglob("execution.json"))
    data = json.loads(path.read_text())
    for key, value in change.items():
        if "." in key:
            group, field = key.split(".")
            data[group][field] = value
        else:
            data[key] = value
    path.write_text(json.dumps(data))
    result = status(local_job)
    assert result["activity"] == "IDLE" and result["previous"]["result"] == "UNKNOWN"


def test_unreadable_completion_does_not_obscure_idle_activity(local_job, monkeypatch):
    workflow.run_backup(local_job, user_mode=True)
    original = Path.open

    def read(self, *args, **kwargs):
        if self.name == "execution.json":
            raise PermissionError("evidence unreadable")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", read)
    result = status(local_job)
    assert result["activity"] == "IDLE" and result["read_error"]
    assert result["previous"]["result"] == "UNKNOWN"


@pytest.mark.parametrize("field", ["schema_version", "current", "previous"])
def test_incomplete_execution_document_is_not_treated_as_completion(local_job, field):
    workflow.run_backup(local_job, user_mode=True)
    path = next(user_workflow_paths().state_root.rglob("execution.json"))
    data = json.loads(path.read_text())
    del data[field]
    path.write_text(json.dumps(data))
    result = status(local_job)
    assert result["activity"] == "IDLE" and result["read_error"]
    assert result["previous"]["result"] == "UNKNOWN"


@pytest.mark.parametrize("failure", ["heartbeat", "process_identity"])
def test_optional_reporting_setup_failure_does_not_fail_execution(local_job, monkeypatch, failure):
    def fail(*_args, **_kwargs):
        if failure == "process_identity":
            import psutil

            raise psutil.AccessDenied()
        raise RuntimeError("thread unavailable")

    if failure == "heartbeat":
        monkeypatch.setattr("vaultkeep.backup.execution.Thread.start", fail)
    else:
        monkeypatch.setattr("vaultkeep.backup.execution.psutil.Process", fail)
    assert workflow.run_backup(local_job, user_mode=True).result == "created"
    assert status(local_job)["previous"]["result"] == "SUCCESS"


def test_symlink_lock_cannot_be_stamped_or_damage_its_target(local_job):
    paths = user_workflow_paths()
    paths.lock_root.mkdir(parents=True)
    target = local_job.parent / "preserve"
    target.write_text("preserve this file")
    lock = job_lock_path(
        root=paths.lock_root, job_id="app", identity_hash=job_identity_hash(local_job, "app")
    )
    lock.symlink_to(target)
    with pytest.raises(OSError):
        workflow.run_backup(local_job, user_mode=True)
    assert target.read_text() == "preserve this file"
    assert status(local_job)["activity"] == "UNKNOWN"
