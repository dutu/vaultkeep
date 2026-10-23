"""Destination guards for CIFS shares and local-directory bind mounts in LXC."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from vaultkeep.backup import workflow
from vaultkeep.config import JobConfig
from vaultkeep.errors import DestinationError


@pytest.mark.parametrize("mount_fstype", ["cifs", "ext4"])
def test_checks_actual_destination_filesystem_below_mount(
    tmp_path: Path,
    valid_config: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    mount_fstype: str,
) -> None:
    root = tmp_path / "app with spaces"
    root.mkdir()
    valid_config["destination"].update(
        root=str(root), mount_point=str(tmp_path), expected_fstype="cifs"
    )
    calls = []

    def inspect(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        # Even a valid CIFS mount point may contain a local mount at destination.root.
        fstype = mount_fstype if command[-2] == "--target" else "cifs"
        return subprocess.CompletedProcess(command, 0, fstype + "\n", "")

    def probe(path: Path) -> None:
        assert mount_fstype == "cifs", "Mismatch must prevent destination writes"
        calls.append(["probe", str(path)])

    monkeypatch.setattr(workflow.subprocess, "run", inspect)
    monkeypatch.setattr(workflow, "probe_destination_writability", probe)
    config = JobConfig.model_validate(valid_config)
    if mount_fstype == "cifs":
        workflow._validate_runtime(config, require_sources=False, require_writable_destination=True)
        assert calls[-1] == ["probe", str(root)]
    else:
        with pytest.raises(DestinationError, match="expected cifs, found ext4"):
            workflow._validate_runtime(
                config, require_sources=False, require_writable_destination=True
            )
    assert calls[0][-2:] == ["--mountpoint", str(tmp_path.resolve())]
    assert calls[1][-2:] == ["--target", str(root)]


@pytest.mark.parametrize("actual_fstype", ["ext4", "zfs", "overlay"])
def test_local_bind_mount_rejected_even_with_present_marker(
    tmp_path: Path,
    valid_config: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    actual_fstype: str,
) -> None:
    (tmp_path / ".vaultkeep-target").touch()
    valid_config["destination"].update(
        root=str(tmp_path),
        mount_point=str(tmp_path),
        expected_fstype="cifs",
        marker_file=".vaultkeep-target",
    )

    def inspect(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 0, actual_fstype + "\n", "")

    def unexpected_probe(path: Path) -> None:
        pytest.fail("A local bind mount must never receive destination writes")

    monkeypatch.setattr(workflow.subprocess, "run", inspect)
    monkeypatch.setattr(workflow, "probe_destination_writability", unexpected_probe)
    with pytest.raises(DestinationError, match=f"expected cifs, found {actual_fstype}"):
        workflow._validate_runtime(
            JobConfig.model_validate(valid_config),
            require_sources=False,
            require_writable_destination=True,
        )


def test_expected_fstype_does_not_require_explicit_mount_point(
    tmp_path: Path, valid_config: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    valid_config["destination"].update(root=str(tmp_path), mount_point=None, expected_fstype="cifs")

    def inspect(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        assert command[-2:] == ["--target", str(tmp_path)]
        return subprocess.CompletedProcess(command, 0, "cifs\n", "")

    monkeypatch.setattr(workflow.subprocess, "run", inspect)
    workflow._validate_runtime(
        JobConfig.model_validate(valid_config),
        require_sources=False,
        require_writable_destination=False,
    )


@pytest.mark.parametrize(
    ("returncode", "stdout"), [(1, ""), (2, "cifs\n"), (0, ""), (0, "ext4\ncifs\n")]
)
def test_mount_inspection_rejects_failed_missing_or_ambiguous_results(
    monkeypatch: pytest.MonkeyPatch, returncode: int, stdout: str
) -> None:
    monkeypatch.setattr(
        workflow.subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(command, returncode, stdout, ""),
    )
    with pytest.raises(DestinationError, match="Cannot inspect destination filesystem"):
        workflow._mounted_filesystem_type(Path("/mnt/backups"))


@pytest.mark.parametrize(
    "error",
    [
        FileNotFoundError("findmnt missing"),
        PermissionError("denied"),
        subprocess.TimeoutExpired("findmnt", 10),
    ],
)
def test_mount_inspection_errors_prevent_destination_writes(
    tmp_path: Path,
    valid_config: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
) -> None:
    valid_config["destination"].update(root=str(tmp_path), mount_point=None, expected_fstype="cifs")

    def fail_inspection(*args: Any, **kwargs: Any) -> Any:
        raise error

    def unexpected_probe(path: Path) -> None:
        pytest.fail("Failed mount inspection must prevent destination writes")

    monkeypatch.setattr(workflow.subprocess, "run", fail_inspection)
    monkeypatch.setattr(workflow, "probe_destination_writability", unexpected_probe)
    with pytest.raises(DestinationError, match="Cannot inspect destination filesystem"):
        workflow._validate_runtime(
            JobConfig.model_validate(valid_config),
            require_sources=False,
            require_writable_destination=True,
        )


def test_real_findmnt_accepts_root_mount_and_rejects_ordinary_directory(tmp_path: Path) -> None:
    assert workflow._mounted_filesystem_type(Path("/"), exact_mount_point=True)
    with pytest.raises(DestinationError, match="Configured mount point is not mounted"):
        workflow._mounted_filesystem_type(tmp_path, exact_mount_point=True)
    assert workflow._mounted_filesystem_type(tmp_path)


@pytest.mark.parametrize("exact_mount_point", [False, True])
@pytest.mark.parametrize("visible_fstype", ["cifs", "ext4"])
def test_findmnt_uses_visible_mount_when_filesystems_are_stacked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    exact_mount_point: bool,
    visible_fstype: str,
) -> None:
    root = tmp_path / "backups"
    root.mkdir()
    mountinfo = tmp_path / "mountinfo"
    hidden_fstype = "ext4" if visible_fstype == "cifs" else "cifs"
    mountinfo.write_text(
        "1 0 0:1 / / rw - ext4 /dev/root rw\n"
        f"2 1 0:2 / {root} rw - {hidden_fstype} hidden rw\n"
        f"3 2 0:3 / {root} rw - {visible_fstype} visible rw\n"
    )
    run = subprocess.run

    def inspect_fixture(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return run([*command, "--tab-file", str(mountinfo)], **kwargs)

    monkeypatch.setattr(workflow.subprocess, "run", inspect_fixture)
    assert workflow._mounted_filesystem_type(root, exact_mount_point=exact_mount_point) == (
        visible_fstype
    )
