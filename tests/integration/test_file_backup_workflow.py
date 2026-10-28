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

from vaultkeep import hooks
from vaultkeep.backup import workflow
from vaultkeep.backup.results import classify_command
from vaultkeep.cli.commands import main
from vaultkeep.errors import ArchiveCreationError, DestinationError, LockError, PruneError
from vaultkeep.file_backups import file_key
from vaultkeep.locking import JobLock, destination_lock, held_destination_filesystem, job_lock_path
from vaultkeep.state.identity import job_identity_hash

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
    valid_config["destination"].update(root=str(destination))
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


def test_damaged_wallet_backup_is_replaced_with_a_restorable_archive(file_job: Path) -> None:
    first = _run(file_job)
    damaged = first.files[0].archive_path
    assert damaged is not None
    damaged.write_bytes(b"corrupted compressed archive")

    result = _run(file_job)

    assert [outcome.result for outcome in result.files] == ["created", "unchanged"]
    replacement = result.files[0].archive_path
    assert replacement is not None and replacement != damaged
    assert not damaged.parent.exists()
    restored = subprocess.run(
        [
            "/usr/bin/tar",
            "--zstd",
            "-xOf",
            str(replacement),
            f"wallets/{result.files[0].source.name}",
        ],
        check=True,
        capture_output=True,
    )
    assert restored.stdout == result.files[0].source.read_bytes()
    assert _run(file_job).result == "unchanged"


def test_per_file_notifications_report_one_job_result_with_partial_failure(file_job, monkeypatch):
    from tests.trigger.conftest import RecordingNotifier

    document = json.loads(file_job.read_text())
    document["notifications"] = {"backup": {"enabled": True}}
    file_job.write_text(json.dumps(document))
    notifier = RecordingNotifier()
    monkeypatch.setattr("vaultkeep.backup.notifications.build_notifier", lambda _config: notifier)
    assert _run(file_job).result == "created"
    assert _run(file_job).result == "unchanged"
    original = workflow.build_archive

    def fail_one(request, **kwargs):
        if "personal wallet" in str(request.snapshot.entries):
            raise ArchiveCreationError("archive tool failed for personal wallet")
        return original(request, **kwargs)

    monkeypatch.setattr(workflow, "build_archive", fail_one)
    (file_job.parent / "wallets" / "personal wallet").write_text("changed")
    assert _run(file_job).result == "failed"
    titles = [title for title, _ in notifier.calls]
    assert titles.count("VaultKeep: Backup started") == 3
    assert titles.count("VaultKeep: Backup completed") == 3
    assert "failed: archive tool failed for personal wallet" in notifier.calls[-1][1]
    assert len({key for key, _ in notifier.options}) == 3


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


def test_numbered_directories_and_legacy_backups_share_manageable_histories(file_job: Path) -> None:
    document = json.loads(file_job.read_text())
    document["destination"]["name_template"] = "backup-{job}-{timestamp_utc:%Y%m%d}"
    document["retention"]["hourly"] = 24
    file_job.write_text(json.dumps(document))
    first = _run(file_job)
    original = first.files[0].archive_path
    assert original is not None
    assert original.parent.name == "backup-app-20261021"

    workflow.datetime.current += timedelta(hours=1)
    (file_job.parent / "wallets" / "personal wallet").write_text("changed wallet")
    second = _run(file_job)
    numbered = second.files[0].archive_path
    assert numbered is not None
    assert numbered.parent.name == "backup-app-20261021-2"
    assert numbered.name == original.name
    assert original.is_file()
    assert workflow.verify_backups(file_job, user_mode=True).backups == 3

    manifest = json.loads((original.parent / f"{original.parent.name}.json").read_text())
    legacy = original.parent.with_name(f"{original.parent.name}-{manifest['backup_id']}")
    original.parent.rename(legacy)
    shutil.rmtree(file_job.parent / "state")
    assert _run(file_job).result == "unchanged"
    assert workflow.verify_backups(file_job, user_mode=True).backups == 3
    assert len(workflow.list_backups(file_job, user_mode=True).backups) == 3

    document["retention"]["hourly"] = 1
    file_job.write_text(json.dumps(document))
    assert workflow.prune_backups(file_job, dry_run=False, user_mode=True).removed == 1
    assert not legacy.exists()
    assert numbered.is_file()


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
    assert workflow.verify_backups(file_job, user_mode=True).backups == 3
    # Management also works after the entire source directory is removed.
    shutil.rmtree(source)
    assert len(workflow.list_backups(file_job, user_mode=True).backups) == 3
    assert workflow.verify_backups(file_job, user_mode=True).backups == 3
    assert workflow.prune_backups(file_job, dry_run=False, user_mode=True).removed == 0


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
    assert classify_command(exit_code, output.out) == "FAILED"
    assert [outcome.result for outcome in _run(file_job).files] == ["created", "unchanged"]
    assert main(["--user", "--config", str(file_job), "run"]) == 0
    output = capsys.readouterr()
    assert output.out == "run: unchanged\n"
    assert classify_command(0, output.out) == "UNCHANGED"


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


