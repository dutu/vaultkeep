"""Safety boundaries for schema-2 execution, suspect replacement, and cleanup."""

from __future__ import annotations

import json
import os
import pwd
from dataclasses import replace
from pathlib import Path

import pytest

from tests.unit import test_workflow_cleanup as cleanup_fixtures
from vaultkeep.backup import workflow
from vaultkeep.backup.status import backup_status
from vaultkeep.config.models import JobConfig
from vaultkeep.destination.discovery import DiscoveryResult, discover_backups
from vaultkeep.destination.identity import replacement_identity
from vaultkeep.destination.pruning import build_prune_plan, valid_replacement
from vaultkeep.destination.suspect import (
    SUSPECT_MARKER_NAME,
    mark_backup_suspect,
    read_suspect_marker,
    record_suspect_replacement,
)
from vaultkeep.errors import (
    ArchiveVerificationError,
    DestinationError,
    HookError,
    LockError,
    PruneError,
    SourceDiscoveryError,
    StateError,
)
from vaultkeep.locking import destination_lock
from vaultkeep.sources.hashing import calculate_config_fingerprint, calculate_source_digest
from vaultkeep.state.identity import job_identity_hash

job = cleanup_fixtures.job


def run(job):
    return workflow.run_backup(
        job,
        paths=workflow.WorkflowPaths(
            job.parent / "state", job.parent / "cache", job.parent / "locks"
        ),
        user_mode=True,
    )


def configure(job, **changes):
    document = json.loads(job.read_text())
    document.update(changes)
    job.write_text(json.dumps(document))
    return workflow.load_validated_config(job)


def offline(job):
    return configure(
        job,
        consistency={"mode": "application_offline"},
        application={
            "name": "bisq",
            "user": pwd.getpwuid(os.geteuid()).pw_name,
            "data_dir": str(job.parent / "source"),
            "matcher": {"entrypoints": [{"kind": "java_main", "value": "test.App"}]},
        },
    )


def test_policy_configuration_matrix(valid_config):
    for enabled in (False, True):
        for mode in ("files", "application_offline"):
            document = valid_config | {
                "trigger": {"enabled": enabled},
                "consistency": {"mode": mode},
            }
            if enabled or mode == "application_offline":
                with pytest.raises(ValueError, match="application is required"):
                    JobConfig.model_validate(document)
                document["application"] = {
                    "name": "bisq",
                    "user": "owner",
                    "data_dir": "/data",
                    "matcher": {"entrypoints": [{"kind": "java_main", "value": "test.App"}]},
                }
            assert JobConfig.model_validate(document).consistency.mode == mode
    assert JobConfig.model_validate(valid_config).consistency.mode == "files"


def test_replacement_class_excludes_format_retention_and_matcher(job):
    config = offline(job)
    identity = replacement_identity(config)
    changed = config.model_copy(
        update={
            "archive": config.archive.model_copy(update={"format": "tar.7z"}),
            "retention": config.retention.model_copy(update={"daily": 90}),
        }
    )
    assert replacement_identity(changed) == identity
    app = config.application
    changed = config.model_copy(
        update={
            "application": app.model_copy(
                update={"matcher": app.matcher.model_copy(update={"argv_tokens": ["extra"]})}
            )
        }
    )
    assert replacement_identity(changed) == identity
    assert calculate_config_fingerprint(changed) != calculate_config_fingerprint(config)
    assert replacement_identity(config.model_copy(update={"exclude": ["new-pattern"]})) != identity
    assert (
        replacement_identity(
            config.model_copy(
                update={"consistency": config.consistency.model_copy(update={"mode": "files"})}
            )
        )
        != identity
    )


