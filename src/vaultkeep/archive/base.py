"""Shared archive models and v1 tool paths."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from vaultkeep.sources.entries import SourceSnapshot

ArchiveFormat = Literal["tar.zst", "tar.7z"]


@dataclass(frozen=True, slots=True)
class ArchiveTools:
    """Absolute Debian archive-tool paths."""

    tar: Path = Path("/usr/bin/tar")
    zstd: Path = Path("/usr/bin/zstd")
    seven_zip: Path = Path("/usr/bin/7z")


@dataclass(frozen=True, slots=True)
class ArchiveBuildRequest:
    """Inputs required to build one archive and checksum inside staging."""

    snapshot: SourceSnapshot
    expected_source_digest: str
    archive_format: ArchiveFormat
    compression_level: int
    archive_path: Path
    checksum_path: Path
    job_id: str
    job_identity_hash: str
    backup_id: str
    local_temp_root: Path = Path("/var/lib/vaultkeep/tmp")
    tools: ArchiveTools = ArchiveTools()


@dataclass(frozen=True, slots=True)
class ArchiveArtifact:
    """Verified archive and checksum facts produced before manifest creation."""

    archive_format: ArchiveFormat
    archive_path: Path
    checksum_path: Path
    sha256: str
    size: int
    source_digest: str
