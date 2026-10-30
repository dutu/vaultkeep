"""Read-only installer checks and schema-specific ownership metadata (stdlib only).

Deployment mutations and rollback stay in install.sh. This file is also executed
with the candidate interpreter to check its actual installed package inventory.
"""

from __future__ import annotations

import fcntl
import hashlib
import importlib
import importlib.metadata
import json
import os
import pwd
import re
import shlex
import stat
import subprocess
import sys
import traceback
from pathlib import Path

VERSION = re.compile(r"\d+(?:\.\d+)*(?:\.dev\d+)?")


def fail(message: str) -> None:
    raise ValueError(message)


def issue(resource: str, reason: str) -> str:
    """Keep resource and cause on separate, printable console lines."""

    def line(value: str) -> str:
        return "".join(char if char.isprintable() else " " for char in value).strip()

    return f"Issue: {line(resource)}\n       {line(reason)}"


def unit_resource(command: list[str], unit: str) -> str:
    account = command[command.index("-u") + 1] if "runuser" in command else "root"
    return f"User '{account}', unit '{unit}'"


def exception_summary(error: Exception) -> str:
    """Summarize operational failures without exposing Python implementation details."""
    if isinstance(error, subprocess.TimeoutExpired):
        return issue("Installer command", f"Timed out after {error.timeout}s; see diagnostics log.")
    if isinstance(error, OSError):
        resource = str(error.filename) if error.filename else "Installer filesystem operation"
        reason = (
            "Required resource does not exist."
            if isinstance(error, FileNotFoundError)
            else "Permission denied; check access for the affected account."
            if isinstance(error, PermissionError)
            else "Filesystem operation failed: "
            f"{error.strerror or str(error) or 'see diagnostics log'}."
        )
        return issue(resource, reason)
    if isinstance(error, json.JSONDecodeError):
        return issue("Installer metadata", "Invalid JSON data; see diagnostics log.")
    if isinstance(error, ValueError):
        return str(error)
    return issue("Installer", "Internal failure; see diagnostics log for technical details.")


def command_failure(argv: list[str], result: subprocess.CompletedProcess[str]) -> str:
    """Use known tool diagnostics; never copy a Python traceback to the console."""
    resource = f"Command '{Path(argv[0]).name}'"
    if "systemctl" in argv:
        offset = argv.index("systemctl") + 1
        operation = next((part for part in argv[offset:] if not part.startswith("-")), "query")
        resource = f"Systemd operation '{operation}'"
        if "runuser" in argv:
            resource += f" for user '{argv[argv.index('-u') + 1]}'"
        if operation in ("show", "start", "stop", "restart", "enable", "disable"):
            units = [part for part in argv[offset:] if part.endswith((".service", ".timer"))]
            if units:
                resource += f", unit '{units[0]}'"
    diagnostic = result.stderr.strip() or result.stdout.strip()
    explicit = [line for line in diagnostic.splitlines() if line.startswith(("ERROR:", "E:"))]
    missing = re.search(r"ModuleNotFoundError: No module named '([^'\n]+)'", diagnostic)
    if missing:
        reason = f"Required Python module '{missing[1]}' is missing; check the installation source."
    elif (
        diagnostic
        and not re.search(r"Traceback|\b\w*(?:Error|Exception):", diagnostic)
        and (explicit or len(diagnostic.splitlines()) == 1)
    ):
        reason = re.sub(
            r"^(?:(?:ERROR:|E:)\s*(?:installer(?: jobs)?:\s*)?)+",
            "",
            explicit[-1] if explicit else diagnostic,
        )
    else:
        reason = (
            "Could not create the Python virtual environment; see diagnostics log for tool output."
            if argv[1:3] == ["-m", "venv"]
            else "Command could not complete; see diagnostics log for tool output."
        )
    return issue(resource, reason)


def report_error(message: str) -> None:
    prefix = "ERROR:"
    if (
        sys.stderr.isatty()
        and os.environ.get("TERM", "dumb") != "dumb"
        and not os.environ.get("NO_COLOR")
    ):
        prefix = "\033[1;31mERROR:\033[0m"
    compact = message.startswith("Issue: ")
    lines = message.splitlines() or [""]
    for line in lines:
        print(line if compact else f"{prefix} installer: {line}", file=sys.stderr)
    descriptor = os.environ.get("VAULTKEEP_INSTALL_ERROR_FD")
    if descriptor:
        try:
            with os.fdopen(os.dup(int(descriptor)), "w") as output:
                print(message if compact else issue("Installer", message), file=output)
        except (OSError, ValueError):
            # Reporting must not replace the operation's original failure.
            pass


