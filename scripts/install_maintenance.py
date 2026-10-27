"""Pause, refresh and resume installed jobs for install.sh (stdlib only).

The private journal is scoped to one installer transaction. User configuration,
unit rendering and file restoration always run with that account's privileges.
"""

from __future__ import annotations

import codecs
import json
import os
import pwd
import re
import subprocess
import sys
import traceback
from pathlib import Path

from install_support import (
    Layout,
    capture_command,
    command_failure,
    exception_summary,
    execute,
    fail,
    issue,
    manager_command,
    properties,
    report_error,
    user_managers,
)

PATTERNS = ("vaultkeep@*.timer", "vaultkeep-trigger.service", "vaultkeep-trigger@*.service")


def launcher_resource(spec: dict, unit: str) -> str:
    return f"User '{spec['account'] or 'root'}', unit '{unit}'"


def environment_value(value: str) -> str:
    """Decode systemctl's literal ANSI-C shell quoting without evaluating a shell.

    See systemctl(1), show-environment. systemd emits octal byte escapes for
    nonprinting bytes and backslash escapes for quotes/backslashes.
    """
    if value.startswith("$'") and value.endswith("'"):
        raw = value[2:-1]
        if not re.fullmatch(r"(?:[^'\\]|\\(?:[abfnrtv\\'\"]|[0-7]{3}))*", raw):
            fail("unrecognized manager environment escaping")
        return os.fsdecode(codecs.escape_decode(os.fsencode(raw))[0])
    return "" if value == '""' else value


def save(path: Path, data: dict) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, indent=2) + "\n")
    temporary.chmod(0o600)
    temporary.replace(path)