def test_offline_checks_bound_source_reads_and_release_restart(job, monkeypatch):
    offline(job)
    events = []
    original_snapshot, original_build = workflow.discover_sources, workflow.build_archive

    def check(config, *, owner_uid):
        assert owner_uid == os.geteuid()
        events.append("offline")

    def snapshot(config):
        assert "after_archive" not in events
        events.append("sources")
        return original_snapshot(config)

    def build(*args, **kwargs):
        events.append("build")
        return original_build(*args, **kwargs)

    def hook(phase, *args, **kwargs):
        events.append(phase)
        if phase == "before_archive":
            (job.parent / "source/data").write_bytes(b"quiesced version")

    monkeypatch.setattr(workflow, "require_application_offline", check)
    monkeypatch.setattr(workflow, "discover_sources", snapshot)
    monkeypatch.setattr(workflow, "build_archive", build)
    monkeypatch.setattr(workflow, "_run_configured_hook", hook)
    result = run(job)
    assert events == [
        "before_check",
        "offline",
        "sources",
        "before_archive",
        "offline",
        "sources",
        "build",
        "offline",
        "after_archive",
        "on_success",
    ]
    manifest = next(result.archive_path.parent.glob("*.json"))
    assert json.loads(manifest.read_text())["source_digest"] == calculate_source_digest(
        original_snapshot(workflow.load_validated_config(job))
    )


@pytest.mark.parametrize("boundary", [1, 2, 3])
def test_offline_failure_releases_once_and_preserves_existing(job, monkeypatch, boundary):
    previous = run(job).archive_path
    offline(job)
    phases, checks = [], []

    def check(*args, **kwargs):
        checks.append(True)
        if len(checks) == boundary:
            raise SourceDiscoveryError("running")

    def hook(phase, *args, **kwargs):
        phases.append(phase)

    monkeypatch.setattr(workflow, "require_application_offline", check)
    monkeypatch.setattr(workflow, "_run_configured_hook", hook)
    with pytest.raises(SourceDiscoveryError, match="running"):
        run(job)
    assert phases.count("after_archive") == 1
    assert phases[-1] == "on_failure"
    assert previous.is_file()
    assert list((job.parent / "backups").rglob("*.tar.zst")) == [previous]


def test_files_mode_never_inspects_processes(job, monkeypatch):
    monkeypatch.setattr(
        "vaultkeep.trigger.process.PsutilProcessProvider.snapshot",
        lambda *_: pytest.fail("files policy inspected processes"),
    )
    assert run(job).result == "created"
    assert run(job).result == "unchanged"


def test_release_failure_preserves_primary_error(job, monkeypatch):
    def hook(phase, *args, **kwargs):
        if phase == "after_archive":
            raise HookError("release failed")

    def fail(*args, **kwargs):
        raise SourceDiscoveryError("primary")

    monkeypatch.setattr(workflow, "build_archive", fail)
    monkeypatch.setattr(workflow, "_run_configured_hook", hook)
    with pytest.raises(SourceDiscoveryError, match="primary") as caught:
        run(job)
    assert any("release failed" in note for note in caught.value.__notes__)
    assert list((job.parent / "backups").iterdir()) == []


def test_shared_lock_covers_aliases_ancestors_and_all_mutators(job):
    alias = job.parent / "alias"
    alias.symlink_to(job.parent / "backups", target_is_directory=True)
    config = workflow.load_validated_config(job)
    with destination_lock(alias):
        with pytest.raises(LockError):
            run(job)
        with pytest.raises(LockError):
            workflow.verify_backups(job)
        with pytest.raises(LockError):
            workflow.prune_backups(job, dry_run=False)
        assert workflow.prune_backups(job, dry_run=True).result == "planned"
        assert workflow.list_backups(job).backups == ()
        with pytest.raises(LockError), destination_lock(job.parent):
            pass
        child = Path(config.destination.root) / "child"
        child.mkdir()
        with pytest.raises(LockError), destination_lock(child):
            pass
    assert run(job).result == "created"


def test_marker_write_failure_is_cleanup_warning_and_retry_is_explicit(job, monkeypatch):
    first = run(job).archive_path
    mark_backup_suspect(first.parent, "original")
    original = read_suspect_marker(first.parent)
    with monkeypatch.context() as patch:
        patch.setattr(
            workflow,
            "record_suspect_replacement",
            lambda *args: (_ for _ in ()).throw(DestinationError("marker unavailable")),
        )
        result = run(job)
    assert result.result == "created" and result.cleanup_errors
    assert first.is_file() and read_suspect_marker(first.parent) == original
    # An unresolved suspect forces another fresh archive, even with unchanged files.
    assert run(job).result == "created"
    assert not first.exists()
    assert run(job).result == "unchanged"


