"""Tests for version reporting."""

from __future__ import annotations

import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from vaultkeep.cli import main
from vaultkeep.version import installed_version, source_commit, version_report

ROOT = Path(__file__).resolve().parents[2]


def project_version() -> str:
    with (ROOT / "pyproject.toml").open("rb") as file:
        data = tomllib.load(file)
    return str(data["project"]["version"])


def test_version_matches_project_metadata() -> None:
    assert installed_version() == project_version()


def test_user_scope_reports_the_application_version(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--user", "--version"]) == 0
    assert capsys.readouterr().out == f"{version_report()}\n"


def test_version_cli_prints_version_and_commit(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = main(["--version"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.out == f"{version_report()}\n"
    assert captured.err == ""


def test_module_entry_point_prints_version_and_commit() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "vaultkeep", "--version"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert result.stdout == f"{version_report()}\n"
    assert result.stderr == ""


def test_checkout_commit_matches_head() -> None:
    expected = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    assert source_commit(ROOT) == expected
    assert version_report() == f"{installed_version()} (commit {expected})"


def test_source_without_git_reports_unknown(tmp_path: Path) -> None:
    assert source_commit(tmp_path) == "unknown"


def test_embedded_commit_survives_without_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import vaultkeep.version as versions

    package = tmp_path / "src/vaultkeep"
    package.mkdir(parents=True)
    commit = "a" * 40
    (package / "_commit.txt").write_text(commit + "\n")
    monkeypatch.setattr(versions, "__file__", str(package / "version.py"))

    def no_git(*args: object, **kwargs: object) -> None:
        pytest.fail("embedded revisions must not invoke Git")

    monkeypatch.setattr(subprocess, "run", no_git)
    assert source_commit(tmp_path) == commit
    assert version_report() == f"{installed_version()} (commit {commit})"


@pytest.mark.parametrize(
    "error",
    [
        FileNotFoundError("git"),
        subprocess.CalledProcessError(128, "git"),
        subprocess.TimeoutExpired("git", 2),
    ],
)
def test_git_failure_reports_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    (tmp_path / ".git").mkdir()

    def fail_git(*args: object, **kwargs: object) -> None:
        raise error

    monkeypatch.setattr(subprocess, "run", fail_git)
    assert source_commit(tmp_path) == "unknown"
