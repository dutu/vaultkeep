"""Opt-in release gates for Debian/systemd and mounted-network validation."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

RELEASE_GATE_ENABLED = os.environ.get("VAULTKEEP_RELEASE_GATE") == "1"
ROOT = Path(__file__).resolve().parents[2]

pytestmark = [
    pytest.mark.release_gate,
    pytest.mark.skipif(
        not RELEASE_GATE_ENABLED,
        reason="set VAULTKEEP_RELEASE_GATE=1 on a prepared Debian release host",
    ),
]


def _run(command: list[str]) -> str:
    result = subprocess.run(
        command,
        text=True,
        capture_output=True,
        check=True,
    )
    return result.stdout.strip()


def _os_release() -> dict[str, str]:
    values: dict[str, str] = {}
    for line in Path("/etc/os-release").read_text(encoding="utf-8").splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", maxsplit=1)
        values[key] = value.strip('"')
    return values


def _mount_fstype(path: str) -> str:
    return _run(["findmnt", "--noheadings", "--target", path, "--output", "FSTYPE"])


def test_release_host_is_supported_debian_systemd_root() -> None:
    assert sys.platform == "linux"
    assert os.geteuid() == 0
    release = _os_release()
    assert release.get("ID") == "debian"
    assert release.get("VERSION_CODENAME") in {"bookworm", "trixie"}
    assert Path("/run/systemd/system").is_dir()
    systemd_version = _run(["systemctl", "--version"]).splitlines()[0].split()[1]
    assert int(systemd_version) >= 247
    for command in ("python3", "tar", "zstd", "7z", "findmnt", "rsync", "systemctl"):
        assert shutil.which(command) is not None


def test_release_host_has_declared_cifs_and_nfs_mounts() -> None:
    cifs_mount = os.environ.get("VAULTKEEP_RELEASE_CIFS_MOUNT")
    nfs_mount = os.environ.get("VAULTKEEP_RELEASE_NFS_MOUNT")
    if cifs_mount is None or nfs_mount is None:
        pytest.fail("set VAULTKEEP_RELEASE_CIFS_MOUNT and VAULTKEEP_RELEASE_NFS_MOUNT")

    assert _mount_fstype(cifs_mount) == "cifs"
    assert _mount_fstype(nfs_mount) in {"nfs", "nfs4"}


def test_release_installer_dry_runs_in_clean_modes() -> None:
    bash = shutil.which("bash")
    if bash is None:
        pytest.fail("bash is required")

    subprocess.run([bash, "install.sh", "install", "--dry-run"], cwd=ROOT, check=True)
    update = subprocess.run(
        [bash, "install.sh", "update", "--dry-run"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert update.returncode == 0 or "update requires an existing installation manifest" in (
        update.stdout + update.stderr
    )