def test_superseded_suspect_survives_failed_delete_without_forcing_archive(job, monkeypatch):
    from vaultkeep.destination import pruning

    first = run(job).archive_path
    mark_backup_suspect(first.parent, "original")
    original = read_suspect_marker(first.parent)
    with monkeypatch.context() as patch:
        patch.setattr(
            pruning,
            "_remove_valid_backup",
            lambda backup: (_ for _ in ()).throw(PruneError("delete denied")),
        )
        result = run(job)
    assert result.result == "created" and result.cleanup_errors
    marker = read_suspect_marker(first.parent)
    assert marker.reason == original.reason and marker.detected_at == original.detected_at
    assert marker.replacement_backup_id is not None
    assert run(job).result == "unchanged"
    assert first.is_file()
    report = backup_status(
        workflow.WorkflowPaths(job.parent / "state", job.parent / "cache", job.parent / "locks"),
        "app",
        job_identity_hash(job, "app"),
    )
    assert report["previous"]["result"] == "UNCHANGED"
    assert workflow.prune_backups(job, dry_run=False).removed == 1
    assert result.archive_path.is_file() and not first.exists()


def test_corrupt_replacement_prevents_every_deletion(job, monkeypatch):
    first = run(job).archive_path
    mark_backup_suspect(first.parent, "original")
    with monkeypatch.context() as patch:
        patch.setattr(workflow, "execute_prune_plan", lambda *args: ())
        second = run(job).archive_path
    second.write_bytes(b"corrupt")
    with pytest.raises(PruneError, match="corrupt"):
        workflow.prune_backups(job, dry_run=False)
    assert first.is_file() and second.is_file()
    assert read_suspect_marker(second.parent) is not None
    assert run(job).result == "created"
    assert not first.exists() and not second.exists()


@pytest.mark.parametrize("reference", ["missing", "self", "cross-class", "suspect"])
def test_invalid_references_never_authorize_deletion(job, monkeypatch, reference):
    first = run(job).archive_path
    with monkeypatch.context() as patch:
        patch.setattr(workflow, "execute_prune_plan", lambda *args: ())
        (job.parent / "source/data").write_bytes(b"changed")
        second = run(job).archive_path
    config = workflow.load_validated_config(job)
    discovery = discover_backups(config, metadata_only=True)
    a = next(backup for backup in discovery.backups if backup.archive_path == first)
    b = next(backup for backup in discovery.backups if backup.archive_path == second)
    mark_backup_suspect(first.parent, "original")
    identifier = (
        "f" * 32
        if reference == "missing"
        else a.manifest.backup_id
        if reference == "self"
        else b.manifest.backup_id
    )
    record_suspect_replacement(first.parent, identifier)
    if reference == "suspect":
        mark_backup_suspect(second.parent, "also suspect")
    discovery = discover_backups(config, metadata_only=True)
    if reference == "cross-class":
        altered = b.manifest.model_copy(
            update={
                "replacement_identity": replacement_identity(
                    config.model_copy(update={"exclude": ["different"]})
                )
            }
        )
        discovery = DiscoveryResult(
            tuple(
                replace(backup, manifest=altered) if backup.archive_path == second else backup
                for backup in discovery.backups
            ),
            (),
        )
    a = next(backup for backup in discovery.backups if backup.archive_path == first)
    assert valid_replacement(a, discovery) is None
    plan = build_prune_plan(discovery, config.retention)
    assert a not in plan.backups_to_delete


def test_incompatible_history_does_not_force_endless_creation_or_cleanup(job):
    first = run(job).archive_path
    mark_backup_suspect(first.parent, "old scope")
    configure(job, exclude=["different-pattern"])
    assert run(job).result == "created"
    assert first.is_file()
    assert run(job).result == "unchanged"
    assert workflow.prune_backups(job, dry_run=False).removed == 0


