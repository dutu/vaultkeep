"""One verified systemd user monitor per canonical configuration path."""

from __future__ import annotations

import hashlib
import os
import re
import stat
import subprocess
import tempfile
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path

from vaultkeep.config.identity import JobIdentity
from vaultkeep.installation import installation_path, systemd_argument
from vaultkeep.maintenance import command_selection, require_generated
from vaultkeep.state.identity import canonical_config_path
from vaultkeep.trigger.config import TriggerConfig
from vaultkeep.trigger.errors import BlockedError, ConfigurationError
from vaultkeep.trigger.paths import account_home, monitor_lock_path, validate_scope


def path_scope(path: Path) -> str:
    """Monitor identity only; never use this for backup or recovery namespaces."""
    return hashlib.sha256(os.fsencode(canonical_config_path(path))).hexdigest()[:16]


def service_name(config: TriggerConfig | JobIdentity | Path) -> str:
    source = canonical_config_path(config if isinstance(config, Path) else config.source_path)
    filename = source.name
    for suffix in (".yaml", ".yml"):
        if filename.endswith(suffix):
            filename = filename[: -len(suffix)]
            break
    # Escape UTF-8 bytes using systemd's unit-name escaping. Hyphens are safe
    # inside this opaque instance (it is never interpreted as a filesystem path).
    escaped = "".join(
        chr(byte)
        if byte in b"abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_:-."
        and not (index == 0 and byte == ord("."))
        else f"\\x{byte:02x}"
        for index, byte in enumerate(os.fsencode(filename))
    )
    unit = f"vaultkeep-trigger@{escaped}-{path_scope(source)}.service"
    if len(unit) > 255:
        raise ConfigurationError("configuration filename is too long for a systemd service name")
    return unit


def legacy_service_name(config: TriggerConfig | JobIdentity) -> str:
    assert config.scope is not None
    return f"vaultkeep-trigger@{config.job_id}-{validate_scope(config.scope)}.service"


def units_root() -> Path:
    value = os.environ.get("XDG_CONFIG_HOME", "")
    base = Path(value) if value and Path(value).is_absolute() else account_home() / ".config"
    return base / "systemd/user"


