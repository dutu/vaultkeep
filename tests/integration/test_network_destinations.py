"""Real archives exercise emulated mounts; opt-in tests exercise mounted CIFS/NFS shares."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from tests.integration import test_archive_tools as archive_fixtures
from tests.unit import test_network_locking as network_fixtures
from vaultkeep.archive import ArchiveTools
from vaultkeep.archive import passwords as passwords_module
from vaultkeep.backup import workflow
from vaultkeep.destination.suspect import mark_backup_suspect
from vaultkeep.errors import DestinationError, LockError
from vaultkeep.locking import NETWORK_LOCK_NAME, destination_lock

share = network_fixtures.share
archive_tool_deadline = archive_fixtures.archive_tool_deadline
TOOLS = ArchiveTools()
pytestmark = pytest.mark.skipif(
    not all(tool.is_file() for tool in (TOOLS.tar, TOOLS.zstd, TOOLS.seven_zip)),
    reason="Real archive tools are unavailable",
)


def _job(tmp_path, root, valid_config, backup_mode="combined", archive_format="tar.zst"):
    source = tmp_path / "source"
    source.mkdir()
    (source / "data").write_bytes(b"first version")
    valid_config["sources"][0]["path"] = str(source)
    valid_config["destination"]["root"] = str(root)
    valid_config["backup_mode"] = backup_mode
    valid_config["retention"] = dict(hourly=1, daily=0, weekly=0, monthly=0, yearly=0)
    valid_config["archive"]["format"] = archive_format
    if archive_format == "tar.7z":
        password = tmp_path / "password"
        password.write_bytes(b"test-secret\n")
        password.chmod(0o600)
        valid_config["encryption"] = {"mode": "password", "password_file": str(password)}
    config = tmp_path / "app.yaml"
    config.write_text(json.dumps(valid_config))
    return config, workflow.WorkflowPaths(
        tmp_path / "state", tmp_path / "cache", tmp_path / "locks"
    )


@pytest.mark.parametrize("filesystem", ["cifs", "nfs4"])
@pytest.mark.parametrize("backup_mode", ["combined", "per_file"])
@pytest.mark.parametrize("archive_format", ["tar.zst", "tar.7z"])
def test_network_backup_verify_replacement_and_prune(
    tmp_path,
    valid_config,
    share,
    monkeypatch,
    filesystem,
    backup_mode,
    archive_format,
    archive_tool_deadline,
):
    share.filesystem = filesystem
    if filesystem == "nfs4":
        # Exercise the Linux NFS rejection while keeping real atomic os.rename.
        network_fixtures.reject_native_noreplace(monkeypatch)
    monkeypatch.setattr(
        passwords_module, "_validate_password_path", lambda path, **kw: path.lstat()
    )
    root = share.root / "backups"
    root.mkdir()
    config, paths = _job(tmp_path, root, valid_config, backup_mode, archive_format)

    def run():
        return workflow.run_backup(config, paths=paths, user_mode=True)

    def archive(result):
        return result.archive_path if backup_mode == "combined" else result.files[0].archive_path

    # Validation acquires the same share lock for its rename probe.
    assert workflow.validate_job(config).result == "valid"
    first = run()
    assert first.result == "created" and run().result == "unchanged"
    assert workflow.verify_backups(config, user_mode=True).backups == 1
    original = archive(first)
    mark_backup_suspect(original.parent, "replace network recovery point")
    replacement = run()
    assert replacement.result == "created" and replacement.removed == 1
    assert not original.exists() and archive(replacement).is_file()
    assert replacement.cleanup_errors == ()

    (tmp_path / "source/data").write_bytes(b"new version")
    with monkeypatch.context() as patch:
        patch.setattr(workflow, "execute_prune_plan", lambda *args: ())
        latest = run()
    assert latest.result == "created"
    assert workflow.prune_backups(config, dry_run=True).result == "planned"
    assert workflow.prune_backups(config, dry_run=False).removed == 1
    assert archive(latest).is_file() and not archive(replacement).exists()
    assert workflow.verify_backups(config, user_mode=True).backups == 1

    with destination_lock(root):
        for mutate in (
            run,
            lambda: workflow.verify_backups(config, user_mode=True),
            lambda: workflow.prune_backups(config, dry_run=False),
        ):
            with pytest.raises(LockError):
                mutate()
        assert workflow.prune_backups(config, dry_run=True).result == "planned"
        assert len(workflow.list_backups(config).backups) == 1
    assert (share.root / NETWORK_LOCK_NAME).is_file()


@pytest.mark.parametrize("kind", ["CIFS", "NFS"])
def test_live_network_share_roundtrip(tmp_path, valid_config, kind, archive_tool_deadline):
    value = os.environ.get(f"VAULTKEEP_TEST_{kind}_ROOT")
    if not value:
        pytest.skip(f"set VAULTKEEP_TEST_{kind}_ROOT to a disposable mounted share")
    root = Path(value)
    from vaultkeep.locking import _validate_destination_mounts

    mount = _validate_destination_mounts(root.resolve(strict=True))
    assert mount.filesystem in ({"cifs", "smb3"} if kind == "CIFS" else {"nfs", "nfs4"})
    with TemporaryDirectory(prefix=".vaultkeep-network-test-", dir=root) as directory:
        config, paths = _job(tmp_path, Path(directory), valid_config)
        first = workflow.run_backup(config, paths=paths, user_mode=True)
        assert first.result == "created"
        assert workflow.verify_backups(config, user_mode=True).backups == 1
        with destination_lock(Path(directory)):
            script = """import sys
