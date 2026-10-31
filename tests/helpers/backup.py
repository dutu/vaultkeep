"""Disposable backup workflows with fake archive bytes and real metadata."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from vaultkeep.archive.base import ArchiveArtifact, ArchiveBuildRequest
from vaultkeep.archive.checksums import calculate_archive_sha256, write_checksum_sidecar
from vaultkeep.backup import workflow


@pytest.fixture
def job(tmp_path: Path, valid_config: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> Path:
    source = tmp_path / "source"
    source.mkdir()
    (source / "data").write_bytes(b"source data")
    destination = tmp_path / "backups"
    destination.mkdir()
    valid_config["sources"][0].update(path=str(source))
    valid_config["destination"].update(root=str(destination))
    config_path = tmp_path / "app.yaml"
    config_path.write_text(json.dumps(valid_config))

    def build(request: ArchiveBuildRequest, **kwargs: Any) -> ArchiveArtifact:
        request.archive_path.write_bytes(b"archive")
        digest = calculate_archive_sha256(request.archive_path)
        write_checksum_sidecar(request.archive_path, request.checksum_path, digest)
        return ArchiveArtifact(
            archive_format=request.archive_format,
            archive_path=request.archive_path,
            checksum_path=request.checksum_path,
            sha256=digest,
            size=request.archive_path.stat().st_size,
            source_digest=request.expected_source_digest,
        )

    monkeypatch.setattr(workflow, "build_archive", build)
    # Cleanup tests create placeholder archive bytes rather than compressed TARs.
    monkeypatch.setattr(workflow, "verify_tar_zstd", lambda *args, **kwargs: None)
    return config_path


def run(job):
    return workflow.run_backup(
        job,
        paths=workflow.WorkflowPaths(
            job.parent / "state", job.parent / "cache", job.parent / "locks"
        ),
        user_mode=True,
    )


def configure(job, **changes):
    document = json.loads(job.read_text())
    document.update(changes)
    job.write_text(json.dumps(document))
    return workflow.load_validated_config(job)


def offline(job):
    return configure(
        job,
        consistency={"mode": "application_offline"},
        application={
            "name": "bisq",
            "data_dir": str(job.parent / "source"),
            "matcher": {"entrypoints": [{"kind": "java_main", "value": "test.App"}]},
        },
    )