def _systemctl(*arguments: str) -> str:
    try:
        result = subprocess.run(
            ["systemctl", "--user", *arguments],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise BlockedError(f"cannot contact the systemd user manager: {error}") from error
    if result.returncode:
        raise BlockedError(
            f"systemctl --user {' '.join(arguments)} failed: {result.stderr.strip()}"
        )
    return result.stdout


def _quote_systemd_argument(value: str, *, executable: bool = False) -> str:
    """Quote one ExecStart argument with systemd syntax and literal expansions."""
    try:
        return systemd_argument(value, executable=executable)
    except ValueError as error:
        raise ConfigurationError(str(error)) from error


def render_service(config: TriggerConfig | JobIdentity, *, legacy: bool = False) -> str:
    # Always use the stable installed entry point, matching timer services.
    args = [
        str(installation_path("VAULTKEEP_BIN_LINK")),
        "--user",
        "--config",
        str(config.source_path),
    ]
    for key, value in sorted(config.params.items()):
        args.extend(["--param", f"{key}={value}"])
    args.extend(["trigger", "monitor"])
    if not legacy:
        assert config.scope is not None
        args.extend(["--expected-identity", validate_scope(config.scope)])
    # ExecStart is parsed by systemd, not by a shell.
    command = " ".join(
        _quote_systemd_argument(arg, executable=index == 0) for index, arg in enumerate(args)
    )
    unit = legacy_service_name(config) if legacy else service_name(config)
    return (
        f"# Managed by Vaultkeep: {unit}\n"
        + ("" if legacy else f"# Vaultkeep Trigger job: {config.job_id}\n")
        + "[Unit]\n"
        f"Description=Vaultkeep shutdown monitor for {config.job_id}\n\n"
        "[Service]\nType=simple\n"
        f"ExecStart={command}\n"
        "Restart=on-failure\nRestartSec=5\nUMask=0077\n"
        "StandardOutput=journal\nStandardError=journal\n\n"
        "[Install]\nWantedBy=default.target\n"
    )


@dataclass(frozen=True)
class InstalledService:
    unit: str
    identity: JobIdentity
    content: str
    values: dict[str, str]
    legacy: bool


def installed_identity(content: str, unit: str) -> tuple[JobIdentity, bool]:
    """Decode and cross-check persisted selection without opening the YAML."""
    source, params = command_selection(content, monitor=True, user=True)
    source = canonical_config_path(source)
    metadata = re.findall(r"^# Vaultkeep Trigger job: ([A-Za-z0-9_-]{1,128})$", content, re.M)
    if metadata:
        if len(metadata) != 1:
            raise ValueError("duplicate installed execution identity")
        identity = JobIdentity(source, metadata[0], params)
        pin = re.findall(r" --expected-identity ([0-9a-f]{16})$", content, re.M)
        if pin != [identity.scope] or unit != service_name(identity):
            raise ValueError("installed execution identity or path identity is inconsistent")
        return identity, False
    match = re.fullmatch(r"vaultkeep-trigger@([A-Za-z0-9_-]{1,128})-([0-9a-f]{16})\.service", unit)
    if match is None:
        raise ValueError("missing installed execution identity metadata")
    identity = JobIdentity(source, match[1], params)
    if match[2] != identity.scope:
        raise ValueError("legacy execution namespace conflicts with its installed command")
    return identity, True


def _inspect_unit(unit: str, source: Path) -> InstalledService:
    from vaultkeep.errors import TimerError
    from vaultkeep.schedule.status import unit_properties

    destination = units_root() / unit
    try:
        values = unit_properties(unit, user_mode=True)
        info = destination.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
            raise ValueError("monitor unit must be a regular file owned by the current user")
        content = destination.read_text(encoding="utf-8")
        if not content.startswith(f"# Managed by Vaultkeep: {unit}\n"):
            raise ValueError("unmanaged monitor service")
        if destination.with_name(unit + ".d").exists():
            raise ValueError("monitor service has local overrides; review them first")
        identity, legacy = installed_identity(content, unit)
        if identity.source_path != canonical_config_path(source):
            raise ValueError("installed command conflicts with the requested configuration path")
        require_generated(content, render_service(identity, legacy=legacy), variable=("ExecStart",))
        if values.get("FragmentPath") != str(destination) or values.get("DropInPaths") != "":
            raise ValueError("effective monitor unit ownership is unverified; review systemctl cat")
        if values.get("NeedDaemonReload") != "no":
            raise ValueError(
                "monitor unit changed or cannot be verified; review and daemon-reload first"
            )
        if values["LoadState"] != "loaded":
            raise ValueError("monitor unit is not loaded; review systemctl cat")
        return InstalledService(unit, identity, content, values, legacy)
    except (OSError, UnicodeError, ValueError, TimerError) as error:
        raise ConfigurationError(f"Cannot verify managed monitor {unit}: {error}") from error


def find_service(source: Path) -> InstalledService | None:
    """Find all local and manager-known monitors; reject ambiguous installations."""
    from vaultkeep.errors import TimerError
    from vaultkeep.schedule.status import unit_properties

    source = canonical_config_path(source)
    expected = service_name(source)
    if (units_root() / (expected + ".d")).exists():
        raise ConfigurationError(
            f"monitor service has local overrides; review them first: {expected}"
        )
    names = {path.name for path in units_root().glob("vaultkeep-trigger@*.service")}
    for command in ("list-unit-files", "list-units"):
        output = _systemctl(
            command, "--all", "--no-legend", "--no-pager", "vaultkeep-trigger@*.service"
        )
        names.update(
            word
            for line in output.splitlines()
            for word in line.split()[:2]
            if word.startswith("vaultkeep-trigger@") and word.endswith(".service")
        )
    matches = []
    for unit in sorted(names):
        path = units_root() / unit
        if unit != expected:
            try:
                if not path.exists() and not path.is_symlink():
                    values = unit_properties(unit, user_mode=True)
                    if (
                        values.get("LoadState") == "not-found"
                        and values.get("ActiveState") == "inactive"
                        and values.get("SubState") == "dead"
                        and not values.get("FragmentPath")
                    ):
                        continue
                    path = Path(values.get("FragmentPath", ""))
                content = path.read_text(encoding="utf-8")
                selected, _ = command_selection(content, monitor=True, user=True)
            except (OSError, UnicodeError, ValueError, TimerError) as error:
                # An unreadable monitor cannot safely be excluded from this path.
                raise ConfigurationError(
                    f"Cannot identify monitor {unit}; review it first: {error}"
                ) from error
            if canonical_config_path(selected) != source:
                continue
        matches.append(_inspect_unit(unit, source))
    if len(matches) > 1:
        raise ConfigurationError(
            "Multiple Trigger services reference this YAML: "
            + ", ".join(item.unit for item in matches)
            + "; stop and safely resolve the conflicting installations explicitly"
        )
    return matches[0] if matches else None


def inspect_service(config: TriggerConfig | JobIdentity) -> dict[str, str]:
    installed = find_service(config.source_path)
    return installed.values if installed else {"LoadState": "not-found"}


def require_monitor_identity(config: TriggerConfig, expected: str | None = None) -> None:
    """A restart or direct invocation must not adopt an unreviewed execution identity."""
    if expected is not None and expected != config.scope:
        raise BlockedError(
            "Trigger execution identity changed; run trigger update before restarting"
        )
    installed = find_service(config.source_path)
    if installed and installed.identity.scope != config.scope:
        raise BlockedError(
            f"Installed Trigger execution identity is {installed.identity.job_id}-"
            f"{installed.identity.scope}; "
            "YAML selects a different identity; run trigger update"
        )


def _require_safe_stop(config: TriggerConfig | JobIdentity) -> None:
    from vaultkeep.trigger.paths import state_root
    from vaultkeep.trigger.state import StateStore

    store = StateStore(state_root(config.scope))
    # Inspect every retained instance, including orphan attempts and blocked records.
    ids = set(store.list_instance_ids()) | set(store.list_blocked_ids()) | {config.job_id}
    for attempt_id in store.list_attempt_ids():
        request = store.read_request(attempt_id)
        if request is not None:
            ids.add(request.instance_id)
    for instance_id in ids:
        state = store.read_instance(instance_id)
        blocked = store.read_blocked(instance_id)
        activity, attempt = store.dispatch_activity(instance_id, state, blocked)
        uncertain = state and any(
            item["outcome"] in ("blocked", "outcome_unknown", "result_unknown")
            for item in state.attempt_summaries
        )
        if (
            blocked
            or attempt
            or activity == "pending"
            or uncertain
            or (state and state.blocking_attempt_id)
        ):
            raise BlockedError(
                "Cannot stop monitor with active, pending or unreconciled backup/safety state; "
                "inspect status and review Trigger recovery records before retrying"
            )


def _write_service(destination: Path, content: str) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(dir=destination.parent, prefix=".vaultkeep-trigger-")
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, destination)
    finally:
        Path(temporary).unlink(missing_ok=True)


