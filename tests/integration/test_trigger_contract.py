"""Exercise Trigger's worker against a real wheel-installed core and disposable job."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def test_installed_worker_core_contract(tmp_path: Path, valid_config: dict) -> None:
    root = Path(__file__).resolve().parents[2]
    wheelhouse = os.environ.get("VAULTKEEP_TEST_WHEELHOUSE", "/tmp/vaultkeep-wheelhouse")
    environment = dict(
        os.environ, PIP_NO_INDEX="1", PIP_FIND_LINKS=wheelhouse, PIP_DISABLE_PIP_VERSION_CHECK="1"
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
    config = dict(valid_config)
    config["sources"] = [
        {"path": str(source), "archive_path_mode": "prefix", "archive_prefix": "source"}
    ]
    config["destination"] = {
        "root": str(destination / "{NETWORK}"),
        "name_template": "backup-{job}-{timestamp_utc:%Y%m%dT%H%M%SZ}",
    }
    config["hooks"] = {}
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
import sys
from dataclasses import replace
from importlib.metadata import version
from pathlib import Path

from vaultkeep.state.identity import job_identity_hash
from vaultkeep.trigger.childproc import RealChildRunner
from vaultkeep.trigger.config import JobSpec, load_config
from vaultkeep.trigger.errors import ConfigurationError
from vaultkeep.trigger.executor import exec_worker
from vaultkeep.trigger.models import AttemptOutcome, Request, new_uuid, utc_now
from vaultkeep.trigger.preflight import validate_jobs, runtime_preflight
from vaultkeep.trigger.state import StateStore

base, job, binary = map(Path, sys.argv[1:])
runtime = base / 'runtime'
runtime.mkdir(mode=0o700)
env = {'HOME': str(base), 'XDG_RUNTIME_DIR': str(runtime),
       'XDG_STATE_HOME': str(base / 'core-state'), 'XDG_CACHE_HOME': str(base / 'cache'),
       'PATH': '/usr/local/bin:/usr/bin:/bin'}
params = {'NETWORK': 'testnet'}
argv = [str(binary), *JobSpec(config=job, params=params).argv()]
runner = RealChildRunner()
core_version = version('vaultkeep')
assert runner.run([str(binary), 'trigger', '--version'], env=env).stdout_tail == core_version
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
request = Request(request_id=new_uuid(), attempt_id=new_uuid(), instance_id='test-instance',
    created_at=now, confirmed_at=now, boot_id=None, config_fingerprint='disposable',
    app='electrum', data_dir=str(base / 'app'),
    matcher={'exe_realpath': None,
             'entrypoints': [{'kind': 'script', 'value': '/disposable/wallet'}],
             'argv_tokens': [], 'cmdline_regex': None, 'env_eq': {}},
    job_config=str(job), job_params=params, vaultkeep=str(binary), environment=env,
    preflight_version=core_version, deadline_seconds=None)

def attempt(expected):
    current = replace(request, request_id=new_uuid(), attempt_id=new_uuid())
    store.write_request(current)
    result = exec_worker(current.attempt_id, store=store, provider=NoProcesses())
    receipt = store.read_receipt(current.attempt_id)
    assert result.outcome == expected, (result, receipt)
    assert receipt.vaultkeep_version == core_version
    return current, receipt

current, receipt = attempt(AttemptOutcome.CREATED)
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
# Errors from real core preserve its error code in a failed receipt.
job.unlink()
_, receipt = attempt(AttemptOutcome.BACKUP_FAILED)
assert receipt.exit_code == 3
print('installed contract passed')
"""
