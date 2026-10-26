"""Exercise Trigger's worker against a real wheel-installed core and disposable job."""

from __future__ import annotations

import json
import os
import pwd
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("backup_mode", ["combined", "per_file"])
def test_installed_worker_core_contract(
    tmp_path: Path, valid_config: dict, backup_mode: str, installer_wheelhouse: Path
) -> None:
    root = Path(__file__).resolve().parents[2]
    environment = dict(
        os.environ,
        PIP_NO_INDEX="1",
        PIP_FIND_LINKS=str(installer_wheelhouse),
        PIP_DISABLE_PIP_VERSION_CHECK="1",
    )
    venv = tmp_path / "installed"
    subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
    python = str(venv / "bin/python")
    for args in (
        ["--require-hashes", "--only-binary=:all:", "-r", str(root / "requirements.lock")],
        ["--no-deps", "--no-build-isolation", str(root)],
    ):
        subprocess.run(
            [python, "-m", "pip", "install", *args],
            env=environment,
            check=True,
            capture_output=True,
            text=True,
        )
    source = tmp_path / "source"
    destination = tmp_path / "backups"
    source.mkdir()
    destination.mkdir()
    (destination / "testnet").mkdir()
    (source / "payload.txt").write_text("disposable contract test\n")
    (source / "second-wallet").write_text("another disposable file\n")
    config = dict(valid_config)
    config["backup_mode"] = backup_mode
    config["sources"] = [
        {"path": str(source), "archive_path_mode": "prefix", "archive_prefix": "source"}
    ]
    config["destination"] = {
        "root": str(destination / "{NETWORK}"),
        "name_template": "backup-{job}-{timestamp_utc:%Y%m%dT%H%M%S%fZ}",
    }
    config["application"] = {
        "name": "electrum",
        "user": pwd.getpwuid(os.geteuid()).pw_name,
        "data_dir": str(source),
        "matcher": {"entrypoints": [{"kind": "script", "value": "/disposable/wallet"}]},
    }
    config["hooks"] = {}
    config["schedule"] = {"enabled": False}
    job = tmp_path / "app.yaml"
    # JSON is a subset of the supported YAML 1.2 syntax.
    job.write_text(json.dumps(config))
    result = subprocess.run(
        [python, "-", str(tmp_path), str(job), str(venv / "bin/vaultkeep")],
        input=CONTRACT,
        text=True,
        capture_output=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "installed contract passed" in result.stdout


CONTRACT = r"""
import fcntl
import json
import os
import shlex
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from dataclasses import replace
from importlib.metadata import version
from io import StringIO
from pathlib import Path

from vaultkeep.cli.output import Console
from vaultkeep.backup.progress import read_progress
from vaultkeep.state.identity import job_identity_hash
from vaultkeep.trigger.childproc import RealChildRunner
from vaultkeep.trigger.config import JobSpec, load_config
from vaultkeep.trigger.environment import build_child_environment
from vaultkeep.trigger.errors import ConfigurationError
from vaultkeep.trigger.executor import exec_worker
from vaultkeep.trigger.models import AttemptOutcome, Request, new_uuid, utc_now
from vaultkeep.trigger.preflight import validate_jobs, runtime_preflight
from vaultkeep.trigger.state import StateStore
from vaultkeep.backup.records import read_history
from vaultkeep.backup.paths import WorkflowPaths
from vaultkeep.backup.status import backup_status
from vaultkeep.cli.status import print_status
from vaultkeep.schedule import TimerManager

base, job, binary = map(Path, sys.argv[1:])
runtime = base / 'runtime'
runtime.mkdir(mode=0o700)
env = {'HOME': str(base), 'XDG_RUNTIME_DIR': str(runtime),
       'XDG_STATE_HOME': str(base / 'core-state'), 'XDG_CACHE_HOME': str(base / 'cache'),
       'PATH': '/usr/local/bin:/usr/bin:/bin'}
assert build_child_environment(home=base, environ=env) == env
params = {'NETWORK': 'testnet'}
identity = job_identity_hash(job, 'app', runtime_params=params)
paths = WorkflowPaths(base / 'core-state/vaultkeep/jobs',
    base / 'cache/vaultkeep/tmp', runtime / 'vaultkeep/locks')
execution_dir = paths.state_root / ('app-' + identity)
argv = [str(binary), *JobSpec(config=job, params=params).argv()]
runner = RealChildRunner()
core_version = version('vaultkeep')
assert runner.run([str(binary), '--version'], env=env).stdout_tail == core_version
schema = runner.run([*argv, 'validate', '--schema-only'], env=env)
assert schema.exit_code == 0, schema
validation = runner.run([*argv, 'validate'], env=env)
assert validation.exit_code == 0, validation
# schema-only accepts a structurally valid missing source; full validation refuses it.
original = job.read_text()
data = json.loads(original)
data['sources'][0]['path'] = str(base / 'missing-source')
job.write_text(json.dumps(data))
schema = runner.run([*argv, 'validate', '--schema-only'], env=env)
assert schema.exit_code == 0, schema
assert runner.run([*argv, 'validate'], env=env).exit_code != 0
job.write_text(original)
assert not (base / 'core-state').exists()

class NoProcesses:
    def snapshot(self): return []
    def read_env(self, *args): return {}
    def alive(self, *args): return False

store = StateStore(base / 'trigger-state')
store.ensure()
now = utc_now()
instance = load_config(job).instances[0]
request = Request(request_id=new_uuid(), attempt_id=new_uuid(), instance_id='test-instance',
    created_at=now, confirmed_at=now, boot_id=None, config_fingerprint='disposable',
    app=instance.app, data_dir=str(instance.data_dir),
    matcher={'exe_realpath': None,
             'entrypoints': [{'kind': 'script', 'value': '/disposable/wallet'}],
             'argv_tokens': [], 'cmdline_regex': None, 'env_eq': {}},
    job_config=str(job), job_params=params, vaultkeep=str(binary), environment=env,
    preflight_version=core_version, deadline_seconds=None,
    owner_uid=instance.owner_uid, execution_policy=instance.execution_policy)

def attempt(expected):
    current = replace(request, request_id=new_uuid(), attempt_id=new_uuid())
    store.write_request(current)
    result = exec_worker(current.attempt_id, store=store, provider=NoProcesses())
    receipt = store.read_receipt(current.attempt_id)
    assert result.outcome == expected, (result, receipt)
    assert receipt.vaultkeep_version == core_version
    if expected in (AttemptOutcome.CREATED, AttemptOutcome.UNCHANGED):
        evidence = read_history(execution_dir / 'execution.json', 'app', identity)
        progress = read_progress(execution_dir / 'progress.json', evidence.previous.execution_id)
        assert progress is not None and progress.state == 'finished'
        assert backup_status(paths, 'app', identity)['previous']['result'] == (
            'SUCCESS' if expected is AttemptOutcome.CREATED else 'UNCHANGED')
    return current, receipt

# Pause after a real workflow phase is published. The wrapper only makes the
# installed engine's archive phase long enough to inspect across processes.
ready, release = base / 'phase-ready', base / 'phase-release'
wrapper = base / 'slow-vaultkeep'
wrapper.write_text(f'''#!{sys.executable}
import time
from pathlib import Path
from vaultkeep.backup.progress import ProgressReporter
from vaultkeep.cli import main
original = ProgressReporter.report
ready, release = Path({str(ready)!r}), Path({str(release)!r})
def report(self, message):
    original(self, message)
    if message == 'Creating and compressing archive' and not release.exists():
        ready.touch()
        deadline = time.monotonic() + 15
        while not release.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert release.exists(), 'test did not release archive phase'
ProgressReporter.report = report
from vaultkeep.destination import finalize
from vaultkeep.backup import workflow
failure = Path({str(base / 'force-failure')!r})
rename, build, cleanup = finalize._rename_noreplace, workflow.build_archive, workflow.shutil.rmtree
prune = workflow.execute_prune_plan
def checked_rename(source, target):
    if (failure.exists() and failure.read_text() == 'finalize'
        and source.name.startswith('~$.partial')):
        raise PermissionError(13, 'Permission denied', str(target))
    return rename(source, target)
def checked_build(*args, **kwargs):
    if failure.exists() and failure.read_text() == 'cleanup':
        raise OSError('Archive failed')
    return build(*args, **kwargs)
def checked_cleanup(path, *args, **kwargs):
    if (failure.exists() and failure.read_text() == 'cleanup'
        and Path(path).name.startswith('~$.partial')):
        raise PermissionError('cleanup denied')
    return cleanup(path, *args, **kwargs)
def checked_prune(*args, **kwargs):
    if failure.exists() and failure.read_text() == 'retention':
        raise OSError('retention cleanup denied')
    return prune(*args, **kwargs)
workflow.execute_prune_plan = checked_prune
finalize._rename_noreplace = checked_rename
workflow.build_archive = checked_build
workflow.shutil.rmtree = checked_cleanup
raise SystemExit(main())
''')
wrapper.chmod(0o700)
request = replace(request, vaultkeep=str(wrapper))
with ThreadPoolExecutor(max_workers=1) as pool:
    future = pool.submit(attempt, AttemptOutcome.CREATED)
    try:
        deadline = time.monotonic() + 15
        while not ready.exists() and time.monotonic() < deadline and not future.done():
            time.sleep(0.01)
        if future.done():
            future.result()
        assert ready.exists(), 'backup did not reach its archive phase'
        attempt_id, = store.list_attempt_ids()
        evidence = read_history(execution_dir / 'execution.json', 'app', identity)
        progress = read_progress(execution_dir / 'progress.json', evidence.current.execution_id)
        assert progress is not None and progress.state == 'running'
        assert progress.phase == 'Creating and compressing archive'
        report = StringIO()
        with redirect_stdout(report):
            print_status({'job_id': 'app', 'backup': backup_status(paths, 'app', identity)},
                Console(no_color=True))
        assert 'RUNNING: Creating and compressing archive (' in report.getvalue(), report.getvalue()
        assert ' / ' in report.getvalue() and ' elapsed)' in report.getvalue()
    finally:
        release.touch()
    current, receipt = future.result(timeout=30)
assert receipt.marker_line == 'run: created'
# Re-executing the exact attempt never invokes another backup.
replayed = exec_worker(current.attempt_id, store=store, provider=NoProcesses())
assert replayed.outcome == AttemptOutcome.CREATED
_, receipt = attempt(AttemptOutcome.UNCHANGED)
assert receipt.marker_line == 'run: unchanged'
# An external/manual run holds the exact namespace passed to the Trigger child.
identity = job_identity_hash(job, 'app', runtime_params=params)
lock = runtime / 'vaultkeep/locks' / ('app-' + identity + '.lock')
with lock.open('rb') as held:
    fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
    _, receipt = attempt(AttemptOutcome.DEFERRED)
    assert receipt.exit_code == 10
# A rendered timer service and a manual command use the same engine and history.
service = TimerManager(user_mode=True)._service_content(job, 'app', params)
scheduled = shlex.split(next(line[10:] for line in service.splitlines()
    if line.startswith('ExecStart=')))
assert scheduled[1:] == argv[1:] + ['run']
for mode in ('manual', 'schedule', 'trigger'):
    for expected in ('SUCCESS', 'UNCHANGED', 'FAILED', 'CLEANUP_FAILED', 'CLEANUP_WARNING'):
        failure = base / 'force-failure'
        if expected in ('FAILED', 'CLEANUP_FAILED'):
            failure.write_text('finalize' if expected == 'FAILED' else 'cleanup')
        elif expected == 'CLEANUP_WARNING':
            failure.write_text('retention')
        elif failure.exists():
            failure.unlink()
        if expected != 'UNCHANGED':
            (base / 'source/payload.txt').write_text(mode + expected)
        if mode == 'trigger':
            outcome = (AttemptOutcome.CREATED if expected in ('SUCCESS', 'CLEANUP_WARNING')
                else AttemptOutcome.UNCHANGED if expected == 'UNCHANGED'
                else AttemptOutcome.BACKUP_FAILED)
            _, receipt = attempt(outcome)
            code = receipt.exit_code
        else:
            command = [str(wrapper), *(scheduled[1:] if mode == 'schedule' else [*argv[1:], 'run'])]
            child = runner.run(command, env=env)
            code = child.exit_code
        observed = backup_status(paths, 'app', identity)['previous']
        assert observed['result'] == ('FAILED' if 'FAILED' in expected
            else 'SUCCESS' if expected == 'CLEANUP_WARNING' else expected), observed
        assert observed['exit_code'] == code
        # Check the installed user-facing command with exactly the child's mode,
        # YAML path, parameters, and XDG state namespace, not just in-process reads.
        inspection = subprocess.run([*argv, 'status', '--json'], env=env,
            capture_output=True, text=True, timeout=10)
        assert inspection.returncode == 0, inspection.stdout + inspection.stderr
        visible = json.loads(inspection.stdout)['backup']
        assert visible['activity'] == 'IDLE', visible
        assert visible['previous'] == observed, visible
        assert visible['previous']['completed_at'] is not None
        assert visible['previous']['duration_seconds'] >= 0
        if expected == 'FAILED':
            assert observed['reason'] == 'Permission denied during finalization', observed
        if expected == 'CLEANUP_WARNING':
            assert observed['cleanup_errors']
            assert 'retention cleanup denied' in observed['cleanup_errors'][0]
            assert code == 0
        if expected == 'CLEANUP_FAILED':
            assert observed['reason'] == 'Archive failed', observed
            assert 'cleanup denied' in Path(observed['log_path']).read_text()
# Errors from real core preserve its error code in a failed receipt.
job.unlink()
_, receipt = attempt(AttemptOutcome.BACKUP_FAILED)
assert receipt.exit_code == 3
print('installed contract passed')
"""
