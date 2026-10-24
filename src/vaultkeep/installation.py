"""Installed layout and systemd template rendering (stdlib only for the installer)."""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

DEFAULT_PATHS = {
    "VAULTKEEP_INSTALL_PREFIX": "/opt/vaultkeep",
    "VAULTKEEP_CONFIG_ROOT": "/etc/vaultkeep",
    "VAULTKEEP_VAR_ROOT": "/var/lib/vaultkeep",
    "VAULTKEEP_BIN_LINK": "/usr/local/bin/vaultkeep",
    "VAULTKEEP_SYSTEMD_ROOT": "/etc/systemd/system",
    "VAULTKEEP_USER_SYSTEMD_ROOT": "/etc/systemd/user",
}


def installation_path(name: str) -> Path:
    """Use explicit overrides, then this release's layout, then standard locations."""
    value = os.environ.get(name)
    if value is None:
        metadata = Path(sys.prefix).parent / "deployment.json"
        try:
            layout = json.loads(metadata.read_text()).get("layout", {})
        except FileNotFoundError:
            layout = {}
        value = layout.get(name, DEFAULT_PATHS[name])
    path = Path(value)
    if (
        not path.is_absolute()
        or str(path) != value
        or ".." in path.parts
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise ValueError(f"installation path must be absolute and normalized: {name}")
    if name == "VAULTKEEP_BIN_LINK" and any(char in value for char in "'\"\\"):
        raise ValueError("systemd executable path must not contain quotes or backslashes")
    return path


def _escape_systemd(value: str, *, executable: bool = False) -> str:
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("systemd arguments must not contain control characters")
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    # systemd does not expand environment variables in the executable token.
    return escaped if executable else escaped.replace("$", "$$")


def systemd_argument(value: str, *, executable: bool = False) -> str:
    """Quote a literal ExecStart argument without shell or systemd expansion."""
    escaped = _escape_systemd(value, executable=executable)
    return value if re.fullmatch(r"[A-Za-z0-9_./:=,@+-]+", value) else f'"{escaped}"'


def render_templates(source: Path, destination: Path) -> None:
    """Render shared units for the layout selected by install.sh."""
    jobs = str(installation_path("VAULTKEEP_CONFIG_ROOT") / "jobs")
    replacements = {
        "@VAULTKEEP_BIN@": systemd_argument(
            str(installation_path("VAULTKEEP_BIN_LINK")), executable=True
        ),
        "@VAULTKEEP_JOB_CONFIG@": f'"{_escape_systemd(jobs)}/%i.yaml"',
        "@VAULTKEEP_JOB_CONDITION@": jobs.replace("%", "%%") + "/%i.yaml",
    }
    for name in ("vaultkeep@.service", "vaultkeep@.timer", "vaultkeep-trigger-exec@.service"):
        content = (source / name).read_text()
        for key, value in replacements.items():
            content = content.replace(key, value)
        (destination / name).write_text(content)


def main() -> None:
    action, *args = sys.argv[1:]
    if action == "templates":
        source, destination = map(Path, args)
        render_templates(source, destination)
    elif action == "metadata":
        metadata_path, version, digest, source_checkout = args
        path = Path(metadata_path)
        path.write_text(
            json.dumps(
                {
                    "schema_version": 3,
                    "version": version,
                    "source_digest": digest,
                    "source_checkout": source_checkout,
                    "layout": {name: str(installation_path(name)) for name in DEFAULT_PATHS},
                },
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )
        # User timers and monitors need these paths; the ownership manifest is private.
        path.chmod(0o644)
    else:
        raise ValueError(f"unknown installation operation: {action}")


if __name__ == "__main__":
    main()
