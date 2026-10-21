"""Independent wallet histories through the real archive and CLI workflows."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from vaultkeep import hooks, workflow
from vaultkeep.cli.commands import main
from vaultkeep.errors import ArchiveCreationError, DestinationError, LockError
from vaultkeep.file_backups import file_key
from vaultkeep.locking import JobLock, job_lock_path
from vaultkeep.state.identity import job_identity_hash
from vaultkeep.trigger.models import AttemptOutcome
from vaultkeep.trigger.results import classify_exit

pytestmark = pytest.mark.skipif(
    not all(Path(tool).is_file() for tool in ("/usr/bin/tar", "/usr/bin/zstd")),
    reason="Real archive tools are unavailable",
)


@pytest.fixture
def file_job(tmp_path: Path, valid_config: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> Path:
    source = tmp_path / "wallets"
    destination = tmp_path / "backups"
    source.mkdir()
    destination.mkdir()
    for name in ("personal wallet", "savings"):
        (source / name).write_text(f"disposable {name}")
    valid_config["backup_mode"] = "per_file"
    valid_config["sources"][0].update(
        path=str(source), archive_prefix="wallets", exclude=["*.lock"]
    )
    valid_config["destination"].update(root=str(destination), mount_point=None)
    valid_config["retention"] = dict(hourly=1, daily=0, weekly=0, monthly=0, yearly=0)
    job = tmp_path / "app.yaml"
    job.write_text(json.dumps(valid_config))
    paths = workflow.WorkflowPaths(tmp_path / "state", tmp_path / "cache", tmp_path / "locks")
    monkeypatch.setattr(workflow, "user_workflow_paths", lambda: paths)

    class Clock:
        current = datetime(2026, 10, 21, 12, tzinfo=UTC)

        @classmethod
        def now(cls) -> datetime:
            cls.current += timedelta(seconds=1)
            return cls.current

    monkeypatch.setattr(workflow, "datetime", Clock)
    return job


def _run(job: Path) -> workflow.CommandResult:
    return workflow.run_backup(job, user_mode=True)


def test_individual_archives_restore_and_only_changed_wallets_are_backed_up(file_job: Path) -> None:
    source = file_job.parent / "wallets"
    (source / "temporary.tmp").touch()
    (source / "temporary.lock").touch()
    (source / "nested").mkdir()
    (source / "nested" / "other-wallet").touch()
    (source / "link").symlink_to(source / "savings")
    first = _run(file_job)
    assert first.result == "created"
    assert first.backups == 2
    for outcome in first.files:
        restore = file_job.parent / f"restore-{outcome.source.name}"
        restore.mkdir()
        subprocess.run(
            ["/usr/bin/tar", "--zstd", "-xf", str(outcome.archive_path), "-C", str(restore)],
            check=True,
            capture_output=True,
        )
        assert (
            restore / "wallets" / outcome.source.name
        ).read_bytes() == outcome.source.read_bytes()
        assert list((restore / "wallets").iterdir()) == [restore / "wallets" / outcome.source.name]

    assert _run(file_job).result == "unchanged"
    personal = source / "personal wallet"
    personal.write_text("changed wallet")
    second = _run(file_job)
    assert [outcome.result for outcome in second.files] == ["created", "unchanged"]
    assert second.removed == 1
    assert second.files[1].archive_path == first.files[1].archive_path
    assert first.files[1].archive_path.is_file()
    assert not first.files[0].archive_path.exists()
    os.utime(personal, None)
    assert _run(file_job).result == "unchanged"

    (source / "new wallet").write_text("new disposable wallet")
    assert [outcome.result for outcome in _run(file_job).files] == [
        "created",
        "unchanged",
        "unchanged",
    ]
    shutil.rmtree(file_job.parent / "state")
    assert _run(file_job).result == "unchanged"


def test_deleted_renamed_and_excluded_wallet_histories_remain_manageable(file_job: Path) -> None:
    first = _run(file_job)
    source = file_job.parent / "wallets"
    (source / "personal wallet").rename(source / "renamed")
    (source / "savings").unlink()
    renamed = _run(file_job)
    assert [outcome.result for outcome in renamed.files] == ["created"]
    assert all(outcome.archive_path.is_file() for outcome in first.files)
    document = json.loads(file_job.read_text())
    document["exclude"].append("renamed")
    file_job.write_text(json.dumps(document))
    assert _run(file_job).files == ()
    assert workflow.verify_backups(file_job).backups == 3
    # Management also works after the entire source directory is removed.
    shutil.rmtree(source)
    assert workflow.list_backups(file_job)[0].backups == 3
    assert workflow.verify_backups(file_job).backups == 3
    assert workflow.prune_backups(file_job, dry_run=False).removed == 0


def test_partial_failure_continues_reports_nonzero_and_retries_only_failed_wallet(
    file_job: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    original = workflow.build_archive

    def build(request: Any, **kwargs: Any) -> Any:
        if request.snapshot.entries[0].absolute_path.name == "personal wallet":
            raise ArchiveCreationError("simulated archive failure")
        return original(request, **kwargs)

    with monkeypatch.context() as scoped:
        scoped.setattr(workflow, "build_archive", build)
        exit_code = main(["--user", "--config", str(file_job), "run"])
    output = capsys.readouterr()
    assert exit_code == 7
    assert output.out == "run: failed\n"
    assert "'personal wallet': failed" in output.err
    assert "simulated archive failure" in output.err
    assert "'savings': created" in output.err
    assert classify_exit(exit_code, output.out) == AttemptOutcome.BACKUP_FAILED
    assert [outcome.result for outcome in _run(file_job).files] == ["created", "unchanged"]
    assert main(["--user", "--config", str(file_job), "run"]) == 0
    output = capsys.readouterr()
    assert output.out == "run: unchanged\n"
    assert classify_exit(0, output.out) == AttemptOutcome.UNCHANGED


def test_empty_directory_creates_nothing_and_does_not_prune_histories(file_job: Path) -> None:
    first = _run(file_job)
    for source in (file_job.parent / "wallets").iterdir():
        source.unlink()
    result = _run(file_job)
    assert result.result == "unchanged"
    assert result.files == ()
    assert all(outcome.archive_path.is_file() for outcome in first.files)


def test_lifecycle_hooks_run_for_each_selected_file(
    file_job: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The sandbox maps host shell ownership to nobody; ownership validation has
    # separate unit tests. Exercise the real shell and per-file lifecycle here.
    monkeypatch.setattr(workflow, "validate_hook_shell", lambda **kwargs: Path("/bin/sh"))
    monkeypatch.setattr(hooks, "validate_hook_shell", lambda **kwargs: Path("/bin/sh"))
    log = file_job.parent / "hooks.log"
    document = json.loads(file_job.read_text())
    document["hooks"] = {
        phase: {"command": f"printf '{message}\\n' >> {shlex.quote(str(log))}"}
        for phase, message in (
            ("before_check", "check"),
            ("on_success", "created"),
            ("on_unchanged", "unchanged"),
        )
    }
    file_job.write_text(json.dumps(document))
    assert _run(file_job).result == "created"
    assert _run(file_job).result == "unchanged"
    assert log.read_text().splitlines() == [
        "check",
        "created",
        "check",
        "created",
        "check",
        "unchanged",
        "check",
        "unchanged",
    ]


def test_whole_job_lock_prevents_overlapping_manual_or_trigger_runs(file_job: Path) -> None:
    lock = JobLock(
        job_lock_path(
            root=file_job.parent / "locks",
            job_id="app",
            identity_hash=job_identity_hash(file_job, "app"),
        )
    )
    lock.acquire()
    try:
        with pytest.raises(LockError):
            _run(file_job)
    finally:
        lock.release()
    assert list((file_job.parent / "backups").iterdir()) == []


def test_parent_marker_is_checked_before_creating_per_file_directories(file_job: Path) -> None:
    document = json.loads(file_job.read_text())
    document["destination"]["marker_file"] = "share-marker"
    file_job.write_text(json.dumps(document))
    with pytest.raises(DestinationError, match="marker is missing"):
        _run(file_job)
    assert list((file_job.parent / "backups").iterdir()) == []
    (file_job.parent / "backups" / "share-marker").touch()
    assert _run(file_job).result == "created"
    assert workflow.verify_backups(file_job).backups == 2


def test_managed_destination_link_is_rejected_without_writing_through_it(file_job: Path) -> None:
    outside = file_job.parent / "outside"
    outside.mkdir()
    (file_job.parent / "backups" / file_key("personal wallet")).symlink_to(outside)
    result = _run(file_job)
    assert [outcome.result for outcome in result.files] == ["failed", "created"]
    assert list(outside.iterdir()) == []


def test_file_replaced_with_symlink_after_selection_is_not_archived(
    file_job: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = workflow.select_files

    def select(config: Any) -> tuple[Path, ...]:
        selected = original(config)
        selected[0].unlink()
        selected[0].symlink_to(selected[1])
        return selected

    monkeypatch.setattr(workflow, "select_files", select)
    assert [outcome.result for outcome in _run(file_job).files] == ["failed", "created"]


def test_manual_prune_validates_all_histories_before_deleting_anything(file_job: Path) -> None:
    document = json.loads(file_job.read_text())
    document["retention"]["hourly"] = 24
    file_job.write_text(json.dumps(document))
    first = _run(file_job)
    for source in (file_job.parent / "wallets").iterdir():
        source.write_text("changed")
    # Keep both backups for later pruning by putting them in separate hours.
    workflow.datetime.current += timedelta(hours=1)
    _run(file_job)
    assert workflow.list_backups(file_job)[0].backups == 4
    document["retention"]["hourly"] = 1
    file_job.write_text(json.dumps(document))
    broken = first.files[-1].archive_path.with_suffix(".zst.sha256")
    original = broken.read_bytes()
    broken.write_text("invalid checksum")
    with pytest.raises(DestinationError, match="malformed"):
        workflow.prune_backups(file_job, dry_run=False)
    assert all(outcome.archive_path.is_file() for outcome in first.files)
    broken.write_bytes(original)
    assert workflow.prune_backups(file_job, dry_run=True).removed == 0
    assert workflow.prune_backups(file_job, dry_run=False).removed == 2
    assert workflow.verify_backups(file_job).backups == 2