def account_spec(account: str | None, uid: int | None, layout: Layout) -> dict:
    if account is None:
        return {
            "account": None,
            "uid": None,
            "command": ["systemctl"],
            "env": {},
            "unit_roots": [str(layout.service.parent)],
            "paths": {
                "jobs_root": str(layout.config / "jobs"),
                "units_root": str(layout.service.parent),
                "registry_path": str(layout.registry),
            },
            "active": [],
            "states": {},
        }
    entry = pwd.getpwnam(account)
    home = Path(entry.pw_dir)
    env = {"HOME": str(home), "USER": account, "LOGNAME": account}
    if uid is not None:
        env.update(
            XDG_RUNTIME_DIR=f"/run/user/{uid}",
            DBUS_SESSION_BUS_ADDRESS=f"unix:path=/run/user/{uid}/bus",
        )
        # Use the manager's XDG roots rather than the root caller's environment.
        exported = execute([*manager_command(account, uid), "show-environment"])
        for line in exported.splitlines():
            key, separator, value = line.partition("=")
            if separator and key in ("XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
                value = environment_value(value)
                if not value:
                    continue
                if not Path(value).is_absolute() or any(
                    ord(char) < 32 or ord(char) == 127 for char in value
                ):
                    fail(f"{account}: non-absolute {key} requires review")
                env[key] = value
    config = Path(env.get("XDG_CONFIG_HOME", str(home / ".config")))
    state = Path(env.get("XDG_STATE_HOME", str(home / ".local/state")))
    root = config / "systemd/user"
    return {
        "account": account,
        "uid": uid,
        "command": manager_command(account, uid) if uid is not None else None,
        "env": env,
        "unit_roots": [str(root)],
        "paths": {
            "jobs_root": str(config / "vaultkeep/jobs"),
            "units_root": str(root),
            "registry_path": str(state / "vaultkeep/systemd-instances.json"),
        },
        "active": [],
        "states": {},
    }


def inventory(layout: Layout) -> dict:
    specs = [account_spec(None, None, layout)]
    managers = user_managers()
    accounts = {account for account, _ in managers}
    for account, uid in managers:
        specs.append(account_spec(account, uid, layout))
    # Disabled and unloaded jobs of accounts without a running manager still
    # need refreshed commands. Do not create sessions or start user managers.
    for entry in pwd.getpwall():
        if entry.pw_name in accounts:
            continue
        root = Path(entry.pw_dir) / ".config/systemd/user"
        if (
            any(root.glob("vaultkeep@*.timer"))
            or any(root.glob("vaultkeep-trigger@*.service"))
            or (Path(entry.pw_dir) / ".config/vaultkeep/jobs").exists()
            or (Path(entry.pw_dir) / ".local/state/vaultkeep/systemd-instances.json").exists()
        ):
            specs.append(account_spec(entry.pw_name, None, layout))
    for spec in specs:
        command = spec["command"]
        if command is None:
            continue
        loaded = execute(
            [*command, "list-units", "--all", "--plain", "--no-legend", "--no-pager", *PATTERNS]
        )
        # systemd 257 returns 1 without output when no unit files match.
        files = execute(
            [*command, "list-unit-files", "--no-legend", "--no-pager", *PATTERNS],
            empty_status=1,
        )
        names = set()
        for line in (loaded + "\n" + files).splitlines():
            fields = line.split()
            if not fields:
                continue
            unit = fields[0]
            if unit in ("vaultkeep@.timer", "vaultkeep-trigger@.service"):
                continue
            if (
                (unit.startswith("vaultkeep@") and unit.endswith(".timer"))
                or (unit.startswith("vaultkeep-trigger@") and unit.endswith(".service"))
                or unit == "vaultkeep-trigger.service"
            ):
                names.add(unit)
            elif unit.startswith("vaultkeep@") or unit.startswith("vaultkeep-trigger-exec@"):
                # Some managers return a superset; activity() checks workers.
                continue
            else:
                fail(
                    issue(
                        f"User '{spec['account'] or 'root'}'",
                        f"Unexpected unit in systemd inventory: {line}",
                    )
                )
        for unit in sorted(names):
            state = properties(command, unit)
            if state["LoadState"] != "loaded" or state["ActiveState"] not in (
                "active",
                "inactive",
                "failed",
            ):
                reason = (
                    f"Unexpected systemd LoadState: {state['LoadState']}. "
                    if state["LoadState"] != "loaded"
                    else ""
                )
                fail(
                    issue(
                        launcher_resource(spec, unit),
                        reason
                        + f"Unexpected systemd state: {state['ActiveState']}/{state['SubState']}.",
                    )
                )
            if state["SubState"] not in (
                "running",
                "waiting",
                "elapsed",
                "dead",
                "failed",
                "exited",
            ):
                fail(
                    issue(
                        launcher_resource(spec, unit),
                        f"Unexpected systemd state: {state['ActiveState']}/{state['SubState']}.",
                    )
                )
            if state["UnitFileState"] not in (
                "enabled",
                "enabled-runtime",
                "disabled",
                "static",
                "indirect",
                "linked",
                "linked-runtime",
            ):
                fail(
                    issue(
                        launcher_resource(spec, unit),
                        f"Unexpected systemd UnitFileState: {state['UnitFileState']!r}; "
                        "review launcher enablement.",
                    )
                )
            fragment = Path(state["FragmentPath"])
            if unit.startswith("vaultkeep-trigger"):
                if unit == "vaultkeep-trigger.service":
                    fail(
                        "legacy standalone Trigger monitor requires migration "
                        "before automatic maintenance"
                    )
                if state.get("DropInPaths") or not fragment.read_text().startswith(
                    f"# Managed by Vaultkeep: {unit}\n"
                ):
                    fail(f"unmanaged monitor/override requires review: {unit}")
            elif spec["account"] is None:
                if fragment != layout.timer:
                    fail(f"unexpected core timer fragment: {unit}")
            elif fragment.name != unit or not fragment.read_text().startswith(
                "[Unit]\nDescription=Vaultkeep user backup timer "
            ):
                fail(f"unmanaged user timer requires review: {unit}")
            if spec["account"] is not None and str(fragment.parent) not in spec["unit_roots"]:
                fail(f"{unit}: unit outside the manager's XDG config root requires review")
            spec["states"][unit] = state["UnitFileState"]
            if state["ActiveState"] == "active":
                spec["active"].append(unit)
    return {"accounts": specs}


def pause(path: Path, layout: Layout) -> None:
    document = inventory(layout)
    # Persist the full resume list before the first stop (including signals).
    save(path, document)
    for spec in document["accounts"]:
        for unit in spec["active"]:
            execute([*spec["command"], "stop", unit])
        print(f"Inventoried jobs for {spec['account'] or 'root'}")


def account_operation(spec: dict, python: str, action: str, payload: dict) -> dict | None:
    argv = [python, "-I", "-m", "vaultkeep.maintenance", action]
    if action == "restore":
        # Restore as the account using only stdlib, independently of either
        # installed release and without requiring access to the source checkout.
        source = Path(__file__).resolve().parents[1] / "src/vaultkeep/maintenance.py"
        argv = [sys.executable, "-I", "-c", source.read_text(), action]
    if spec["account"] is not None:
        argv = [
            "runuser",
            "-u",
            spec["account"],
            "--",
            "env",
            "-i",
            "PATH=/usr/local/bin:/usr/bin:/bin",
            *[f"{key}={value}" for key, value in spec["env"].items()],
            *argv,
        ]
    result = capture_command(argv, input=json.dumps(payload), timeout=120)
    if result.returncode:
        try:
            response = json.loads(result.stdout)
        except ValueError:
            response = {}
        if isinstance(response, dict):
            if response.get("errors"):
                fail(configuration_report(response["errors"]))
            if isinstance(response.get("failure"), str):
                fail(
                    issue(f"User '{spec['account'] or 'root'}', managed jobs", response["failure"])
                )
        # Failures before the module starts (for example runuser or Python)
        # cannot use the JSON response; use the safe command diagnostic.
        reason = command_failure(argv, result).split("\n", 1)[1].strip()
        fail(issue(f"User '{spec['account'] or 'root'}', managed jobs", reason))
    return json.loads(result.stdout) if action in ("plan", "check-configurations") else None


def configuration_report(errors: list[dict]) -> str:
    def single_line(value: str) -> str:
        return "".join(char if char.isprintable() else " " for char in value).strip()

    lines = []
    for error in errors:
        lines.append(f"Issue: {single_line(error['file'])}")
        for problem in error["issues"]:
            message = single_line(problem["message"]).rstrip(".")
            field = single_line(problem["path"])
            suffix = f" (field: {field})" if field != "<root>" else ""
            lines.append(f"       {message}{suffix}.")
    return "\n".join(lines)


def check_configurations(layout: Layout, python: str) -> None:
    errors = []
    failures = []
    for spec in inventory(layout)["accounts"]:
        try:
            result = account_operation(
                spec, python, "check-configurations", {**spec, "user": spec["account"] is not None}
            )
            errors.extend(result["errors"])
        except (ValueError, OSError, subprocess.SubprocessError) as error:
            traceback.print_exc(file=sys.stderr)
            failures.append(exception_summary(error))
    if errors or failures:
        details = []
        if errors:
            details.append(configuration_report(errors))
        details.extend(failures)
        fail("\n\n".join(details))


def refresh(path: Path, python: str) -> None:
    document = json.loads(path.read_text())
    document["python"] = python
    for spec in document["accounts"]:
        spec["plan"] = account_operation(
            spec, python, "plan", {**spec, "user": spec["account"] is not None}
        )
        planned = {job["unit"] for job in spec["plan"]["jobs"]}
        if set(spec["states"]) - planned:
            missing = ", ".join(sorted(set(spec["states"]) - planned))
            fail(
                issue(
                    f"User '{spec['account'] or 'root'}', units '{missing}'",
                    "Installed launcher has no refreshable managed job; "
                    "review its unit and configuration.",
                )
            )
    # Snapshot every account before changing the first account's files.
    save(path, document)
    for spec in document["accounts"]:
        account_operation(spec, python, "apply", spec["plan"])


def validate(path: Path) -> None:
    document = json.loads(path.read_text())
    for spec in document["accounts"]:
        account_operation(
            spec, document["python"], "validate", {**spec["plan"], "active": spec["active"]}
        )
        if spec["command"]:
            for unit, enabled in spec["states"].items():
                state = properties(spec["command"], unit)
                if state["UnitFileState"] != enabled or state["ActiveState"] not in (
                    "inactive",
                    "failed",
                ):
                    fail(
                        issue(
                            launcher_resource(spec, unit),
                            "Launcher state changed during maintenance: "
                            f"{state['ActiveState']}/{state.get('SubState', '<missing>')}, "
                            f"UnitFileState={state['UnitFileState']} "
                            f"(expected {enabled}, inactive or failed).",
                        )
                    )


def restore(path: Path) -> None:
    document = json.loads(path.read_text())
    errors = []
    for spec in document["accounts"]:
        if "plan" not in spec:
            continue
        try:
            account_operation(spec, document["python"], "restore", spec["plan"])
        except (ValueError, OSError, subprocess.SubprocessError) as error:
            traceback.print_exc(file=sys.stderr)
            errors.append(exception_summary(error))
    if errors:
        fail("\n".join(errors))


def resume(path: Path) -> None:
    document = json.loads(path.read_text())
    errors = []
    for spec in document["accounts"]:
        for unit in spec["active"]:
            try:
                state = properties(spec["command"], unit)
                if state["UnitFileState"] != spec["states"][unit]:
                    fail(
                        issue(
                            launcher_resource(spec, unit),
                            "Enablement changed during maintenance: "
                            f"UnitFileState={state['UnitFileState']} "
                            f"(expected {spec['states'][unit]}).",
                        )
                    )
                execute([*spec["command"], "start", unit])
                state = properties(spec["command"], unit)
                if state["ActiveState"] != "active":
                    fail(
                        issue(
                            launcher_resource(spec, unit),
                            "Launcher did not resume: systemd state "
                            f"{state['ActiveState']}/{state.get('SubState', '<missing>')}.",
                        )
                    )
            except (ValueError, OSError, subprocess.SubprocessError) as error:
                traceback.print_exc(file=sys.stderr)
                summary = exception_summary(error)
                errors.append(
                    summary
                    if summary.startswith("Issue: ")
                    else issue(launcher_resource(spec, unit), summary)
                )
    if errors:
        fail("\n".join(errors))


def main() -> None:
    action, *args = sys.argv[1:]
    if action == "check-configurations":
        check_configurations(Layout(), args[0])
        return
    journal, *args = args
    path = Path(journal)
    if action == "pause":
        pause(path, Layout())
    elif action == "refresh":
        refresh(path, args[0])
    elif action == "validate":
        validate(path)
    elif action == "restore":
        restore(path)
    elif action == "resume":
        resume(path)
    else:
        fail(f"unknown maintenance operation: {action}")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        traceback.print_exc(file=sys.stderr)
        report_error(exception_summary(error))
        raise SystemExit(1) from None
