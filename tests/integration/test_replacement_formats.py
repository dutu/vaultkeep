"""Committed replacement transitions exercise both real archive formats and encryption."""

from __future__ import annotations

import json

import pytest

from tests.integration import test_archive_tools as archive_fixtures
from vaultkeep.archive import ArchiveTools, PasswordSecret, verify_tar_7z, verify_tar_zstd
from vaultkeep.archive import passwords as passwords_module
from vaultkeep.backup.workflow import WorkflowPaths, run_backup
from vaultkeep.destination.suspect import mark_backup_suspect

archive_tool_deadline = archive_fixtures.archive_tool_deadline
TOOLS = ArchiveTools()
pytestmark = pytest.mark.skipif(
    not all(tool.is_file() for tool in (TOOLS.tar, TOOLS.zstd, TOOLS.seven_zip)),
    reason="Real archive tools are unavailable",
)


@pytest.mark.parametrize("initial_format", ["tar.zst", "tar.7z"])
def test_committed_replacement_across_format_and_encryption_change(
    tmp_path, valid_config, monkeypatch, initial_format, archive_tool_deadline
):
    source = tmp_path / "source"
    source.mkdir()
    (source / "data").write_bytes(b"same source coverage across format transitions")
    destination = tmp_path / "backups"
    destination.mkdir()
    password_path = tmp_path / "password"
    password_path.write_bytes(b"secret\n")
    password_path.chmod(0o600)
    # Ownership/path policy has separate tests; only relax /tmp's writable ancestor.
    monkeypatch.setattr(
        passwords_module, "_validate_password_path", lambda path, **kw: path.lstat()
    )
    valid_config["sources"][0]["path"] = str(source)
    valid_config["destination"]["root"] = str(destination)
    config = tmp_path / "app.yaml"
    paths = WorkflowPaths(tmp_path / "state", tmp_path / "cache", tmp_path / "locks")

    def select_format(archive_format):
        valid_config["archive"]["format"] = archive_format
        valid_config["encryption"] = (
            {"mode": "password", "password_file": str(password_path)}
            if archive_format == "tar.7z"
            else {"mode": "none"}
        )
        config.write_text(json.dumps(valid_config))

    select_format(initial_format)
    original = run_backup(config, paths=paths, user_mode=True)
    mark_backup_suspect(original.archive_path.parent, "replace archive from earlier format")
    replacement_format = "tar.7z" if initial_format == "tar.zst" else "tar.zst"
    select_format(replacement_format)
    replacement = run_backup(config, paths=paths, user_mode=True)
    assert replacement.result == "created" and replacement.cleanup_errors == ()
    assert replacement.removed == 1 and not original.archive_path.exists()
    assert replacement.archive_path.name.endswith(replacement_format)
    manifest = json.loads(next(replacement.archive_path.parent.glob("*.json")).read_text())
    assert manifest["encrypted"] == (replacement_format == "tar.7z")
    if replacement_format == "tar.7z":
        with PasswordSecret(b"secret") as password:
            verify_tar_7z(replacement.archive_path, job_id="app", password=password, tools=TOOLS)
    else:
        verify_tar_zstd(replacement.archive_path, tools=TOOLS)
    assert run_backup(config, paths=paths, user_mode=True).result == "unchanged"