from pathlib import Path
from vaultkeep.locking import destination_lock
from vaultkeep.errors import LockError
try:
    with destination_lock(Path(sys.argv[1])):
        pass
except LockError:
    sys.exit(23)
"""
            child = subprocess.run(
                [sys.executable, "-c", script, directory],
                capture_output=True,
                timeout=10,
            )
            assert child.returncode == 23, child.stderr
        (tmp_path / "source/data").write_bytes(b"changed")
        second = workflow.run_backup(config, paths=paths, user_mode=True)
        assert second.result == "created" and not first.archive_path.exists()
        assert workflow.prune_backups(config, dry_run=False).removed == 0


def test_share_root_destination_ignores_persistent_lock_in_inventory(
    tmp_path, valid_config, share, archive_tool_deadline
):
    config, paths = _job(tmp_path, share.root, valid_config)
    result = workflow.run_backup(config, paths=paths, user_mode=True)
    lock = share.root / NETWORK_LOCK_NAME
    assert result.result == "created" and lock.is_file()
    inventory = workflow.list_backups(config)
    assert len(inventory.backups) == 1 and inventory.malformed == ()
    assert workflow.verify_backups(config, user_mode=True).backups == 1
    assert workflow.prune_backups(config, dry_run=False).removed == 0
    assert lock.is_file() and result.archive_path.is_file()


@pytest.mark.parametrize("operation", ["run", "verify", "prune", "validate"])
@pytest.mark.parametrize("guard", ["marker", "filesystem"])
def test_network_destination_guards_precede_lock_file_creation(
    tmp_path, valid_config, share, monkeypatch, operation, guard
):
    if guard == "marker":
        valid_config["destination"]["marker_file"] = ".required-marker"
        expected = "marker is missing"
    else:
        valid_config["destination"]["expected_fstype"] = "cifs"
        monkeypatch.setattr(workflow, "_mounted_filesystem_type", lambda path: "ext4")
        expected = "filesystem type mismatch"
    config, paths = _job(tmp_path, share.root, valid_config)
    operations = {
        "run": lambda: workflow.run_backup(config, paths=paths, user_mode=True),
        "verify": lambda: workflow.verify_backups(config, user_mode=True),
        "prune": lambda: workflow.prune_backups(config, dry_run=False),
        "validate": lambda: workflow.validate_job(config),
    }
    with pytest.raises(DestinationError, match=expected):
        operations[operation]()
    assert list(share.root.iterdir()) == []