def manage_service(config: TriggerConfig | JobIdentity | Path, action: str) -> None:
    from vaultkeep.backup.paths import user_workflow_paths
    from vaultkeep.errors import LockError
    from vaultkeep.locking import JobLock, job_lock_path
    from vaultkeep.trigger.monitor import MonitorLock

    source = canonical_config_path(config if isinstance(config, Path) else config.source_path)
    unit = service_name(source)
    destination = units_root() / unit
    # Serialize lifecycle commands independently of execution identity and lifetime lock.
    management = JobLock(monitor_lock_path(path_scope(source)).with_name("service.lock"))
    try:
        management.acquire()
        installed = find_service(source)
        if action == "install" and installed and not installed.legacy:
            raise ConfigurationError(
                f"Trigger already installed for this YAML: {unit}; use trigger update"
            )
        if action != "install" and installed is None:
            raise ConfigurationError(
                f"managed monitor service not found: {unit}; use trigger install"
            )
        if action in ("install", "update"):
            assert isinstance(config, TriggerConfig)
            if not config.enabled_instances():
                raise ConfigurationError("monitor installation requires trigger.enabled: true")
        with ExitStack() as locks:
            if installed:
                old = installed.identity
                lock = JobLock(
                    job_lock_path(
                        root=user_workflow_paths().lock_root,
                        job_id=old.job_id,
                        identity_hash=old.scope,
                    )
                )
                lock.acquire()
                locks.callback(lock.release)
                _require_safe_stop(old)
                _systemctl("disable", "--now", installed.unit)
            # A direct monitor cannot race the final safety check or unit replacement.
            locks.enter_context(MonitorLock(monitor_lock_path(path_scope(source))))
            if installed:
                # Also exclude monitors from pre-path-lock versions during migration.
                if installed.identity.scope != path_scope(source):
                    locks.enter_context(MonitorLock(monitor_lock_path(installed.identity.scope)))
                _require_safe_stop(installed.identity)
            if action in ("disable", "remove"):
                if action == "remove":
                    assert installed is not None
                    (units_root() / installed.unit).unlink()
                    _systemctl("daemon-reload")
                return
            assert isinstance(config, TriggerConfig)
            if installed is None or installed.identity.scope != config.scope:
                assert config.scope is not None
                selected_lock = JobLock(
                    job_lock_path(
                        root=user_workflow_paths().lock_root,
                        job_id=config.job_id,
                        identity_hash=config.scope,
                    )
                )
                selected_lock.acquire()
                locks.callback(selected_lock.release)
                _require_safe_stop(config)
            _write_service(destination, render_service(config))
            if installed and installed.legacy:
                (units_root() / installed.unit).unlink()
            try:
                _systemctl("daemon-reload")
            except BlockedError:
                if installed:
                    _write_service(units_root() / installed.unit, installed.content)
                if installed is None or installed.legacy:
                    destination.unlink()
                raise
        try:
            _systemctl("enable", "--now", unit)
        except BlockedError:
            # Do not restore an old selection while a new monitor may still run.
            _systemctl("disable", "--now", unit)
            with ExitStack() as locks:
                rollback_lock = JobLock(
                    job_lock_path(
                        root=user_workflow_paths().lock_root,
                        job_id=config.job_id,
                        identity_hash=config.scope,
                    )
                )
                rollback_lock.acquire()
                locks.callback(rollback_lock.release)
                locks.enter_context(MonitorLock(monitor_lock_path(path_scope(source))))
                _require_safe_stop(config)
                if installed:
                    _write_service(units_root() / installed.unit, installed.content)
                if installed is None or installed.legacy:
                    destination.unlink()
                _systemctl("daemon-reload")
            raise
    except LockError as error:
        raise BlockedError(
            f"Cannot manage monitor: {error}; wait for the active operation to finish"
        ) from error
    finally:
        management.release()
