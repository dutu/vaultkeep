"""Installed distribution version and source revision access."""

import re
import subprocess
import sys
from importlib.metadata import version
from pathlib import Path

_DISTRIBUTION_NAME = "vaultkeep"


def installed_version() -> str:
    """Return the installed Vaultkeep distribution version."""
    return version(_DISTRIBUTION_NAME)


def source_commit(root: Path) -> str:
    """Read an embedded revision or resolve HEAD in the selected source checkout."""
    marker = root / "src/vaultkeep/_commit.txt"
    if marker.is_file():
        return marker.read_text(encoding="ascii").strip()
    if not (root / ".git").exists():
        return "unknown"
    # The installer runs as root on a user-owned checkout; trust only this selected path.
    try:
        result = subprocess.run(
            ["git", "-c", f"safe.directory={root.resolve()}", "rev-parse", "--verify", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    commit = result.stdout.strip()
    return commit if re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit) else "unknown"


def version_report() -> str:
    """Return the application version and commit, without requiring Git at runtime."""
    marker = Path(__file__).with_name("_commit.txt")
    commit = (
        marker.read_text(encoding="ascii").strip()
        if marker.is_file()
        else source_commit(Path(__file__).resolve().parents[2])
    )
    return f"{installed_version()} (commit {commit})"


if __name__ == "__main__":
    print(source_commit(Path(sys.argv[1])))