@pytest.mark.parametrize("failure", ["state", "success-hook"])
def test_required_postcommit_failure_never_cleans_or_marks_archive(job, monkeypatch, failure):
    first = run(job).archive_path
    mark_backup_suspect(first.parent, "original")
    if failure == "success-hook":

        def fail(phase, *args, **kwargs):
            if phase == "on_success":
                raise HookError("success failed")

        monkeypatch.setattr(workflow, "_run_configured_hook", fail)
    else:
        from vaultkeep.state import atomic

        original = atomic.write_local_state

        def fail(path, state):
            if state.last_successful_backup and state.last_successful_backup.backup_path != str(
                first
            ):
                raise StateError("state unavailable")
            original(path, state)

        monkeypatch.setattr(atomic, "write_local_state", fail)
    with pytest.raises((HookError, StateError)):
        run(job)
    archives = list((job.parent / "backups").rglob("*.tar.zst"))
    assert len(archives) == 2 and first in archives
    assert read_suspect_marker(first.parent).replacement_backup_id is None
    assert read_suspect_marker(next(path for path in archives if path != first).parent) is None


def test_unsupported_matching_manifest_blocks_cleanup(job):
    first = run(job).archive_path
    manifest_path = next(first.parent.glob("*.json"))
    document = json.loads(manifest_path.read_text())
    document["manifest_version"] = 1
    manifest_path.write_text(json.dumps(document))
    (job.parent / "source/data").write_bytes(b"new data")
    with pytest.raises(DestinationError, match="Unsupported destination format"):
        run(job)
    assert list((job.parent / "backups").rglob("*.tar.zst")) == [first]
    with pytest.raises(PruneError, match="malformed"):
        workflow.prune_backups(job, dry_run=False)


def test_child_snapshot_must_match_frozen_policy(job, monkeypatch):
    monkeypatch.setenv(
        "VAULTKEEP_EXPECTED_POLICY",
        json.dumps({"consistency": "application_offline", "application": None, "owner_uid": None}),
    )
    with pytest.raises(SourceDiscoveryError, match="changed after Trigger dispatch"):
        run(job)
    assert not list((job.parent / "backups").iterdir())


def test_explicit_verify_marks_corruption_but_operational_errors_do_not(job, monkeypatch):
    first = run(job).archive_path
    with monkeypatch.context() as patch:
        patch.setattr(
            workflow,
            "verify_tar_zstd",
            lambda *args, **kwargs: (_ for _ in ()).throw(ArchiveVerificationError("tool missing")),
        )
        with pytest.raises(ArchiveVerificationError, match="tool missing"):
            workflow.verify_backups(job)
    assert read_suspect_marker(first.parent) is None
    first.write_bytes(b"corrupt")
    with pytest.raises(ArchiveVerificationError, match="checksum"):
        workflow.verify_backups(job)
    assert read_suspect_marker(first.parent) is not None


@pytest.mark.parametrize(
    "payload",
    [b"", b"{}", b"not-json", b"x" * 4097],
    ids=["empty", "incomplete", "invalid-json", "oversized"],
)
def test_invalid_markers_are_barriers_and_previews_do_not_repair(job, payload):
    first = run(job).archive_path
    marker = first.parent / SUSPECT_MARKER_NAME
    marker.write_bytes(payload)
    before = {path: path.read_bytes() for path in first.parent.iterdir()}
    inventory = workflow.list_backups(job)
    assert inventory.malformed and not inventory.backups
    with pytest.raises(PruneError, match="malformed"):
        workflow.prune_backups(job, dry_run=True)
    with pytest.raises(PruneError, match="malformed"):
        workflow.prune_backups(job, dry_run=False)
    assert {path: path.read_bytes() for path in first.parent.iterdir()} == before


def test_per_file_success_cleans_independently_of_failed_file(job, monkeypatch):
    (job.parent / "source/z-bad").write_bytes(b"other")
    configure(job, backup_mode="per_file")
    initial = run(job)
    old = {item.source.name: item.archive_path for item in initial.files}
    for archive in old.values():
        mark_backup_suspect(archive.parent, "original")
    original_build = workflow.build_archive

    def build(request, **kwargs):
        if request.snapshot.entries[0].absolute_path.name == "z-bad":
            raise ArchiveVerificationError("bad file failed")
        return original_build(request, **kwargs)

    monkeypatch.setattr(workflow, "build_archive", build)
    result = run(job)
    assert result.result == "failed"
    assert not old["data"].exists()
    assert old["z-bad"].exists()
    assert read_suspect_marker(old["z-bad"].parent).replacement_backup_id is None
    assert [item.result for item in result.files] == ["created", "failed"]