def log_command_result(
    argv: list[str], result: subprocess.CompletedProcess[str] | subprocess.TimeoutExpired | OSError
) -> None:
    """Retain captured tool output without adding it to the error summary."""
    transcript = os.environ.get("VAULTKEEP_INSTALL_LOG")
    if not transcript:
        return
    try:
        with open(transcript, "a") as output:
            status = (
                result.returncode
                if isinstance(result, subprocess.CompletedProcess)
                else f"timeout ({result.timeout}s)"
                if isinstance(result, subprocess.TimeoutExpired)
                else str(result)
            )
            output.write(f"Command: {shlex.join(argv)}\nExit code: {status}\n")
            for label in ("stdout", "stderr"):
                content = getattr(result, label, None)
                if isinstance(content, bytes):
                    content = content.decode(errors="replace")
                output.write(f"{label}:\n{content or ''}\n")
    except OSError:
        pass


def capture_command(
    argv: list[str], *, timeout: int | None, input: str | None = None
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            argv, input=input, capture_output=True, text=True, timeout=timeout, check=False
        )
    except (subprocess.TimeoutExpired, OSError) as error:
        log_command_result(argv, error)
        raise
    log_command_result(argv, result)
    return result


def path_env(name: str, default: str) -> Path:
    value = os.environ.get(name, default)
    path = Path(value)
    if (
        not path.is_absolute()
        or str(path) != value
        or ".." in path.parts
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        fail(f"installer path must be absolute and normalized: {name}")
    if name == "VAULTKEEP_BIN_LINK" and any(char in value for char in "'\"\\"):
        fail("systemd executable path must not contain quotes or backslashes")
    return path


class Layout:
    def __init__(self) -> None:
        self.prefix = path_env("VAULTKEEP_INSTALL_PREFIX", "/opt/vaultkeep")
        self.config = path_env("VAULTKEEP_CONFIG_ROOT", "/etc/vaultkeep")
        self.var = path_env("VAULTKEEP_VAR_ROOT", "/var/lib/vaultkeep")
        self.bin = path_env("VAULTKEEP_BIN_LINK", "/usr/local/bin/vaultkeep")
        self.trigger_bin = path_env(
            "VAULTKEEP_TRIGGER_BIN_LINK", "/usr/local/bin/vaultkeep-trigger"
        )
        system = path_env("VAULTKEEP_SYSTEMD_ROOT", "/etc/systemd/system")
        user = path_env("VAULTKEEP_USER_SYSTEMD_ROOT", "/etc/systemd/user")
        self.service = system / "vaultkeep@.service"
        self.timer = system / "vaultkeep@.timer"
        self.monitor = user / "vaultkeep-trigger.service"
        self.worker = user / "vaultkeep-trigger-exec@.service"
        self.registry = self.var / "systemd-instances.json"
        self.current = self.prefix / "current"
        self.manifest = self.prefix / "install-manifest.json"
        self.releases = self.prefix / "releases"
        roots = [
            self.prefix,
            self.config,
            self.var,
            system,
            user,
            self.bin.parent,
            self.trigger_bin.parent,
        ]
        for root in roots:
            if root == Path("/") or root.is_symlink() or root.resolve() != root:
                fail(f"installer root or ancestor is a symlink/unsafe path: {root}")
        for left in (self.prefix, self.config, self.var):
            for right in roots:
                if left != right and (left in right.parents or right in left.parents):
                    fail(f"overlapping installer roots: {left}, {right}")

    def links(self, components: list[str], schema: int = 3) -> dict[Path, str]:
        links = {self.bin: str(self.current / "venv/bin/vaultkeep")}
        if schema < 3 and "trigger" in components:
            links[self.trigger_bin] = str(self.current / "venv/bin/vaultkeep-trigger")
        return links

    def units(self, components: list[str], schema: int = 3) -> dict[Path, str]:
        units = {self.service: "systemd/vaultkeep@.service", self.timer: "systemd/vaultkeep@.timer"}
        if "trigger" in components:
            units.update(
                {
                    self.worker: "systemd/vaultkeep-trigger-exec@.service",
                }
            )
        if schema < 3 and "trigger" in components:
            units[self.monitor] = "components/trigger/systemd/vaultkeep-trigger.service"
        return units


def selection(value: object) -> list[str]:
    if value not in (["core"], ["core", "trigger"]):
        fail("invalid components: expected ['core'] or ['core', 'trigger']")
    return value


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_manifest(layout: Layout) -> dict:
    if layout.manifest.is_symlink():
        fail(f"manifest must not be a symlink: {layout.manifest}")
    data = json.loads(layout.manifest.read_text())
    required = {
        "schema_version",
        "version",
        "source_digest",
        "prefix",
        "active_release",
        "retained_release",
        "current_symlink",
        "bin_symlink",
        "config_root",
        "var_root",
        "timer_registry",
        "owned_artifacts",
    }
    schema = data.get("schema_version")
    if type(schema) is not int or schema not in (1, 2, 3):
        fail("unsupported install manifest schema")
    if schema == 2:
        required.add("components")
    if set(data) != required:
        fail("install manifest has unexpected fields")
    components = (
        selection(data["components"])
        if schema == 2
        else ["core", "trigger"]
        if schema == 3
        else ["core"]
    )
    version = data["version"]
    if not isinstance(version, str) or VERSION.fullmatch(version) is None:
        fail("invalid manifest version")
    if not isinstance(data["source_digest"], str) or not re.fullmatch(
        r"[0-9a-f]{64}", data["source_digest"]
    ):
        fail("invalid manifest source digest")
    expected = {
        "prefix": layout.prefix,
        "current_symlink": layout.current,
        "bin_symlink": layout.bin,
        "config_root": layout.config,
        "var_root": layout.var,
        "timer_registry": layout.registry,
    }
    for key, path in expected.items():
        if data[key] != str(path):
            fail(f"manifest {key} does not match installer path: {path}")
    suffix = "-with-trigger" if schema == 2 and "trigger" in components else ""
    active = layout.releases / (version + suffix)
    if data["active_release"] != str(active):
        fail("invalid active release path")
    retained = data["retained_release"]
    if not isinstance(retained, str):
        fail("invalid retained release path")
    if retained:
        if not isinstance(retained, str):
            fail("invalid retained release path")
        path = Path(retained)
        if (
            path.parent != layout.releases
            or not re.fullmatch(r"\d+(?:\.\d+)*(?:\.dev\d+)?(?:-with-trigger)?", path.name)
            or path == active
            or path.is_symlink()
        ):
            fail("invalid retained release path")
    if layout.releases.is_symlink() or active.is_symlink():
        fail("release directory must not be a symlink")
    allowed = {
        layout.prefix: "directory",
        active: "directory",
        layout.current: "symlink",
        layout.registry: "file",
    }
    allowed.update(dict.fromkeys(layout.links(components, schema), "symlink"))
    allowed.update(dict.fromkeys(layout.units(components, schema), "file"))
    records = data["owned_artifacts"]
    if not isinstance(records, list):
        fail("invalid owned_artifacts")
    found = set()
    for record in records:
        if not isinstance(record, dict) or not isinstance(record.get("path"), str):
            fail("invalid artifact record")
        path = Path(record["path"])
        if str(path) != record["path"] or path not in allowed or path in found:
            fail(f"manifest path outside installer allowlist or duplicate: {path}")
        found.add(path)
        if record.get("type") != allowed[path]:
            fail(f"invalid artifact type: {path}")
        if record["type"] == "symlink":
            target = (
                "releases/" + active.name
                if path == layout.current
                else layout.links(components, schema)[path]
            )
            if set(record) != {"path", "type", "target"} or record["target"] != target:
                fail(f"invalid artifact target: {path}")
        else:
            if set(record) != {"path", "type", "mode", "sha256"}:
                fail(f"invalid artifact fields: {path}")
            if not re.fullmatch(r"0o[0-7]{3,4}", str(record["mode"])):
                fail(f"invalid artifact mode: {path}")
            checksum = record["sha256"]
            if (
                not isinstance(checksum, str)
                or (record["type"] == "file" and not re.fullmatch(r"[0-9a-f]{64}", checksum))
                or (record["type"] == "directory" and checksum != "")
            ):
                fail(f"invalid artifact digest: {path}")
    if found != set(allowed):
        fail("manifest is missing owned artifacts")
    data["components"] = components
    return data


def check_ownership(
    layout: Layout, source: Path, components: list[str], *, staging: bool = False
) -> None:
    if not staging and layout.prefix.exists():
        scratch = [*layout.prefix.glob(".transaction.*"), *layout.prefix.glob(".maintenance.*")]
        stage_root = layout.prefix / ".staging"
        if stage_root.is_symlink():
            fail(f"staging root must not be a symlink: {stage_root}")
        if stage_root.is_dir():
            scratch.extend(stage_root.iterdir())
        if scratch:
            fail(f"incomplete transaction requires inspection: {scratch[0]}")
    data = read_manifest(layout) if layout.manifest.exists() else None
    old = {Path(r["path"]): r for r in data["owned_artifacts"]} if data else {}
    paths = (
        {layout.current: "releases/" + Path(data["active_release"]).name}
        if data
        else {layout.current: ""}
    )
    paths.update(layout.links(components))
    if data:
        paths.update(layout.links(data["components"], data["schema_version"]))
    for path, target in paths.items():
        if not os.path.lexists(path):
            continue
        if path not in old or not path.is_symlink() or os.readlink(path) != target:
            fail(f"unrelated or modified command/current link: {path}")
    units = layout.units(components)
    if data:
        units.update(layout.units(data["components"], data["schema_version"]))
    for path in units:
        if path.with_name(path.name + ".d").exists():
            fail(f"unit drop-in requires operator review: {path}.d")
        if not os.path.lexists(path):
            continue
        if path not in old or path.is_symlink() or not path.is_file():
            fail(f"unrelated or masked unit: {path}")
        if digest(path) != old[path]["sha256"]:
            fail(f"locally modified managed template: {path}")
    if not data and layout.prefix.exists() and not staging:
        # Never adopt an unmanaged release tree left by another installation.
        for name in ("current", "releases", ".staging"):
            path = layout.prefix / name
            if path.is_dir() and not path.is_symlink() and not any(path.iterdir()):
                continue
            if os.path.lexists(path):
                fail(f"unmanaged deployment requires inspection: {path}")
    if data:
        release = Path(data["active_release"])
        identity = release / "deployment.json"
        if identity.is_symlink():
            fail(f"deployment metadata is a symlink: {identity}")
        if identity.exists():
            metadata = json.loads(identity.read_text())
            expected_schema = data["schema_version"]
            if (
                metadata.get("schema_version") != expected_schema
                or metadata.get("version") != data["version"]
                or metadata.get("source_digest") != data["source_digest"]
                or (expected_schema == 2 and metadata.get("components") != data["components"])
            ):
                fail(f"deployment metadata disagrees with manifest: {identity}")
            if "layout" in metadata and metadata["layout"] != {
                "VAULTKEEP_INSTALL_PREFIX": str(layout.prefix),
                "VAULTKEEP_CONFIG_ROOT": str(layout.config),
                "VAULTKEEP_VAR_ROOT": str(layout.var),
                "VAULTKEEP_BIN_LINK": str(layout.bin),
                "VAULTKEEP_SYSTEMD_ROOT": str(layout.service.parent),
                "VAULTKEEP_USER_SYSTEMD_ROOT": str(layout.worker.parent),
            }:
                fail(f"deployment layout disagrees with manifest: {identity}")


def needs_reconciliation(layout: Layout, components: list[str]) -> bool:
    return any(
        not os.path.lexists(p)
        for p in [layout.current, *layout.links(components), *layout.units(components)]
    )


def record(path: Path) -> dict:
    if path.is_symlink():
        return {"path": str(path), "type": "symlink", "target": os.readlink(path)}
    return {
        "path": str(path),
        "type": "file" if path.is_file() else "directory",
        "mode": oct(stat.S_IMODE(path.stat().st_mode)),
        "sha256": digest(path) if path.is_file() else "",
    }


def write_manifest(
    layout: Layout, version: str, checksum: str, retained: str, components: list[str]
) -> None:
    release = layout.releases / version
    data = {
        "schema_version": 3,
        "version": version,
        "source_digest": checksum,
        "prefix": str(layout.prefix),
        "active_release": str(release),
        "retained_release": retained,
        "current_symlink": str(layout.current),
        "bin_symlink": str(layout.bin),
        "config_root": str(layout.config),
        "var_root": str(layout.var),
        "timer_registry": str(layout.registry),
        "owned_artifacts": [
            record(p)
            for p in [
                layout.prefix,
                release,
                layout.current,
                *layout.links(components),
                *layout.units(components),
                layout.registry,
            ]
        ],
    }
    temporary = layout.manifest.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    temporary.chmod(0o600)
    os.replace(temporary, layout.manifest)


def execute(argv: list[str], *, empty_status: int | None = None) -> str:
    result = capture_command(argv, timeout=20)
    if result.returncode and not (
        result.returncode == empty_status
        and not result.stdout.strip()
        and not result.stderr.strip()
    ):
        fail(command_failure(argv, result))
    return result.stdout


def verify_environment(release: Path, version: str, components: list[str], source: Path) -> None:
    if Path(sys.prefix) != release / "venv":
        fail("candidate interpreter has incorrect virtual environment prefix")
    lock = source / "requirements.lock"
    expected = {
        normalize(n): v
        for n, v in re.findall(r"^([\w.-]+)==([^\s\\]+)", lock.read_text(), re.MULTILINE)
    }
    expected["vaultkeep"] = version
    installed = {
        normalize(d.metadata["Name"]): d.version for d in importlib.metadata.distributions()
    }
    if any(installed.get(name) != pin for name, pin in expected.items()):
        fail("installed distribution/dependency versions do not match selected lock")
    if set(installed) - set(expected) - {"pip", "setuptools"}:
        fail("unexpected distributions in application environment")
    for module in ("vaultkeep", "pydantic", "pathspec", "ruamel.yaml"):
        importlib.import_module(module)
    commands = ["vaultkeep"]
    importlib.import_module("vaultkeep.trigger")
    importlib.import_module("psutil")
    if (release / "venv/bin/vaultkeep-trigger").exists():
        fail("obsolete standalone Trigger entry point in application environment")
    execute([sys.executable, "-m", "pip", "check"])
    for command in commands:
        executable = release / "venv/bin" / command
        # pip uses a shell trampoline when the interpreter path contains spaces.
        header = "\n".join(executable.read_text().splitlines()[:4])
        if str(release / "venv/bin/python") not in header:
            fail(f"incorrect console script interpreter: {executable}")
        if execute([str(executable), "--version"]).strip() != version:
            fail(f"incorrect command version: {executable}")
    for example in (source / "examples").glob("*.yaml"):
        execute(
            [
                str(release / "venv/bin/vaultkeep"),
                "--config",
                str(example),
                "validate",
                "--schema-only",
            ]
        )
    execute([str(release / "venv/bin/vaultkeep"), "trigger", "--help"])


def normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def user_managers() -> list[tuple[str, int]]:
    output = execute(
        [
            "systemctl",
            "list-units",
            "--all",
            "--plain",
            "--no-legend",
            "--no-pager",
            "user@*.service",
        ]
    )
    managers = []
    for line in output.splitlines():
        fields = line.split()
        if len(fields) < 4 or not re.fullmatch(r"user@\d+\.service", fields[0]):
            fail(
                issue(
                    "Systemd user managers", f"Cannot identify unit and state in response: {line}"
                )
            )
        if fields[2] in ("inactive", "failed"):
            continue
        if fields[2] != "active":
            fail(issue(fields[0], f"Unexpected systemd state: {fields[2]}/{fields[3]}."))
        uid = int(fields[0][5:-8])
        account = pwd.getpwuid(uid)
        runtime = Path(f"/run/user/{uid}")
        if runtime.is_symlink() or not runtime.is_dir() or runtime.stat().st_uid != uid:
            fail(f"unusable runtime directory for account {account.pw_name}")
        managers.append((account.pw_name, uid))
    return managers


def manager_command(account: str, uid: int) -> list[str]:
    return [
        "runuser",
        "-u",
        account,
        "--",
        "env",
        f"XDG_RUNTIME_DIR=/run/user/{uid}",
        f"DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/{uid}/bus",
        "systemctl",
        "--user",
    ]


def properties(command: list[str], unit: str) -> dict[str, str]:
    result = capture_command(
        [
            *command,
            "show",
            unit,
            "--no-pager",
            "--all",
            "--property=LoadState,ActiveState,SubState,FragmentPath,DropInPaths,ExecStart,UnitFileState,Type,Restart,TimeoutStartUSec,UMask,PartOf,BindsTo,MainPID,ControlPID",
        ],
        timeout=20,
    )
    data = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    required = {"LoadState", "ActiveState", "SubState"}
    absent = (
        data.get("LoadState") == "not-found"
        and data.get("ActiveState") == "inactive"
        and data.get("SubState") == "dead"
    )
    if absent:
        # Absent units may not expose the service interface at all. Normalize
        # optional empty fields only after the manager has confirmed absence.
        for name in ("FragmentPath", "DropInPaths", "ExecStart", "UnitFileState"):
            data.setdefault(name, "")
    else:
        required.update(("FragmentPath", "DropInPaths", "UnitFileState"))
        if unit.endswith(".service"):
            required.add("ExecStart")
    # systemctl show can return 1 for an affirmatively absent unit. Errors from
    # loaded units and incomplete responses remain inconclusive.
    if not required <= data.keys() or (
        result.returncode and not (result.returncode == 1 and absent)
    ):
        missing = ", ".join(sorted(required - data.keys()))
        reason = ""
        if result.returncode:
            reason = (
                "Systemd property query failed: "
                + command_failure(command, result).split("\n", 1)[1].strip()
            )
        if missing:
            reason += f" Systemd response is missing required properties: {missing}."
        values = ", ".join(
            f"{key}={data.get(key, '<missing>')}"
            for key in sorted(required & {"LoadState", "ActiveState", "SubState"})
        )
        fail(issue(unit_resource(command, unit), f"{reason} {values}."))
    return data


def check_trigger_command(
    state: dict[str, str], unit: str, executable: Path, label: str, *, legacy: bool = False
) -> None:
    value = state["ExecStart"]
    match = re.fullmatch(r"\{ path=(.+?) ; argv\[\]=(.+?) ; .*\}", value)
    allowed = {str(executable)}
    if not legacy and unit.startswith("vaultkeep-trigger@"):
        # Per-job monitors from the previous renderer used the default entry
        # point even with a custom install layout. Refresh migrates this path.
        allowed.add("/usr/local/bin/vaultkeep")
    if match is None or match[1] not in allowed:
        fail(f"{label}: unexpected ExecStart executable or encoding: {unit}")
    executable = Path(match[1])
    argv = shlex.split(match[2])
    expected = [str(executable)] if legacy else [str(executable), "--user", "trigger"]
    offset = len(expected)
    if unit == "vaultkeep-trigger.service" or unit.startswith("vaultkeep-trigger@"):
        # Accept the former option order while inspecting installed deployments
        # for upgrade/removal. Newly rendered monitors use top-level job options.
        suffix = ["monitor"]
        if not legacy and argv[:offset] != expected:
            expected = [str(executable), "--user"]
            offset = len(expected)
            suffix = ["trigger", "monitor"]
        if not legacy and (len(argv) < offset + 3 or argv[offset] != "--config"):
            fail(f"{label}: unexpected ExecStart (monitor requires its job configuration): {unit}")
        if (
            len(argv) >= offset + 3
            and argv[offset] == "--config"
            and Path(argv[offset + 1]).is_absolute()
        ):
            expected.extend(argv[offset : offset + 2])
            offset += 2
        if not legacy:
            while len(argv) > offset + 2 and argv[offset] == "--param" and "=" in argv[offset + 1]:
                expected.extend(argv[offset : offset + 2])
                offset += 2
        expected.extend(suffix)
        if (
            not legacy
            and unit.startswith("vaultkeep-trigger@")
            and argv[len(expected) : len(expected) + 1] == ["--expected-identity"]
            and len(argv) == len(expected) + 2
            and re.fullmatch(r"[0-9a-f]{16}", argv[-1]) is not None
        ):
            expected.extend(argv[-2:])
    else:
        expected.extend(["exec-worker", "%i" if "@." in unit else unit.split("@", 1)[1][:-8]])
    if argv != expected:
        fail(f"{label}: unexpected ExecStart arguments: {unit}")


def check_manager(
    command: list[str],
    label: str,
    *,
    uninstall: bool,
    trigger_bin: Path,
    validate_trigger: bool = True,
    legacy_trigger: bool = False,
    launchers: set[str] | None = None,
) -> set[int]:
    patterns = [
        "vaultkeep@*.timer",
        "vaultkeep@*.service",
        "vaultkeep-trigger.service",
        "vaultkeep-trigger@*.service",
        "vaultkeep-trigger-exec@*.service",
    ]
    output = execute(
        [*command, "list-units", "--all", "--plain", "--no-legend", "--no-pager", *patterns]
    )
    units = set()
    for line in output.splitlines():
        fields = line.split()
        if len(fields) < 4 or not relevant_unit(fields[0]):
            fail(issue(label, f"Cannot identify unit and state in systemd response: {line}"))
        units.add(fields[0])
    # Disabled/unloaded monitors still need override and uninstall enablement checks.
    if "--user" in command:
        # systemctl show rejects a bare template name. A read-only property query
        # for a named instance resolves the effective template without starting it.
        units.update(
            ["vaultkeep-trigger.service", "vaultkeep-trigger-exec@installer-check.service"]
        )
    monitor_pids: set[int] = set()
    for unit in sorted(units):
        state = properties(command, unit)
        launcher = launchers is not None and unit in launchers
        if not launcher and state["ActiveState"] not in ("inactive", "failed"):
            fail(
                issue(
                    unit_resource(command, unit),
                    f"Systemd state: {state['ActiveState']}/{state['SubState']}; "
                    "protected backup/worker activity cannot be ruled out; "
                    "wait for work to finish.",
                )
            )
        if state["LoadState"] not in ("loaded", "not-found"):
            fail(
                issue(
                    unit_resource(command, unit),
                    f"Unexpected systemd LoadState: {state['LoadState']}.",
                )
            )
        if not launcher and state["SubState"] not in ("dead", "failed", "exited"):
            fail(
                issue(
                    unit_resource(command, unit),
                    f"Unexpected systemd state: {state['ActiveState']}/{state['SubState']}.",
                )
            )
        if (
            uninstall
            and not (state["LoadState"] == "not-found" and state["UnitFileState"] == "")
            and state["UnitFileState"]
            not in (
                "disabled",
                "static",
                "not-found",
                "generated",
            )
        ):
            fail(f"{label}: disable {unit} before uninstall")
        if (
            validate_trigger
            and unit.startswith("vaultkeep-trigger")
            and state["LoadState"] == "loaded"
        ):
            if unit.startswith("vaultkeep-trigger@"):
                fragment = Path(state["FragmentPath"])
                marker = f"# Managed by Vaultkeep: {unit}\n"
                if (
                    not fragment.is_absolute()
                    or fragment.name != unit
                    or not fragment.is_file()
                    or not fragment.read_text().startswith(marker)
                ):
                    fail(f"{label}: migrate/review effective monitor unit: {unit}")
                if state.get("DropInPaths"):
                    fail(f"{label}: monitor overrides require review: {unit}")
            else:
                expected = (
                    Layout().monitor if unit == "vaultkeep-trigger.service" else Layout().worker
                )
                if state["FragmentPath"] != str(expected) or (
                    unit == "vaultkeep-trigger.service" and not legacy_trigger
                ):
                    fail(f"{label}: migrate/review effective unit: {unit}")
            # A monitor config override is supported. A worker always uses its
            # frozen request and must keep the shared command without --config.
            check_trigger_command(state, unit, trigger_bin, label, legacy=legacy_trigger)
            expected_type = "oneshot" if unit.startswith("vaultkeep-trigger-exec@") else "simple"
            expected_restart = "on-failure" if expected_type == "simple" else "no"
            if (
                state.get("Type") != expected_type
                or state.get("Restart") != expected_restart
                or state.get("UMask") != "0077"
            ):
                values = ", ".join(
                    f"{key}={state.get(key, '<missing>')} (expected {expected})"
                    for key, expected in (
                        ("Type", expected_type),
                        ("Restart", expected_restart),
                        ("UMask", "0077"),
                    )
                    if state.get(key) != expected
                )
                fail(
                    issue(
                        unit_resource(command, unit),
                        f"Unexpected Trigger service properties: {values}.",
                    )
                )
            if expected_type == "oneshot" and (
                state.get("TimeoutStartUSec") != "infinity"
                or state.get("PartOf") != ""
                or state.get("BindsTo") != ""
            ):
                values = ", ".join(
                    f"{key}={state.get(key, '<missing>')!r} (expected {expected!r})"
                    for key, expected in (
                        ("TimeoutStartUSec", "infinity"),
                        ("PartOf", ""),
                        ("BindsTo", ""),
                    )
                    if state.get(key) != expected
                )
                fail(
                    issue(
                        unit_resource(command, unit),
                        f"Worker timeout/lifecycle override requires review: {values}.",
                    )
                )
        elif (
            unit.startswith("vaultkeep@")
            and unit.endswith(".service")
            and state["LoadState"] == "loaded"
        ):
            if str(Layout().bin) not in state["ExecStart"]:
                fail(f"{label}: unexpected core service executable: {unit}")
        if launcher and unit.startswith("vaultkeep-trigger@"):
            pair = (state["ActiveState"], state["SubState"])
            if (
                pair
                not in {
                    ("active", "running"),
                    ("activating", "auto-restart"),
                    ("inactive", "dead"),
                    ("failed", "failed"),
                }
                or state.get("ControlPID") != "0"
            ):
                fail(
                    issue(
                        unit_resource(command, unit),
                        f"Cannot establish safe monitor activity in {pair[0]}/{pair[1]} "
                        f"(ControlPID={state.get('ControlPID', '<missing>')}); "
                        "inspect the unit and wait for its transition to finish.",
                    )
                )
            pid = state.get("MainPID", "")
            if not pid.isdecimal() or ((int(pid) > 0) != (pair == ("active", "running"))):
                fail(
                    issue(
                        unit_resource(command, unit),
                        "Cannot identify the monitor process: MainPID is missing or invalid; "
                        "inspect systemctl show before retrying.",
                    )
                )
            if int(pid):
                monitor_pids.add(int(pid))
    jobs = execute([*command, "list-jobs", "--no-legend", "--no-pager"])
    for line in jobs.splitlines():
        fields = line.split()
        if len(fields) < 2:
            fail(issue(label, f"Cannot identify queued job in systemd response: {line}"))
        if relevant_unit(fields[1]) and not (launchers and fields[1] in launchers):
            fail(
                issue(
                    unit_resource(command, fields[1]),
                    "Queued protected work; let the job finish before maintenance.",
                )
            )
    if uninstall:
        files = execute(
            [*command, "list-unit-files", "--no-legend", "--no-pager", *patterns],
            empty_status=1,
        )
        for line in files.splitlines():
            fields = line.split()
            if len(fields) < 2 or fields[1] not in ("disabled", "static", "generated"):
                fail(f"{label}: disable/review schedule enablement before uninstall")

    return monitor_pids


def relevant_unit(name: str) -> bool:
    return bool(
        re.fullmatch(
            r"vaultkeep@.+\.(?:service|timer)|vaultkeep-trigger\.service|"
            r"vaultkeep-trigger@.+\.service|vaultkeep-trigger-exec@.*\.service",
            name,
        )
    )


def check_lock(path: Path, label: str, *, monitor_pids: set[int] | None = None) -> None:
    if not os.path.lexists(path):
        return
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            fail(f"{label}: invalid lock file")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            if monitor_pids:
                # Exempt only the positively identified monitor's lifetime lock.
                # Backup/job locks are never exempted, even for the same PID.
                info = os.fstat(descriptor)
                for line in Path("/proc/locks").read_text().splitlines():
                    fields = line.split()
                    if len(fields) < 8 or fields[1:4] != ["FLOCK", "ADVISORY", "WRITE"]:
                        continue
                    major, minor, inode = fields[5].split(":")
                    if (int(major, 16), int(minor, 16), int(inode)) == (
                        os.major(info.st_dev),
                        os.minor(info.st_dev),
                        info.st_ino,
                    ) and int(fields[4]) in monitor_pids:
                        return
            if monitor_pids is not None:
                fail(
                    issue(
                        str(path),
                        f"User '{label}': held monitor lifetime lock has an "
                        "unidentified owner; inspect the monitor process and /proc/locks "
                        "before retrying.",
                    )
                )
            fail(
                issue(
                    str(path),
                    f"User '{label}': held runtime/job lock; "
                    "wait for protected work to finish before maintenance.",
                )
            )
    finally:
        os.close(descriptor)


def activity(
    layout: Layout,
    include_users: bool,
    uninstall: bool,
    components: list[str],
    *,
    launchers: dict | None = None,
) -> None:
    data = read_manifest(layout) if layout.manifest.exists() else None
    legacy = bool(data and data["schema_version"] < 3 and "trigger" in data["components"])
    executable = layout.trigger_bin if legacy else layout.bin
    specs = (
        launchers["accounts"]
        if launchers is not None
        else [{"account": None, "command": ["systemctl"], "env": {}}]
    )
    if launchers is None and include_users:
        for account, uid in user_managers():
            home = Path(pwd.getpwuid(uid).pw_dir)
            specs.append(
                {
                    "account": account,
                    "uid": uid,
                    "command": manager_command(account, uid),
                    "env": {"HOME": str(home)},
                }
            )
    monitor_pids: set[int] = set()
    for spec in specs:
        account = spec["account"] or "root"
        pids = set()
        if spec["command"] is not None:
            pids = check_manager(
                spec["command"],
                account,
                uninstall=uninstall,
                trigger_bin=executable,
                legacy_trigger=legacy,
                validate_trigger="trigger" in components,
                launchers=set(spec["states"]) if launchers is not None else None,
            )
            monitor_pids.update(pids)
        if spec["account"] is None:
            continue
        env = spec["env"]
        home = Path(env["HOME"])
        runtime = Path(f"/run/user/{spec['uid']}") if spec["uid"] is not None else None
        if runtime is not None:
            check_lock(runtime / "vaultkeep-trigger/monitor.lock", account, monitor_pids=pids)
            for path in (runtime / "vaultkeep-trigger/jobs").glob("*/monitor.lock"):
                check_lock(path, account, monitor_pids=pids)
        roots = {Path(env.get("XDG_CACHE_HOME", home / ".cache")) / "vaultkeep/locks"}
        if runtime is not None:
            roots.add(runtime / "vaultkeep/locks")
        for root in roots:
            for path in root.glob("*.lock"):
                check_lock(path, account)
    for root in (Path("/run/lock/vaultkeep"), layout.var / "locks"):
        for path in root.rglob("*.lock"):
            check_lock(path, "root")
    # Read-only positive evidence for foreground commands outside standard units.
    if include_users:
        for directory in Path("/proc").iterdir():
            if not directory.name.isdigit():
                continue
            try:
                argv = (directory / "cmdline").read_bytes().split(b"\0")
            except FileNotFoundError:
                continue
            except PermissionError:
                fail(
                    issue(
                        f"Process {directory.name}",
                        "Permission denied inspecting foreground backup activity.",
                    )
                )
            words = [os.fsdecode(word) for word in argv if word]
            names = [Path(word).name for word in words[:3]]
            known_monitor = (
                int(directory.name) in monitor_pids
                and "monitor" in words
                and "exec-worker" not in words
                and not any(word in words for word in ("run", "prune", "verify"))
            )
            if known_monitor:
                continue
            if (
                (
                    "vaultkeep-trigger" in names
                    or "vaultkeep_trigger" in names
                    or "vaultkeep" in names
                )
                and ("monitor" in words or "exec-worker" in words)
            ) or ("vaultkeep" in names and any(w in words for w in ("run", "prune", "verify"))):
                operation = next(
                    (word for word in ("run", "prune", "verify", "exec-worker") if word in words),
                    "monitor",
                )
                reason = (
                    "Unidentified foreground monitor; inspect its launcher before maintenance."
                    if operation == "monitor"
                    else f"Vaultkeep {operation} is running; wait for protected work to finish."
                )
                fail(issue(f"Process {directory.name}", reason))


def main() -> None:
    command, *args = sys.argv[1:]
    if command == "run":
        # Shell operations had no deadline; preserve that and their exit status.
        result = capture_command(args, timeout=None)
        if result.returncode:
            report_error(command_failure(args, result))
            raise SystemExit(
                result.returncode if result.returncode > 0 else 128 - result.returncode
            )
        return
    if command == "environment":
        release, version, components, source = args
        verify_environment(Path(release), version, selection(components.split(",")), Path(source))
        return
    layout = Layout()
    if command == "manifest":
        data = read_manifest(layout)
        value = data[args[0]]
        print(",".join(value) if isinstance(value, list) else value)
    elif command == "ownership":
        check_ownership(
            layout, Path(args[0]), selection(args[1].split(",")), staging=len(args) == 3
        )
    elif command == "needs-reconciliation":
        print(int(needs_reconciliation(layout, selection(args[0].split(",")))))
    elif command == "write-manifest":
        version, checksum, retained, components = args
        write_manifest(layout, version, checksum, retained, selection(components.split(",")))
    elif command == "activity":
        activity(layout, args[0] == "1", args[1] == "1", selection(args[2].split(",")))
    elif command == "reload-users":
        # Refresh the inventory; a manager may have appeared during staging.
        errors = []
        for account, uid in user_managers():
            try:
                execute([*manager_command(account, uid), "daemon-reload"])
            except (ValueError, OSError, subprocess.SubprocessError) as error:
                traceback.print_exc(file=sys.stderr)
                summary = exception_summary(error)
                errors.append(
                    summary
                    if summary.startswith("Issue: ")
                    else issue(f"User '{account}', user manager", summary)
                )
        if errors:
            fail("\n".join(errors))
    else:
        fail(f"unknown installer check: {command}")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        if os.environ.get("VAULTKEEP_INSTALL_LOG"):
            traceback.print_exc(file=sys.stderr)
        report_error(exception_summary(error))
        raise SystemExit(1) from None