@pytest.mark.parametrize("expected_fstype", [None, "cifs"])
def test_parent_marker_is_checked_before_creating_per_file_directories(
    file_job: Path, monkeypatch: pytest.MonkeyPatch, expected_fstype: str | None
) -> None:
    document = json.loads(file_job.read_text())
    document["destination"]["marker_file"] = "share-marker"
    document["destination"]["expected_fstype"] = expected_fstype
    file_job.write_text(json.dumps(document))
    inspected: list[Path] = []

    def inspect(path: Path) -> str:
        inspected.append(path)
        return "cifs"

    monkeypatch.setattr(workflow, "_mounted_filesystem_type", inspect)
    with pytest.raises(DestinationError, match="marker is missing"):
        _run(file_job)
    root = file_job.parent / "backups"
    assert list(root.iterdir()) == []
    (root / "share-marker").touch()
    assert _run(file_job).result == "created"
    assert workflow.verify_backups(file_job, user_mode=True).backups == 2
    assert all(
        not (root / file_key(name) / "share-marker").exists()
        for name in ("personal wallet", "savings")
    )
    if expected_fstype is not None:
        assert {root, root / file_key("personal wallet"), root / file_key("savings")} <= (
            set(inspected)
        )
    else:
        assert inspected == []


@pytest.mark.parametrize("marker_file", [None, "share-marker"])
@pytest.mark.parametrize("inspection_fails", [False, True])
def test_parent_filesystem_is_checked_before_creating_per_file_directories(
    file_job: Path,
    monkeypatch: pytest.MonkeyPatch,
    marker_file: str | None,
    inspection_fails: bool,
) -> None:
    document = json.loads(file_job.read_text())
    document["destination"].update(marker_file=marker_file, expected_fstype="cifs")
    file_job.write_text(json.dumps(document))
    root = file_job.parent / "backups"
    if marker_file is not None:
        (root / marker_file).touch()

    def inspect(path: Path) -> str:
        assert path == root
        if inspection_fails:
            raise DestinationError("Cannot inspect destination filesystem")
        return "ext4"

    def unexpected_probe(path: Path) -> None:
        pytest.fail("Parent filesystem safety checks must precede destination writes")

    monkeypatch.setattr(workflow, "_mounted_filesystem_type", inspect)
    monkeypatch.setattr(workflow, "probe_destination_writability", unexpected_probe)
    message = "Cannot inspect" if inspection_fails else "expected cifs, found ext4"
    with pytest.raises(DestinationError, match=message):
        _run(file_job)
    assert {path.name for path in root.iterdir()} == ({marker_file} if marker_file else set())


