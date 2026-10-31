"""Path-based monitor identity and fail-closed lifecycle transitions."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace

import pytest

from tests.trigger.conftest import FakeController, FakeProvider, RecordingNotifier, make_request
from tests.trigger.test_service import config
from vaultkeep.backup.paths import user_workflow_paths
from vaultkeep.cli import main
from vaultkeep.config.identity import JobIdentity
from vaultkeep.locking import JobLock, job_lock_path
from vaultkeep.state.identity import job_identity_hash
from vaultkeep.trigger import service
from vaultkeep.trigger.config import load_config
from vaultkeep.trigger.errors import BlockedError, ConfigurationError, StateError
from vaultkeep.trigger.models import InstanceState, LaunchRecord, utc_now
from vaultkeep.trigger.monitor import Monitor, MonitorLock, unit_name_for
from vaultkeep.trigger.paths import monitor_lock_path, state_root
from vaultkeep.trigger.state import StateStore
from vaultkeep.trigger.status import recovery_status, trigger_status


class Manager:
    def __init__(self, root):
        self.root = root
        self.calls = []
        self.fail = None
        self.after_stop = None
        self.external = {}
        self.active = set()

    def __call__(self, *args):
        self.calls.append(args)
        if args[0].startswith("list-"):
            return "\n".join(f"{unit} disabled" for unit in self.external)
        if self.fail and self.fail(args):
            raise BlockedError(f"injected systemd failure: {args[0]}")
        if args[0] == "disable":
            self.active.discard(args[-1])
            if self.after_stop:
                self.after_stop()
        if args[0] == "enable":
            assert not self.active, "replacement started while another monitor was active"
            self.active.add(args[-1])
        return ""

    def properties(self, unit, **_):
        path = self.external.get(unit, self.root / unit)
        if not path.exists():
            return {"LoadState": "not-found"}
        return {
            "LoadState": "loaded",
            "ActiveState": "active" if unit in self.active else "inactive",
            "SubState": "running" if unit in self.active else "dead",
            "UnitFileState": "enabled" if unit in self.active else "disabled",
            "FragmentPath": str(path),
            "DropInPaths": "",
            "NeedDaemonReload": "no",
        }

    def mutations(self):
        return [args for args in self.calls if not args[0].startswith("list-")]


@pytest.fixture
def installation(tmp_path, valid_config, monkeypatch):
    job = config(tmp_path, valid_config)
    root = tmp_path / "units"
    manager = Manager(root)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr("vaultkeep.trigger.paths.account_home", lambda: tmp_path)
    monkeypatch.setattr(service, "units_root", lambda: root)
    monkeypatch.setattr(service, "_systemctl", manager)
    monkeypatch.setattr("vaultkeep.schedule.status.unit_properties", manager.properties)
    return job, manager


def changed(job, *, job_id="new-job", params=None):
    params = {"repo": "nas-b"} if params is None else params
    scope = job_identity_hash(job.source_path, job_id, runtime_params=params)
    return replace(job, job_id=job_id, params=params, scope=scope)


def seed_legacy(job, manager):
    unit = service.legacy_service_name(job)
    manager.root.mkdir(exist_ok=True)
    path = manager.root / unit
    path.write_text(service.render_service(job, legacy=True))
    manager.active.add(unit)
    return path


def evidence(job):
    root = state_root(job.scope)
    return {
        str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()
    }


def test_service_identity_depends_only_on_canonical_path(installation, tmp_path):
    job, _ = installation
    before = service.service_name(job)
    digest = hashlib.sha256(bytes(job.source_path)).hexdigest()[:16]
    assert before == f"vaultkeep-trigger@app-{digest}.service"
    job.source_path.write_text("invalid: [")
    assert service.service_name(changed(job)) == before
    assert service.service_name(changed(job, params={"repo": "nas-c"})) == before
    alias = tmp_path / "alias.yml"
    alias.symlink_to(job.source_path)
    assert service.service_name(alias) == before
    assert service.path_scope(alias) == service.path_scope(job.source_path)
    assert service.service_name(tmp_path / "other" / "app.yaml") != before
    job.source_path.unlink()
    assert service.service_name(job.source_path) == before


@pytest.mark.parametrize(
    "filename,escaped",
    [
        ("Bisq_sd1-srv_backups-full.yaml", "Bisq_sd1-srv_backups-full"),
        ("space %\\é.yml", r"space\x20\x25\x5c\xc3\xa9"),
        (".hidden.yaml", r"\x2ehidden"),
    ],
)
def test_service_filename_escaping(tmp_path, filename, escaped):
    source = tmp_path / filename
    assert (
        service.service_name(source)
        == f"vaultkeep-trigger@{escaped}-{service.path_scope(source)}.service"
    )


def test_second_install_with_different_parameters_is_refused(installation):
    job, manager = installation
    service.manage_service(job, "install")
    original = (manager.root / service.service_name(job)).read_bytes()
    manager.calls.clear()
    with pytest.raises(ConfigurationError, match="already installed"):
        service.manage_service(changed(job, job_id=job.job_id), "install")
    assert manager.mutations() == []
    assert (manager.root / service.service_name(job)).read_bytes() == original
    assert len(list(manager.root.glob("*.service"))) == 1


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("edit", ["job", "invalid", "missing"])
@pytest.mark.parametrize("action", ["disable", "remove"])
def test_lifecycle_uses_installed_identity_without_loading_yaml(installation, legacy, edit, action):
    job, manager = installation
    if legacy:
        path = seed_legacy(job, manager)
    else:
        service.manage_service(job, "install")
        path = manager.root / service.service_name(job)
    store = StateStore(state_root(job.scope))
    store.write_instance(InstanceState(instance_id=job.job_id))
    before = evidence(job)
    if edit == "missing":
        job.source_path.unlink()
    elif edit == "invalid":
        job.source_path.write_text("invalid: [")
    else:
        raw = json.loads(job.source_path.read_text())
        raw["job"]["id"] = "other-job"
        raw["trigger"]["params"] = {"repo": "other"}
        job.source_path.write_text(json.dumps(raw))
    assert main(["--user", "--config", str(job.source_path), "trigger", action]) == 0
    assert path.exists() == (action == "disable")
    assert evidence(job) == before
    assert all("exec@" not in " ".join(args) for args in manager.calls)


@pytest.mark.parametrize("action", ["update", "disable", "remove"])
@pytest.mark.parametrize("legacy", [False, True])
def test_old_execution_lock_blocks_changes_even_after_yaml_identity_changes(
    installation, action, legacy
):
    job, manager = installation
    if legacy:
        seed_legacy(job, manager)
    else:
        service.manage_service(job, "install")
    manager.calls.clear()
    lock = JobLock(
        job_lock_path(
            root=user_workflow_paths().lock_root, job_id=job.job_id, identity_hash=job.scope
        )
    )
    lock.acquire()
    try:
        with pytest.raises(BlockedError, match="active operation"):
            service.manage_service(changed(job), action)
    finally:
        lock.release()
    assert manager.mutations() == []


@pytest.mark.parametrize(
    "kind", ["pending", "blocked", "active", "unassociated", "uncertain", "corrupt"]
)
def test_identity_transition_preserves_unresolved_old_evidence(installation, kind):
    job, manager = installation
    service.manage_service(job, "install")
    store = StateStore(state_root(job.scope))
    state = InstanceState(instance_id=job.job_id)
    request = make_request(job.instances[0])
    if kind == "pending":
        state.pending_request = replace(request, attempt_id="")
    elif kind == "active":
        state.active_attempt_id = request.attempt_id
    elif kind == "unassociated":
        store.write_request(request)
        store.write_launch(LaunchRecord(request.attempt_id, "worker.service", utc_now()))
    elif kind == "blocked":
        store.write_blocked(job.job_id, "operator review required")
    elif kind == "uncertain":
        state.attempt_summaries = [
            {
                "attempt_id": request.attempt_id,
                "request_id": request.request_id,
                "outcome": "outcome_unknown",
                "completed_at": utc_now(),
            }
        ]
    store.write_instance(state)
    if kind == "corrupt":
        store.instance_path(job.job_id).write_text("{")
    before = evidence(job)
    manager.calls.clear()
    with pytest.raises(
        (BlockedError, StateError),
        match=r"state|corrupt|unreconciled|instance",
    ):
        service.manage_service(changed(job), "update")
    assert evidence(job) == before
    assert manager.mutations() == []


def test_stop_rechecks_state_written_during_shutdown(installation):
    job, manager = installation
    service.manage_service(job, "install")
    original = (manager.root / service.service_name(job)).read_bytes()
    store = StateStore(state_root(job.scope))
    manager.after_stop = lambda: store.write_blocked(job.job_id, "late unresolved dispatch")
    with pytest.raises(BlockedError, match="unreconciled"):
        service.manage_service(changed(job), "update")
    assert (manager.root / service.service_name(job)).read_bytes() == original
    assert store.read_blocked(job.job_id) == "late unresolved dispatch"


def test_update_changes_execution_identity_without_renaming_or_moving_state(installation):
    job, manager = installation
    service.manage_service(job, "install")
    store = StateStore(state_root(job.scope))
    store.write_instance(InstanceState(instance_id=job.job_id))
    before = evidence(job)
    new = changed(job)
    service.manage_service(new, "update")
    installed = service.find_service(job.source_path)
    assert installed.identity.scope == new.scope != job.scope
    assert installed.identity.job_id == new.job_id
    assert installed.identity.params == new.params
    assert installed.unit == service.service_name(job)
    assert evidence(job) == before
    assert not state_root(new.scope).exists()
    assert manager.mutations()[-3:] == [
        ("disable", "--now", service.service_name(job)),
        ("daemon-reload",),
        ("enable", "--now", service.service_name(job)),
    ]


@pytest.mark.parametrize("entry", ["install", "update"])
def test_single_legacy_service_migrates_with_history_untouched(installation, entry):
    job, manager = installation
    old_path = seed_legacy(job, manager)
    store = StateStore(state_root(job.scope))
    store.write_instance(InstanceState(instance_id=job.job_id))
    before = evidence(job)
    service.manage_service(changed(job), entry)
    installed = service.find_service(job.source_path)
    assert not old_path.exists()
    assert not installed.legacy
    assert manager.active == {installed.unit}
    assert installed.identity.scope == changed(job).scope
    assert evidence(job) == before
    assert manager.mutations() == [
        ("disable", "--now", old_path.name),
        ("daemon-reload",),
        ("enable", "--now", installed.unit),
    ]


@pytest.mark.parametrize("action", ["install", "update", "disable", "remove"])
def test_multiple_legacy_services_are_an_explicit_conflict(installation, action):
    job, manager = installation
    first = seed_legacy(job, manager)
    second = seed_legacy(changed(job), manager)
    before = {path: path.read_bytes() for path in (first, second)}
    with pytest.raises(ConfigurationError, match="Multiple Trigger services"):
        service.manage_service(job, action)
    assert manager.mutations() == []
    assert all(path.read_bytes() == data for path, data in before.items())
    report = recovery_status(job.source_path)
    assert report["read_error"] and "Multiple Trigger services" in report["diagnostics"][0]


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("failure", ["disable", "daemon-reload", "enable"])
def test_failed_service_operations_leave_verified_recoverable_definition(
    installation, failure, legacy
):
    job, manager = installation
    if legacy:
        old = seed_legacy(job, manager)
    else:
        service.manage_service(job, "install")
        old = manager.root / service.service_name(job)
    original = old.read_bytes()
    remaining = [True]

    def fail(args):
        if args[0] == failure and remaining:
            remaining.pop()
            return True
        return False

    manager.fail = fail
    with pytest.raises(BlockedError, match="injected"):
        service.manage_service(changed(job), "update")
    assert old.read_bytes() == original
    assert list(manager.root.glob("*.service")) == [old]
    assert service.find_service(job.source_path).identity.scope == job.scope
    if failure == "enable":
        assert ("disable", "--now", service.service_name(job)) in manager.calls


def test_failed_stop_after_partial_activation_keeps_new_identity_verifiable(installation):
    job, manager = installation
    seed_legacy(job, manager)

    def fail(args):
        if args[0] == "enable":
            manager.active.add(args[-1])  # Manager accepted start before the bus reply failed.
            return True
        return args[0] == "disable" and args[-1] == service.service_name(job)

    manager.fail = fail
    with pytest.raises(BlockedError, match="disable"):
        service.manage_service(changed(job), "update")
    installed = service.find_service(job.source_path)
    assert installed.identity.scope == changed(job).scope
    assert len(list(manager.root.glob("*.service"))) == 1
    assert manager.active == {installed.unit}


@pytest.mark.parametrize("edit", ["job", "parameters"])
def test_unexpected_restart_cannot_adopt_new_execution_identity(installation, monkeypatch, edit):
    job, _ = installation
    service.manage_service(job, "install")
    raw = json.loads(job.source_path.read_text())
    if edit == "job":
        raw["job"]["id"] = "edited-job"
    else:
        raw["trigger"]["params"] = {"repo": "edited"}
        raw["destination"]["root"] += "/{repo}"
    job.source_path.write_text(json.dumps(raw))
    desired = load_config(job.source_path)
    with pytest.raises(BlockedError, match="trigger update"):
        service.require_monitor_identity(desired, job.scope)
    with pytest.raises(BlockedError, match="trigger update"):
        service.require_monitor_identity(desired)
    report = trigger_status(desired)
    assert report["monitor"] == "UPDATE REQUIRED"
    assert report["details"]["installed_execution_identity"] == f"{job.job_id}-{job.scope}"
    assert report["details"]["desired_execution_identity"] == f"{desired.job_id}-{desired.scope}"
    assert not state_root(desired.scope).exists()


def test_direct_monitors_with_different_parameters_share_path_lock(installation, monkeypatch):
    job, _ = installation
    other = changed(job)
    monkeypatch.setattr(
        "vaultkeep.trigger.preflight.runtime_preflight", lambda *_: pytest.fail("preflight ran")
    )
    monitor = Monitor(
        other,
        store=StateStore(state_root(other.scope)),
        provider=FakeProvider(),
        controller=FakeController(),
        notifier=RecordingNotifier(),
    )
    with (
        MonitorLock(monitor_lock_path(service.path_scope(job.source_path))),
        pytest.raises(BlockedError, match="another monitor"),
    ):
        monitor.run()
    assert job.scope != other.scope
    assert unit_name_for("attempt", job.scope) != unit_name_for("attempt", other.scope)
    assert JobIdentity(job.source_path, job.job_id, job.params).scope == job.scope


@pytest.mark.parametrize("tamper", ["pin", "params", "job", "override", "owner", "effective"])
def test_installed_metadata_and_unit_ownership_are_required(installation, monkeypatch, tamper):
    job, manager = installation
    service.manage_service(job, "install")
    path = manager.root / service.service_name(job)
    if tamper == "pin":
        path.write_text(path.read_text().replace(job.scope, "0" * 16))
    elif tamper == "params":
        path.write_text(
            path.read_text().replace(" trigger monitor", " --param repo=changed trigger monitor")
        )
    elif tamper == "job":
        path.write_text(path.read_text().replace("Trigger job: app", "Trigger job: changed"))
    elif tamper == "override":
        path.with_name(path.name + ".d").mkdir()
    elif tamper == "owner":
        monkeypatch.setattr(service.os, "geteuid", lambda: 999999)
    else:
        properties = manager.properties
        monkeypatch.setattr(
            "vaultkeep.schedule.status.unit_properties",
            lambda unit, **kwargs: dict(properties(unit), DropInPaths="/tmp/unreviewed.conf"),
        )
    manager.calls.clear()
    with pytest.raises(ConfigurationError):
        service.find_service(job.source_path)
    assert path.exists() and manager.mutations() == []


def test_manager_known_legacy_outside_managed_root_is_not_ignored(installation, tmp_path):
    job, manager = installation
    external = tmp_path / service.legacy_service_name(job)
    external.write_text(service.render_service(job, legacy=True))
    manager.external[external.name] = external
    with pytest.raises(ConfigurationError, match="Cannot verify"):
        service.manage_service(job, "install")
    assert manager.mutations() == []


@pytest.mark.parametrize("edit", ["invalid", "missing", "invalid-id"])
def test_status_recovers_installed_identity_read_only(installation, edit):
    from vaultkeep.cli.status import collect_status

    job, manager = installation
    service.manage_service(job, "install")
    store = StateStore(state_root(job.scope))
    store.write_instance(InstanceState(instance_id=job.job_id))
    if edit == "missing":
        job.source_path.unlink()
    elif edit == "invalid":
        job.source_path.write_text("broken: [")
    else:
        raw = json.loads(job.source_path.read_text())
        raw["job"]["id"] = "invalid id!"
        job.source_path.write_text(json.dumps(raw))
    before = evidence(job)
    unit = manager.root / service.service_name(job)
    timestamp = unit.stat().st_mtime_ns
    manager.calls.clear()
    report = collect_status(job.source_path, user_mode=True, runtime_params={})
    assert report["configuration"]["status"] == "INVALID"
    assert (
        report["trigger"]["details"]["installed_execution_identity"] == f"{job.job_id}-{job.scope}"
    )
    assert evidence(job) == before
    assert unit.stat().st_mtime_ns == timestamp
    assert manager.mutations() == []


def test_legacy_inspection_never_migrates(installation):
    job, manager = installation
    path = seed_legacy(job, manager)
    original = path.read_bytes()
    report = trigger_status(job)
    assert report["monitor"] == "UPDATE REQUIRED"
    assert "Legacy" in " ".join(report["diagnostics"])
    assert path.read_bytes() == original
    assert not (manager.root / service.service_name(job)).exists()
    assert manager.mutations() == []


def test_active_process_without_monitor_evidence_is_not_healthy(installation, monkeypatch):
    job, manager = installation
    service.manage_service(job, "install")
    properties = manager.properties
    monkeypatch.setattr(
        "vaultkeep.schedule.status.unit_properties",
        lambda unit, **kwargs: dict(properties(unit), ActiveState="active", SubState="running"),
    )
    report = trigger_status(job)
    assert report["monitor"] != "RUNNING"
    assert recovery_status(job.source_path)["monitor"] == "UNKNOWN"


def test_new_monitor_cannot_overlap_running_legacy_binary(installation, monkeypatch):
    job, manager = installation
    seed_legacy(job, manager)
    monkeypatch.setattr(
        "vaultkeep.trigger.preflight.runtime_preflight", lambda *_: pytest.fail("preflight ran")
    )
    monitor = Monitor(
        job,
        store=StateStore(state_root(job.scope)),
        provider=FakeProvider(),
        controller=FakeController(),
        notifier=RecordingNotifier(),
    )
    with (
        MonitorLock(monitor_lock_path(job.scope)),
        pytest.raises(BlockedError, match="another monitor"),
    ):
        monitor.run()


def test_cli_update_accepts_changed_job_id_and_yaml_parameters(installation, monkeypatch):
    job, _manager = installation
    service.manage_service(job, "install")
    store = StateStore(state_root(job.scope))
    store.write_instance(InstanceState(instance_id=job.job_id))
    before = evidence(job)
    raw = json.loads(job.source_path.read_text())
    raw["job"]["id"] = "new-id"
    raw["destination"]["root"] += "/{repo}"
    raw["trigger"]["params"] = {"repo": "nas-b"}
    job.source_path.write_text(json.dumps(raw))
    preflights = []
    monkeypatch.setattr("vaultkeep.trigger.cli.validate_vaultkeep_executable", lambda _: None)
    monkeypatch.setattr(
        "vaultkeep.trigger.cli.runtime_preflight",
        lambda config, *_, **__: preflights.append(config),
    )
    assert main(["--user", "--config", str(job.source_path), "trigger", "update"]) == 0
    installed = service.find_service(job.source_path)
    assert preflights[0].job_id == "new-id"
    assert installed.unit == service.service_name(job)
    assert installed.identity.job_id == "new-id"
    assert installed.identity.params == {"repo": "nas-b"}
    assert installed.identity.scope == load_config(job.source_path).scope
    assert evidence(job) == before


def test_failed_activation_does_not_retire_a_new_identity_with_active_backup(installation):
    job, manager = installation
    seed_legacy(job, manager)
    desired = changed(job)
    backup = JobLock(
        job_lock_path(
            root=user_workflow_paths().lock_root,
            job_id=desired.job_id,
            identity_hash=desired.scope,
        )
    )

    def fail(args):
        if args[0] == "enable":
            backup.acquire()
            return True
        return False

    manager.fail = fail
    try:
        with pytest.raises(BlockedError, match="active operation"):
            service.manage_service(desired, "update")
        installed = service.find_service(job.source_path)
        assert installed.identity.scope == desired.scope
        assert len(list(manager.root.glob("*.service"))) == 1
    finally:
        backup.release()


def test_disabled_yaml_does_not_hide_an_installed_monitor_without_state(installation, monkeypatch):
    job, _ = installation
    service.manage_service(job, "install")
    monkeypatch.setattr(
        "vaultkeep.trigger.status.RealSystemdController.unit_state", lambda *_: None
    )
    report = trigger_status(replace(job, instances=()), verbose=True)
    assert report is not None
    assert report["monitor"] != "RUNNING"
    assert report["details"]["installed_execution_identity"] == f"{job.job_id}-{job.scope}"