def test_offline_final_check_is_independent_per_file(job, monkeypatch):
    (job.parent / "source/z-bad").write_bytes(b"other")
    configure(job, backup_mode="per_file")
    offline(job)
    counts = {}
    phases = []

    def check(config, **kwargs):
        name = Path(config.sources[0].path).name
        counts[name] = counts.get(name, 0) + 1
        if name == "z-bad" and counts[name] == 3:
            raise SourceDiscoveryError("application restarted during bad file")

    def hook(phase, config, *args, **kwargs):
        phases.append((Path(config.sources[0].path).name, phase))

    monkeypatch.setattr(workflow, "require_application_offline", check)
    monkeypatch.setattr(workflow, "_run_configured_hook", hook)
    result = run(job)
    assert result.result == "failed"
    assert counts == {"source": 1, "data": 3, "z-bad": 3}
    assert phases.count(("data", "after_archive")) == 1
    assert phases.count(("z-bad", "after_archive")) == 1
    assert [item.result for item in result.files] == ["created", "failed"]


def test_partial_cleanup_stops_and_reports_completed_deletions(job, monkeypatch):
    from vaultkeep.destination import pruning

    run(job)
    with monkeypatch.context() as patch:
        patch.setattr(workflow, "execute_prune_plan", lambda *args: ())
        for content in (b"second", b"third"):
            (job.parent / "source/data").write_bytes(content)
            run(job)
    archives = list((job.parent / "backups").rglob("*.tar.zst"))
    for archive in archives:
        mark_backup_suspect(archive.parent, "original")
    remove = pruning._remove_valid_backup
    calls = []

    def remove_or_fail(backup):
        calls.append(backup.directory)
        if len(calls) == 2:
            raise PruneError("second deletion denied")
        remove(backup)

    monkeypatch.setattr(pruning, "_remove_valid_backup", remove_or_fail)
    result = run(job)
    assert result.result == "created" and result.cleanup_errors
    assert result.removed == 1
    assert len(calls) == 2
    assert len(list((job.parent / "backups").rglob("*.tar.zst"))) == 3
    assert result.archive_path.is_file()
    assert "after 1 deletions" in result.cleanup_errors[0]


def test_source_hash_name_uses_refreshed_snapshot(job, monkeypatch):
    configure(
        job,
        destination={
            "root": str(job.parent / "backups"),
            "name_template": "backup-{job}-{timestamp_utc:%Y%m%dT%H%M%SZ}-{source_hash:.12}",
        },
    )

    def hook(phase, *args, **kwargs):
        if phase == "before_archive":
            (job.parent / "source/data").write_bytes(b"refreshed")

    monkeypatch.setattr(workflow, "_run_configured_hook", hook)
    result = run(job)
    manifest = json.loads(next(result.archive_path.parent.glob("*.json")).read_text())
    assert manifest["source_digest"].removeprefix("sha256:")[:12] in result.archive_path.name


def test_status_keeps_local_reporting_when_application_account_is_unavailable(job, monkeypatch):
    from vaultkeep.cli import status

    paths = workflow.WorkflowPaths(job.parent / "state", job.parent / "cache", job.parent / "locks")
    run(job)
    config = offline(job)
    with pytest.raises(SourceDiscoveryError):
        configure(
            job,
            application=config.application.model_dump()
            | {"user": "vaultkeep-test-account-does-not-exist"},
            trigger={"enabled": True},
        )
    monkeypatch.setattr(status, "user_workflow_paths", lambda: paths)
    report = status.collect_status(job, user_mode=True, runtime_params={})
    assert report["backup"]["previous"]["result"] == "SUCCESS"
    assert report["trigger"]["application"] == "UNKNOWN"
    assert report["trigger"]["diagnostics"]


def test_offline_inspection_failure_is_not_absence(job, monkeypatch):
    from vaultkeep.trigger.detection import DetectionResult
    from vaultkeep.trigger.models import Observation

    offline(job)
    monkeypatch.setattr(
        "vaultkeep.trigger.detection.detect_spec",
        lambda **kwargs: DetectionResult(
            Observation.INSPECTION_UNAVAILABLE, reason="process metadata inaccessible"
        ),
    )
    monkeypatch.setattr(
        workflow,
        "discover_sources",
        lambda *args: pytest.fail("source discovery preceded reliable offline evidence"),
    )
    with pytest.raises(SourceDiscoveryError, match="metadata inaccessible"):
        run(job)
    assert list((job.parent / "backups").iterdir()) == []