@pytest.mark.parametrize("failed_guard", ["marker", "filesystem"])
def test_parent_safety_is_rechecked_before_each_per_file_directory(
    file_job: Path, monkeypatch: pytest.MonkeyPatch, failed_guard: str
) -> None:
    document = json.loads(file_job.read_text())
    document["destination"].update(marker_file="share-marker", expected_fstype="cifs")
    file_job.write_text(json.dumps(document))
    root = file_job.parent / "backups"
    marker = root / "share-marker"
    marker.touch()
    actual_fstype = "cifs"
    monkeypatch.setattr(workflow, "_mounted_filesystem_type", lambda path: actual_fstype)
    original = workflow._run_single_backup

    def run_then_change_destination(*args: Any, **kwargs: Any) -> workflow.CommandResult:
        nonlocal actual_fstype
        result = original(*args, **kwargs)
        if failed_guard == "marker":
            marker.unlink()
        else:
            actual_fstype = "ext4"
        return result

    monkeypatch.setattr(workflow, "_run_single_backup", run_then_change_destination)
    result = _run(file_job)
    assert result.result == "failed"
    assert [outcome.result for outcome in result.files] == ["created", "failed"]
    assert not (root / file_key("savings")).exists()


@pytest.mark.parametrize("backup_mode", ["combined", "per_file"])
@pytest.mark.parametrize("missing_state", [False, True])
def test_previous_fingerprint_requires_new_backup_with_existing_or_reconstructed_state(
    file_job: Path, monkeypatch: pytest.MonkeyPatch, backup_mode: str, missing_state: bool
) -> None:
    document = json.loads(file_job.read_text())
    document["backup_mode"] = backup_mode
    document["retention"]["hourly"] = 24
    file_job.write_text(json.dumps(document))
    previous_fingerprint = "sha256:25833b2e0667992f839d675e10a7d65622251b1fc374b38fc5d90949faa16b84"
    with monkeypatch.context() as scoped:
        scoped.setattr(
            workflow, "calculate_config_fingerprint", lambda *a, **k: previous_fingerprint
        )
        assert _run(file_job).result == "created"
        assert _run(file_job).result == "unchanged"
    if missing_state:
        state_paths = tuple((file_job.parent / "state").rglob("state.json"))
        assert state_paths
        for path in state_paths:
            path.unlink()

    workflow.datetime.current += timedelta(hours=1)
    assert _run(file_job).result == "created"
    assert _run(file_job).result == "unchanged"
    expected_backups = 4 if backup_mode == "per_file" else 2
    assert workflow.verify_backups(file_job, user_mode=True).backups == expected_backups


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


def test_manual_prune_validates_each_history_independently(file_job: Path) -> None:
    document = json.loads(file_job.read_text())
    document["retention"]["hourly"] = 24
    file_job.write_text(json.dumps(document))
    first = _run(file_job)
    for source in (file_job.parent / "wallets").iterdir():
        source.write_text("changed")
    # Keep both backups for later pruning by putting them in separate hours.
    workflow.datetime.current += timedelta(hours=1)
    _run(file_job)
    assert len(workflow.list_backups(file_job, user_mode=True).backups) == 4
    document["retention"]["hourly"] = 1
    file_job.write_text(json.dumps(document))
    broken = first.files[-1].archive_path.with_suffix(".zst.sha256")
    original = broken.read_bytes()
    broken.write_text("invalid checksum")
    with pytest.raises(DestinationError, match="Malformed"):
        workflow.prune_backups(file_job, dry_run=False, user_mode=True)
    assert not first.files[0].archive_path.exists()
    assert first.files[-1].archive_path.is_file()
    broken.write_bytes(original)
    assert workflow.prune_backups(file_job, dry_run=True, user_mode=True).removed == 0
    assert workflow.prune_backups(file_job, dry_run=False, user_mode=True).removed == 1
    assert workflow.verify_backups(file_job, user_mode=True).backups == 2


