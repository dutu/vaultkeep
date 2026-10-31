"""Recovery after abrupt process death uses only durable destination intent."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.helpers.backup import job as job
from tests.helpers.backup import run
from vaultkeep.backup import workflow
from vaultkeep.destination.discovery import discover_backups
from vaultkeep.destination.pending import PRUNE_MARKER_NAME


@pytest.mark.parametrize("stage", ["before_publish", "archive", "checksum", "manifest", "marker"])
def test_process_death_is_resumable(job, monkeypatch, stage):
    old = run(job).archive_path
    (job.parent / "source" / "data").write_bytes(b"changed")
    with monkeypatch.context() as patch:
        patch.setattr(workflow, "execute_prune_plan", lambda *args: ())
        survivor = run(job).archive_path
    target = {
        "archive": old.name,
        "checksum": old.name + ".sha256",
        "manifest": next(old.parent.glob("*.json")).name,
        "marker": PRUNE_MARKER_NAME,
        "before_publish": "",
    }[stage]
    script = r"""
import os
import sys
from pathlib import Path
from vaultkeep.backup.workflow import prune_backups
from vaultkeep.destination import pending
original = os.unlink
name = sys.argv[2]
if not name:
    pending.publish_noreplace = lambda *args: os._exit(91)
def unlink(path, *args, **kwargs):
    original(path, *args, **kwargs)
    if str(path) == name:
        os._exit(91)
os.unlink = unlink
prune_backups(Path(sys.argv[1]), dry_run=False, user_mode=True)
"""
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[2] / "src"))
    result = subprocess.run(
        [sys.executable, "-c", script, str(job), target], env=env, capture_output=True, timeout=30
    )
    assert result.returncode == 91, result.stderr.decode()
    facts = discover_backups(workflow.load_validated_config(job), metadata_only=True)
    assert not facts.malformed
    if stage == "before_publish":
        assert old.is_file() and len(facts.backups) == 2 and not facts.pending
    elif stage == "marker":
        assert facts.orphans == (old.parent,) and not facts.pending
    else:
        assert len(facts.pending) == 1 and not old.exists()
        assert (old.parent / PRUNE_MARKER_NAME).is_file()
    assert workflow.prune_backups(job, dry_run=False, user_mode=True).removed == 1
    assert not old.parent.exists() and survivor.is_file()