def test_release_hook_cannot_commit_unexpected_staging_artifacts(job, monkeypatch):
    def hook(phase, *args, **kwargs):
        if phase == "after_archive":
            stage = next(
                path
                for path in (job.parent / "backups").iterdir()
                if path.name.startswith("~$.partial")
            )
            (stage / "unexpected").write_bytes(b"hook artifact")

    monkeypatch.setattr(workflow, "_run_configured_hook", hook)
    with pytest.raises(DestinationError, match="unexpected staging artifacts"):
        run(job)
    assert list((job.parent / "backups").iterdir()) == []


def test_explicit_per_file_prune_cleans_valid_history_despite_unsafe_sibling(job, monkeypatch):
    (job.parent / "source/z-bad").write_bytes(b"other")
    configure(job, backup_mode="per_file")
    initial = run(job)
    old = {item.source.name: item.archive_path for item in initial.files}
    mark_backup_suspect(old["data"].parent, "replace me")
    with monkeypatch.context() as patch:
        patch.setattr(workflow, "execute_prune_plan", lambda *args: ())
        run(job)
    bad_history = old["z-bad"].parent.parent
    preserved = bad_history.parent / "operator-copy"
    bad_history.rename(preserved)
    bad_history.symlink_to(preserved, target_is_directory=True)
    before = {
        path.relative_to(preserved): path.read_bytes()
        for path in preserved.rglob("*")
        if path.is_file()
    }
    with pytest.raises(PruneError, match="affected histories") as caught:
        workflow.prune_backups(job, dry_run=False)
    assert not old["data"].exists()
    assert len(caught.value.removed) == 1
    assert {
        path.relative_to(preserved): path.read_bytes()
        for path in preserved.rglob("*")
        if path.is_file()
    } == before


@pytest.mark.parametrize("layout", ["subtree", "nested"])
def test_shared_lock_rejects_mount_layouts_that_hide_ancestor_overlap(job, monkeypatch, layout):
    from vaultkeep import locking

    destination = job.parent / "backups"
    original = Path.read_text

    def mountinfo(path, *args, **kwargs):
        if path == Path("/proc/self/mountinfo"):
            text = "1 0 1:1 / / rw - tmpfs tmpfs rw\n"
            if layout == "subtree":
                return text + f"2 1 1:1 /hidden/child {destination} rw - tmpfs tmpfs rw\n"
            return text + f"2 1 2:1 / {destination}/nested rw - tmpfs tmpfs rw\n"
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", mountinfo)
    monkeypatch.setattr(
        locking, "_destination_mount_id", lambda path: 2 if layout == "subtree" else 1
    )
    with (
        pytest.raises(DestinationError, match="mount layout"),
        locking.destination_lock(destination),
    ):
        pytest.fail("unsupported overlap acquired a mutation lock")
    assert workflow.prune_backups(job, dry_run=True).result == "planned"


def test_automatic_cleanup_cannot_remove_new_archive_on_exact_timestamp_tie(job, monkeypatch):
    from datetime import UTC, datetime
    from types import SimpleNamespace

    timestamp = datetime(2026, 10, 26, 0, 0, 0, 123456, tzinfo=UTC)
    monkeypatch.setattr(workflow, "datetime", SimpleNamespace(now=lambda: timestamp))
    identifiers = iter(("f" * 32, "1" * 32))
    monkeypatch.setattr(
        workflow, "uuid", SimpleNamespace(uuid4=lambda: SimpleNamespace(hex=next(identifiers)))
    )
    first = run(job).archive_path
    (job.parent / "source/data").write_bytes(b"changed")
    result = run(job)
    assert result.result == "created" and result.archive_path.is_file()
    assert first.is_file()
    assert "newly created backup" in result.cleanup_errors[0]
    assert json.loads(next(result.archive_path.parent.glob("*.json")).read_text())[
        "created_at_utc"
    ].endswith(".123456Z")
