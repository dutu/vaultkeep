"""Installer-only job rendering and restoration, executed as the owning account.

JSON travels over stdin/stdout. No shell commands or job hooks are executed, and
refreshing a unit never enables, starts, or reloads a service manager.
"""

from __future__ import annotations

import base64
import json
import os
import re
import shlex
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any


def read_file(path: Path) -> dict[str, Any] | None:
    """Snapshot a regular file without following its final symlink."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(f"not a regular managed file: {path}")
        content = stream.read()
    return {
        "content": base64.b64encode(content).decode("ascii"),
        "mode": stat.S_IMODE(info.st_mode),
        "atime_ns": info.st_atime_ns,
        "mtime_ns": info.st_mtime_ns,
    }


def command_selection(content: str, *, monitor: bool, user: bool) -> tuple[Path, dict[str, str]]:
    """Decode only the literal argument forms written by Vaultkeep's renderers."""
    commands = [line[10:] for line in content.splitlines() if line.startswith("ExecStart=")]
    commands = [command for command in commands if command]
    if len(commands) != 1 or "\\\n" in content:
        raise ValueError("managed service requires exactly one literal ExecStart")
    raw = commands[0]
    # Generated units escape literal systemd expansions as %% and $$. Refuse
    # custom specifiers, environment substitutions and C-style escapes.
    if re.search(r"(?<!%)%(?!%)|(?<!\$)\$(?!\$)", raw) or "\\x" in raw:
        raise ValueError("custom systemd expansions require manual review")
    argv = [word.replace("%%", "%").replace("$$", "$") for word in shlex.split(raw)]
    if not argv or argv.pop(0) != "/usr/local/bin/vaultkeep":
        raise ValueError("unexpected managed service executable")
    if user and (not argv or argv.pop(0) != "--user"):
        raise ValueError("user service requires --user")
    # Before the unified CLI, per-job monitors placed 'trigger' before options.
    if monitor and argv and argv[0] == "trigger":
        argv.pop(0)
        suffix = ["monitor"]
    else:
        suffix = ["trigger", "monitor"] if monitor else ["run"]
    if len(argv) < 2 or argv.pop(0) != "--config":
        raise ValueError("managed service requires its original --config")
    config = Path(argv.pop(0))
    if not config.is_absolute():
        raise ValueError("managed service configuration must be absolute")
    params: dict[str, str] = {}
    while argv and argv[0] == "--param":
        if len(argv) < 2 or "=" not in argv[1]:
            raise ValueError("invalid managed service parameter")
        key, value = argv[1].split("=", 1)
        if key in params:
            raise ValueError("duplicate managed service parameter")
        params[key] = value
        del argv[:2]
    if argv != suffix:
        raise ValueError("custom managed service arguments require manual review")
    return config, params


def require_generated(original: str, rendered: str, *, variable: tuple[str, ...]) -> None:
    """Permit regenerated values, but refuse extra or edited service directives."""

    def normalize(content: str) -> list[str]:
        return [
            line.split("=", 1)[0] + "=<generated>"
            if any(line.startswith(key + "=") for key in variable)
            else line
            for line in content.splitlines()
        ]

    if normalize(original) != normalize(rendered):
        raise ValueError("locally modified managed unit requires review")


