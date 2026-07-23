"""Tests for the Debian installer shell entry point."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
INSTALLER = ROOT / "install.sh"


def _bash() -> str:
    shell = shutil.which("bash")
    if shell is None:
        pytest.skip("bash is not available")
    return shell


def _env(tmp_path: Path) -> dict[str, str]:
    environment = os.environ.copy()
    environment.update(
        {
            "VAULTKEEP_INSTALL_TESTING": "1",
            "VAULTKEEP_INSTALL_PREFIX": str(tmp_path / "opt" / "vaultkeep"),
            "VAULTKEEP_CONFIG_ROOT": str(tmp_path / "etc" / "vaultkeep"),
            "VAULTKEEP_VAR_ROOT": str(tmp_path / "var" / "lib" / "vaultkeep"),
            "VAULTKEEP_BIN_LINK": str(tmp_path / "usr" / "local" / "bin" / "vaultkeep"),
            "VAULTKEEP_SYSTEMD_ROOT": str(tmp_path / "etc" / "systemd" / "system"),
        }
    )
    return environment


@pytest.mark.skipif(sys.platform == "win32", reason="installer is a Debian shell script")
def test_installer_has_valid_bash_syntax() -> None:
    subprocess.run([_bash(), "-n", str(INSTALLER)], cwd=ROOT, check=True)


@pytest.mark.skipif(sys.platform == "win32", reason="installer is a Debian shell script")
def test_install_dry_run_reports_plan_without_writing(tmp_path: Path) -> None:
    result = subprocess.run(
        [_bash(), str(INSTALLER), "install", "--dry-run"],
        cwd=ROOT,
        env=_env(tmp_path),
        text=True,
        capture_output=True,
        check=True,
    )

    assert "Vaultkeep install plan" in result.stdout
    assert "PLAN stage release" in result.stdout
    assert "PLAN write" in result.stdout
    assert not (tmp_path / "opt").exists()
    assert not (tmp_path / "etc").exists()
    assert not (tmp_path / "var").exists()


@pytest.mark.skipif(sys.platform == "win32", reason="installer is a Debian shell script")
def test_uninstall_dry_run_uses_manifest_and_preserves_config(tmp_path: Path) -> None:
    prefix = tmp_path / "opt" / "vaultkeep"
    release = prefix / "releases" / "0.1.0.dev0"
    release.mkdir(parents=True)
    manifest = prefix / "install-manifest.json"
    bin_link = tmp_path / "usr" / "local" / "bin" / "vaultkeep"
    service_unit = tmp_path / "etc" / "systemd" / "system" / "vaultkeep@.service"
    timer_unit = tmp_path / "etc" / "systemd" / "system" / "vaultkeep@.timer"
    timer_registry = tmp_path / "var" / "lib" / "vaultkeep" / "systemd-instances.json"
    manifest.write_text(
        json.dumps(
            {
                "active_release": str(release),
                "bin_symlink": str(bin_link),
                "config_root": str(tmp_path / "etc" / "vaultkeep"),
                "current_symlink": str(prefix / "current"),
                "owned_artifacts": [
                    {"path": str(prefix), "type": "directory"},
                    {"path": str(release), "type": "directory"},
                    {
                        "path": str(prefix / "current"),
                        "target": "releases/0.1.0.dev0",
                        "type": "symlink",
                    },
                    {
                        "path": str(bin_link),
                        "target": f"{prefix}/current/venv/bin/vaultkeep",
                        "type": "symlink",
                    },
                    {"path": str(service_unit), "type": "file"},
                    {"path": str(timer_unit), "type": "file"},
                    {"path": str(timer_registry), "type": "file"},
                ],
                "prefix": str(prefix),
                "retained_release": "",
                "schema_version": 1,
                "source_digest": "abc123",
                "timer_registry": str(timer_registry),
                "var_root": str(tmp_path / "var" / "lib" / "vaultkeep"),
                "version": "0.1.0.dev0",
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [_bash(), str(INSTALLER), "uninstall", "--dry-run"],
        cwd=ROOT,
        env=_env(tmp_path),
        text=True,
        capture_output=True,
        check=True,
    )

    assert "Vaultkeep uninstall plan" in result.stdout
    assert f"preserve {tmp_path / 'etc' / 'vaultkeep'}" in result.stdout
    assert release.exists()
