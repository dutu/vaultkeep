"""Independent destination guards, including filesystems visible through LXC bind mounts."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from vaultkeep.backup import workflow
from vaultkeep.config import JobConfig
from vaultkeep.errors import DestinationError


@pytest.mark.parametrize("marker_file", [None, ".vaultkeep-target"])
@pytest.mark.parametrize("expected_fstype", [None, "cifs", "ext4", "nfs4"])
def test_destination_safety_checks_are_independent_and_precede_writability_probe(
    tmp_path: Path,
    valid_config: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    marker_file: str | None,
    expected_fstype: str | None,
) -> None:
    root = tmp_path / "app with spaces"
    root.mkdir()
    if marker_file is not None:
        (root / marker_file).touch()
    valid_config["destination"].update(
        root=str(root), marker_file=marker_file, expected_fstype=expected_fstype
    )
    calls = []

    def inspect(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        assert expected_fstype is not None, "Unconfigured filesystem checks must be skipped"
        assert command == [
            "findmnt",
            "--kernel",
            "--uniq",
            "--noheadings",
            "--raw",
            "--output",
            "FSTYPE",
            "--target",
            str(root),
        ]
        calls.append("filesystem")
        return subprocess.CompletedProcess(command, 0, expected_fstype + "\n", "")

    original_is_file = Path.is_file

    def check_marker(path: Path) -> bool:
        if path == root / (marker_file or ""):
            calls.append("marker")
        return original_is_file(path)

    def probe(path: Path) -> None:
        assert path == root
        calls.append("probe")

    monkeypatch.setattr(workflow.subprocess, "run", inspect)
    monkeypatch.setattr(Path, "is_file", check_marker)
    monkeypatch.setattr(workflow, "probe_destination_writability", probe)
    workflow._validate_runtime(
        JobConfig.model_validate(valid_config),
        require_sources=False,
        require_writable_destination=True,
    )
    expected_calls = ["filesystem"] if expected_fstype is not None else []
    if marker_file is not None:
        expected_calls.append("marker")
    assert calls == [*expected_calls, "probe"]


@pytest.mark.parametrize("marker_kind", ["missing", "directory", "broken_symlink"])
@pytest.mark.parametrize("expected_fstype", [None, "cifs"])
def test_marker_must_exist_as_regular_file_before_destination_writes(
    tmp_path: Path,
    valid_config: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    marker_kind: str,
    expected_fstype: str | None,
) -> None:
    marker = tmp_path / "marker"
    if marker_kind == "directory":
        marker.mkdir()
    elif marker_kind == "broken_symlink":
        marker.symlink_to(tmp_path / "missing")
    valid_config["destination"].update(
        root=str(tmp_path), marker_file="marker", expected_fstype=expected_fstype
    )

    def unexpected_probe(path: Path) -> None:
        pytest.fail("Invalid marker must prevent destination writes")

    monkeypatch.setattr(
        workflow.subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(command, 0, "cifs\n", ""),
    )
    monkeypatch.setattr(workflow, "probe_destination_writability", unexpected_probe)
    with pytest.raises(DestinationError, match="marker is missing"):
        workflow._validate_runtime(
            JobConfig.model_validate(valid_config),
            require_sources=False,
            require_writable_destination=True,
        )
    if marker_kind == "missing":
        assert not marker.exists()


def test_inaccessible_marker_prevents_destination_writes(
    tmp_path: Path, valid_config: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    valid_config["destination"].update(root=str(tmp_path), marker_file="marker")

    def inaccessible_marker(path: Path) -> bool:
        raise PermissionError("Marker cannot be inspected")

    def unexpected_probe(path: Path) -> None:
        pytest.fail("Inaccessible marker must prevent destination writes")

    monkeypatch.setattr(Path, "is_file", inaccessible_marker)
    monkeypatch.setattr(workflow, "probe_destination_writability", unexpected_probe)
    with pytest.raises(PermissionError, match="Marker cannot be inspected"):
        workflow._validate_runtime(
            JobConfig.model_validate(valid_config),
            require_sources=False,
            require_writable_destination=True,
        )


@pytest.mark.parametrize("actual_fstype", ["ext4", "zfs", "overlay"])
def test_local_bind_mount_rejected_even_with_present_marker(
    tmp_path: Path,
    valid_config: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    actual_fstype: str,
) -> None:
    (tmp_path / ".vaultkeep-target").touch()
    valid_config["destination"].update(
        root=str(tmp_path), expected_fstype="cifs", marker_file=".vaultkeep-target"
    )

    def inspect(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        assert command[-2:] == ["--target", str(tmp_path)]
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


@pytest.mark.parametrize(
    ("returncode", "stdout"), [(1, ""), (2, "cifs\n"), (0, ""), (0, "ext4\ncifs\n")]
)
def test_filesystem_inspection_rejects_failed_missing_or_ambiguous_results_before_writes(
    tmp_path: Path,
    valid_config: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    returncode: int,
    stdout: str,
) -> None:
    valid_config["destination"].update(root=str(tmp_path), expected_fstype="cifs")

    def unexpected_probe(path: Path) -> None:
        pytest.fail("Failed filesystem inspection must prevent destination writes")

    monkeypatch.setattr(
        workflow.subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(command, returncode, stdout, ""),
    )
    monkeypatch.setattr(workflow, "probe_destination_writability", unexpected_probe)
    with pytest.raises(DestinationError, match="Cannot inspect destination filesystem"):
        workflow._validate_runtime(
            JobConfig.model_validate(valid_config),
            require_sources=False,
            require_writable_destination=True,
        )


@pytest.mark.parametrize(
    "error",
    [
        FileNotFoundError("findmnt missing"),
        PermissionError("denied"),
        subprocess.TimeoutExpired("findmnt", 10),
        UnicodeError("invalid output encoding"),
    ],
)
def test_filesystem_inspection_errors_prevent_destination_writes(
    tmp_path: Path,
    valid_config: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
) -> None:
    valid_config["destination"].update(root=str(tmp_path), expected_fstype="cifs")

    def fail_inspection(*args: Any, **kwargs: Any) -> Any:
        raise error

    def unexpected_probe(path: Path) -> None:
        pytest.fail("Failed filesystem inspection must prevent destination writes")

    monkeypatch.setattr(workflow.subprocess, "run", fail_inspection)
    monkeypatch.setattr(workflow, "probe_destination_writability", unexpected_probe)
    with pytest.raises(DestinationError, match="Cannot inspect destination filesystem"):
        workflow._validate_runtime(
            JobConfig.model_validate(valid_config),
            require_sources=False,
            require_writable_destination=True,
        )


def test_real_findmnt_identifies_filesystems_for_root_and_ordinary_directory(
    tmp_path: Path,
) -> None:
    assert workflow._mounted_filesystem_type(Path("/"))
    assert workflow._mounted_filesystem_type(tmp_path)


@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("visible_fstype", ["cifs", "ext4"])
def test_findmnt_uses_visible_bind_mount_when_filesystems_are_stacked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    nested: bool,
    visible_fstype: str,
) -> None:
    root = tmp_path / "backups"
    root.mkdir()
    destination = root / "app" if nested else root
    destination.mkdir(exist_ok=True)
    mountinfo = tmp_path / "mountinfo"
    hidden_fstype = "ext4" if visible_fstype == "cifs" else "cifs"
    mountinfo.write_text(
        "1 0 0:1 / / rw - ext4 /dev/root rw\n"
        f"2 1 0:2 / {root} rw - {hidden_fstype} hidden rw\n"
        f"3 2 0:3 /share/subdirectory {root} rw - {visible_fstype} visible rw\n"
    )
    run = subprocess.run

    def inspect_fixture(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return run([*command, "--tab-file", str(mountinfo)], **kwargs)

    monkeypatch.setattr(workflow.subprocess, "run", inspect_fixture)
    assert workflow._mounted_filesystem_type(destination) == visible_fstype