@pytest.mark.parametrize("operation", ["run", "verify", "prune"])
def test_busy_child_history_does_not_block_siblings(file_job, monkeypatch, operation):
    first = _run(file_job)
    histories = [item.archive_path.parent.parent for item in first.files]
    original_prune = workflow.execute_prune_plan
    monkeypatch.setattr(workflow, "execute_prune_plan", lambda *args: ())
    for source in (file_job.parent / "wallets").iterdir():
        source.write_text("changed wallet")
    _run(file_job)
    monkeypatch.setattr(workflow, "execute_prune_plan", original_prune)
    protected = {path: path.read_bytes() for path in histories[0].rglob("*") if path.is_file()}
    verified = []
    with destination_lock(histories[0]):
        if operation == "run":
            result = _run(file_job)
            assert [item.result for item in result.files] == ["failed", "unchanged"]
            assert "already locked" in result.files[0].error
        elif operation == "verify":
            with pytest.raises(DestinationError, match="already locked"):
                workflow.verify_backups(file_job, user_mode=True, on_verified=verified.append)
            assert len(verified) == 2
            assert all(path.is_relative_to(histories[1]) for path in verified)
        else:
            with pytest.raises(PruneError, match="already locked") as caught:
                workflow.prune_backups(file_job, dry_run=False, user_mode=True)
            assert len(caught.value.removed) == 1
            assert Path(caught.value.removed[0]).is_relative_to(histories[1])
        assert {
            path: path.read_bytes() for path in histories[0].rglob("*") if path.is_file()
        } == protected
    assert held_destination_filesystem(histories[0]) is None
    with destination_lock(histories[0]):
        pass


@pytest.mark.parametrize("operation", ["run", "verify", "prune"])
def test_child_lock_spans_discovery_and_mutation_and_releases_between_histories(
    file_job, monkeypatch, operation
):
    first = _run(file_job)
    histories = [item.archive_path.parent.parent for item in first.files]
    if operation == "run":
        for source in (file_job.parent / "wallets").iterdir():
            source.write_text("changed wallet")
    seen = set()

    def check(root, phase):
        root = root.resolve()
        seen.add((root, phase))
        assert held_destination_filesystem(root) is not None
        with pytest.raises(LockError), destination_lock(root):
            pytest.fail("history mutation was not protected")
        # A previously processed history and the parent remain independent.
        other = next(history for history in histories if history != root)
        with destination_lock(other), destination_lock(root.parent):
            pass
        assert held_destination_filesystem(root) is not None

    discover = workflow.discover_backups
    build = workflow.build_archive
    verify = workflow.verify_tar_zstd
    prune = workflow.execute_prune_plan

    def checked_discover(config, **kwargs):
        check(Path(config.destination.root), "discovery")
        return discover(config, **kwargs)

    def checked_build(request, **kwargs):
        check(request.archive_path.parent.parent, "archive")
        return build(request, **kwargs)

    def checked_verify(path, **kwargs):
        check(path.parent.parent, "archive")
        return verify(path, **kwargs)

    def checked_prune(plan, discovery):
        check(discovery.backups[0].directory.parent, "cleanup")
        return prune(plan, discovery)

    monkeypatch.setattr(workflow, "discover_backups", checked_discover)
    monkeypatch.setattr(workflow, "build_archive", checked_build)
    monkeypatch.setattr(workflow, "verify_tar_zstd", checked_verify)
    monkeypatch.setattr(workflow, "execute_prune_plan", checked_prune)
    if operation == "run":
        assert _run(file_job).result == "created"
    elif operation == "verify":
        assert workflow.verify_backups(file_job, user_mode=True).backups == 2
    else:
        assert workflow.prune_backups(file_job, dry_run=False, user_mode=True).result == "pruned"
    phases = {"discovery", "archive"} if operation != "prune" else {"discovery", "cleanup"}
    if operation == "run":
        phases.add("cleanup")
    assert seen == {(history, phase) for history in histories for phase in phases}