def plan(document: dict[str, Any]) -> dict[str, Any]:
    """Render existing jobs; retain snapshots before any mutation."""
    from vaultkeep.backup.workflow import load_validated_config
    from vaultkeep.schedule import TimerManager, TimerPaths, render_schedule
    from vaultkeep.trigger.config import load_config as load_trigger
    from vaultkeep.trigger.service import render_service, service_name

    user = document["user"]
    paths = TimerPaths(**{key: Path(value) for key, value in document["paths"].items()})
    manager = TimerManager(paths, user_mode=user)
    changes = []
    jobs = []

    def change(path: Path, content: str | None, before: dict[str, Any] | None) -> None:
        if before is None and content is None:
            return
        after = base64.b64encode(content.encode()).decode("ascii") if content is not None else None
        changes.append({"path": str(path), "before": before, "after": after})

    timers: set[str] = set()
    monitors: set[Path] = set()
    for root in map(Path, document["unit_roots"]):
        if user:
            for path in root.glob("vaultkeep@*.timer"):
                timers.add(path.name[10:-6])
            monitors.update(root.glob("vaultkeep-trigger@*.service"))
        else:
            timers.update(path.name[10:-8] for path in root.glob("vaultkeep@*.timer.d"))
    registry = manager._registry()
    timers.update(registry)
    for instance in sorted(timers):
        timer = manager._timer_drop_in_path(instance)
        service = manager._service_drop_in_path(instance)
        if instance not in registry or registry[instance] != str(timer):
            raise ValueError(f"timer ownership registry requires review: {timer}")
        timer_snapshot = read_file(timer)
        if timer_snapshot is None:
            raise ValueError(f"managed timer is missing: {timer}")
        service_content = None
        service_snapshot = None
        if user or service.exists() or service.is_symlink():
            service_snapshot = read_file(service)
            if service_snapshot is None:
                raise ValueError(f"managed service is missing: {service}")
            config_path, params = command_selection(
                base64.b64decode(service_snapshot["content"]).decode(), monitor=False, user=user
            )
            service_content = base64.b64decode(service_snapshot["content"]).decode()
        else:
            config_path, params = paths.jobs_root / f"{instance}.yaml", {}
        config = manager._load_managed(config_path, require_enabled=True, runtime_params=params)
        if manager._instance_id(config_path, config.job.id, params) != instance:
            raise ValueError(f"job identity changed: {timer}")
        # Additional local unit overrides must never be silently overwritten.
        for directory, allowed in (
            (paths.units_root / f"vaultkeep@{instance}.timer.d", {timer}),
            (paths.units_root / f"vaultkeep@{instance}.service.d", {service}),
        ):
            if any(path not in allowed for path in directory.glob("*.conf")):
                raise ValueError(f"local schedule overrides require review: {directory}")
        rendered_timer = manager._timer_content(instance, render_schedule(config))
        rendered_service = manager._service_content(config_path, instance, params)
        require_generated(
            base64.b64decode(timer_snapshot["content"]).decode(),
            rendered_timer,
            variable=("OnCalendar", "RandomizedDelaySec", "FixedRandomDelay", "Persistent"),
        )
        if service_content is not None:
            if rendered_service is None:
                raise ValueError(f"unexpected service override requires review: {service}")
            require_generated(service_content, rendered_service, variable=("ExecStart",))
        change(timer, rendered_timer, timer_snapshot)
        change(service, rendered_service, service_snapshot)
        jobs.append(
            {
                "config": str(config_path),
                "params": params,
                "unit": f"vaultkeep@{instance}.timer",
                "monitor": False,
            }
        )
    for path in sorted(monitors):
        snapshot = read_file(path)
        assert snapshot is not None
        content = base64.b64decode(snapshot["content"]).decode()
        if not content.startswith(f"# Managed by Vaultkeep: {path.name}\n"):
            raise ValueError(f"unmanaged monitor service: {path}")
        if path.with_name(path.name + ".d").exists():
            raise ValueError(f"local monitor overrides require review: {path}")
        config_path, params = command_selection(content, monitor=True, user=True)
        load_validated_config(config_path, runtime_params=params)
        trigger = load_trigger(config_path, runtime_params=params)
        if service_name(trigger) != path.name:
            raise ValueError(f"job identity changed: {path}")
        rendered = render_service(trigger)
        require_generated(content, rendered, variable=("ExecStart",))
        change(path, rendered, snapshot)
        jobs.append(
            {"config": str(config_path), "params": params, "unit": path.name, "monitor": True}
        )
    return {"changes": changes, "jobs": jobs}


def apply(document: dict[str, Any], *, restore: bool = False) -> None:
    """Write as the account, refusing to clobber concurrent edits on apply/rollback."""
    errors = []
    for change in document["changes"]:
        try:
            path = Path(change["path"])
            current = read_file(path)
            content = current["content"] if current else None
            before = change["before"]
            original = before["content"] if before else None
            if content not in (original, change["after"]):
                raise ValueError(f"managed unit changed during maintenance: {path}")
            target = original if restore else change["after"]
            if content == target:
                continue
            if target is None:
                path.unlink(missing_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
                # Keep restoration stdlib-only: it must also work if the
                # candidate interpreter/package is damaged or already gone.
                descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".vaultkeep-")
                try:
                    with os.fdopen(descriptor, "wb") as stream:
                        stream.write(base64.b64decode(target))
                        stream.flush()
                        os.fsync(stream.fileno())
                        os.fchmod(stream.fileno(), before["mode"] if before else 0o644)
                    os.replace(temporary, path)
                finally:
                    Path(temporary).unlink(missing_ok=True)
                if restore and before:
                    os.utime(path, ns=(before["atime_ns"], before["mtime_ns"]))
        except (OSError, ValueError) as error:
            if not restore:
                raise
            errors.append(str(error))
    if errors:
        raise ValueError("; ".join(errors))


def validate(document: dict[str, Any]) -> None:
    """Validate all jobs and runtime readiness only for launchers being resumed."""
    from vaultkeep.backup.workflow import load_validated_config, validate_job
    from vaultkeep.trigger.config import load_config as load_trigger

    for job in document["jobs"]:
        config_path = Path(job["config"])
        params = job["params"]
        load_validated_config(config_path, runtime_params=params)
        if job["unit"] not in document["active"]:
            continue
        validate_job(config_path, runtime_params=params)
        if job["monitor"]:
            from vaultkeep.trigger.paths import state_root
            from vaultkeep.trigger.preflight import runtime_preflight
            from vaultkeep.trigger.systemd import RealSystemdController

            config = load_trigger(config_path, runtime_params=params)
            if not config.enabled_instances():
                raise ValueError(f"active monitor configuration is disabled: {job['unit']}")
            runtime_preflight(
                config, RealSystemdController(), state_root(config.scope), validate_backup=False
            )


def main() -> None:
    action = sys.argv[1]
    document = json.load(sys.stdin)
    if action == "plan":
        print(json.dumps(plan(document)))
    elif action in ("apply", "restore"):
        apply(document, restore=action == "restore")
    elif action == "validate":
        validate(document)
    else:
        raise ValueError(f"unknown maintenance action: {action}")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"ERROR: installer jobs: {error}", file=sys.stderr)
        raise SystemExit(1) from None
