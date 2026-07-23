"""Tests for versioned source and configuration hashing."""

from __future__ import annotations

import os
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from vaultkeep.config import JobConfig
from vaultkeep.errors import SourceChangedError
from vaultkeep.sources import (
    SourceSnapshot,
    calculate_config_fingerprint,
    calculate_source_digest,
    discover_sources,
)
from vaultkeep.sources import hashing as hashing_module


def _job(
    valid_config: dict[str, Any],
    source: Path,
    *,
    exclude: list[str] | None = None,
) -> JobConfig:
    candidate = deepcopy(valid_config)
    candidate["sources"] = [{"path": str(source)}]
    candidate["exclude"] = exclude or []
    return JobConfig.model_validate(candidate)


def _source_digest(valid_config: dict[str, Any], source: Path) -> str:
    return calculate_source_digest(discover_sources(_job(valid_config, source)))


def test_digest_is_independent_of_creation_and_traversal_order(
    tmp_path: Path, valid_config: dict[str, Any]
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    for root, names in ((first, ("z", "a")), (second, ("a", "z"))):
        for name in names:
            (root / name).write_text(name, encoding="utf-8")

    first_snapshot = discover_sources(_job(valid_config, first))
    second_snapshot = discover_sources(_job(valid_config, second))

    # Root paths differ, so compare equivalent snapshots after using the same root.
    first_digest = calculate_source_digest(first_snapshot)
    recreated = first / "z"
    content = recreated.read_text(encoding="utf-8")
    recreated.unlink()
    recreated.write_text(content, encoding="utf-8")
    second_digest = calculate_source_digest(discover_sources(_job(valid_config, first)))

    assert first_digest == second_digest
    assert [entry.raw_archive_path for entry in second_snapshot.entries] == sorted(
        entry.raw_archive_path for entry in second_snapshot.entries
    )


def test_file_content_change_changes_digest(tmp_path: Path, valid_config: dict[str, Any]) -> None:
    source = tmp_path / "source"
    source.mkdir()
    file_path = source / "file"
    file_path.write_text("first", encoding="utf-8")
    before = _source_digest(valid_config, source)

    file_path.write_text("second", encoding="utf-8")
    after = _source_digest(valid_config, source)

    assert before != after


def test_rename_changes_digest(tmp_path: Path, valid_config: dict[str, Any]) -> None:
    source = tmp_path / "source"
    source.mkdir()
    original = source / "original"
    original.write_text("same", encoding="utf-8")
    before = _source_digest(valid_config, source)

    original.rename(source / "renamed")
    after = _source_digest(valid_config, source)

    assert before != after


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits are required")
def test_permission_change_changes_digest(tmp_path: Path, valid_config: dict[str, Any]) -> None:
    source = tmp_path / "file"
    source.write_text("same", encoding="utf-8")
    source.chmod(0o600)
    before = _source_digest(valid_config, source)

    source.chmod(0o640)
    after = _source_digest(valid_config, source)

    assert before != after


def test_modification_time_is_excluded(tmp_path: Path, valid_config: dict[str, Any]) -> None:
    source = tmp_path / "file"
    source.write_text("same", encoding="utf-8")
    before = _source_digest(valid_config, source)
    status = source.stat()

    os.utime(
        source,
        ns=(status.st_atime_ns, status.st_mtime_ns + 2_000_000_000),
    )
    after = _source_digest(valid_config, source)

    assert before == after


def test_excluded_content_does_not_change_digest(
    tmp_path: Path, valid_config: dict[str, Any]
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    included = source / "included"
    excluded = source / "ignored.tmp"
    included.write_text("same", encoding="utf-8")
    excluded.write_text("first", encoding="utf-8")
    config = _job(valid_config, source, exclude=["*.tmp"])
    before = calculate_source_digest(discover_sources(config))

    excluded.write_text("second", encoding="utf-8")
    after = calculate_source_digest(discover_sources(config))

    assert before == after


def test_empty_directory_name_affects_digest(tmp_path: Path, valid_config: dict[str, Any]) -> None:
    source = tmp_path / "source"
    source.mkdir()
    empty = source / "empty"
    empty.mkdir()
    before = _source_digest(valid_config, source)

    empty.rename(source / "renamed")
    after = _source_digest(valid_config, source)

    assert before != after


def test_digest_format_version_is_part_of_digest(
    tmp_path: Path, valid_config: dict[str, Any]
) -> None:
    source = tmp_path / "file"
    source.write_text("content", encoding="utf-8")
    snapshot = discover_sources(_job(valid_config, source))

    assert calculate_source_digest(snapshot, format_version=1) != calculate_source_digest(
        snapshot, format_version=2
    )


def test_digest_versions_must_be_positive(tmp_path: Path, valid_config: dict[str, Any]) -> None:
    source = tmp_path / "file"
    source.write_text("content", encoding="utf-8")
    snapshot = discover_sources(_job(valid_config, source))
    config = JobConfig.model_validate(valid_config)

    with pytest.raises(ValueError):
        calculate_source_digest(snapshot, format_version=0)
    with pytest.raises(ValueError):
        calculate_config_fingerprint(config, format_version=0)


def test_snapshot_rejects_unsorted_and_duplicate_entries(
    tmp_path: Path, valid_config: dict[str, Any]
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "a").write_text("a", encoding="utf-8")
    snapshot = discover_sources(_job(valid_config, source))

    with pytest.raises(ValueError, match="sorted"):
        SourceSnapshot(tuple(reversed(snapshot.entries)))
    with pytest.raises(ValueError, match="unique"):
        SourceSnapshot((snapshot.entries[0], snapshot.entries[0]))


def test_change_after_discovery_is_rejected(tmp_path: Path, valid_config: dict[str, Any]) -> None:
    source = tmp_path / "file"
    source.write_text("first", encoding="utf-8")
    snapshot = discover_sources(_job(valid_config, source))

    source.write_text("changed", encoding="utf-8")

    with pytest.raises(SourceChangedError, match="changed"):
        calculate_source_digest(snapshot)


def test_change_during_hashing_is_rejected(
    tmp_path: Path,
    valid_config: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "file"
    source.write_bytes(b"x" * 1024)
    snapshot = discover_sources(_job(valid_config, source))
    original_read = hashing_module._read_chunk
    changed = False

    def change_mtime_after_read(stream: Any, size: int) -> bytes:
        nonlocal changed
        chunk = original_read(stream, size)
        if not changed:
            changed = True
            status = source.stat()
            os.utime(
                source,
                ns=(status.st_atime_ns, status.st_mtime_ns + 2_000_000_000),
            )
        return chunk

    monkeypatch.setattr(hashing_module, "_read_chunk", change_mtime_after_read)

    with pytest.raises(SourceChangedError, match="changed"):
        calculate_source_digest(snapshot)


@pytest.mark.parametrize(("replacement", "message"), [(b"", "shorter"), (b"x", "longer")])
def test_stream_length_changes_are_rejected(
    tmp_path: Path,
    valid_config: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    replacement: bytes,
    message: str,
) -> None:
    source = tmp_path / "file"
    source.write_bytes(b"content")
    snapshot = discover_sources(_job(valid_config, source))
    original_read = hashing_module._read_chunk

    def replace_selected_read(stream: Any, size: int) -> bytes:
        if replacement == b"" or size == 1:
            return replacement
        return original_read(stream, size)

    monkeypatch.setattr(hashing_module, "_read_chunk", replace_selected_read)

    with pytest.raises(SourceChangedError, match=message):
        calculate_source_digest(snapshot)


def test_symlink_target_change_changes_digest(tmp_path: Path, valid_config: dict[str, Any]) -> None:
    first_target = tmp_path / "first"
    second_target = tmp_path / "second"
    first_target.write_text("same", encoding="utf-8")
    second_target.write_text("same", encoding="utf-8")
    link = tmp_path / "link"
    try:
        link.symlink_to(first_target)
    except OSError:
        pytest.skip("Creating symlinks is not available")
    before = _source_digest(valid_config, link)

    link.unlink()
    link.symlink_to(second_target)
    after = _source_digest(valid_config, link)

    assert before != after


@pytest.mark.skipif(os.name == "nt", reason="Raw byte filenames are POSIX-only")
def test_non_utf8_filename_is_hashed_losslessly(
    tmp_path: Path, valid_config: dict[str, Any]
) -> None:
    raw_name = b"raw-\xff"
    descriptor = os.open(
        os.path.join(os.fsencode(tmp_path), raw_name),
        os.O_WRONLY | os.O_CREAT,
        0o600,
    )
    os.write(descriptor, b"content")
    os.close(descriptor)

    digest = _source_digest(valid_config, tmp_path)

    assert digest.startswith("sha256:")


def test_backup_relevant_configuration_changes_fingerprint(
    valid_config: dict[str, Any],
) -> None:
    original = calculate_config_fingerprint(JobConfig.model_validate(valid_config))
    candidates: list[dict[str, Any]] = []

    source_path = deepcopy(valid_config)
    source_path["sources"][0]["path"] = "/srv/other"
    candidates.append(source_path)

    exclusions = deepcopy(valid_config)
    exclusions["exclude"].append("*.bak")
    candidates.append(exclusions)

    source_options = deepcopy(valid_config)
    source_options["source_options"]["follow_symlinks"] = True
    candidates.append(source_options)

    destination = deepcopy(valid_config)
    destination["destination"]["name_template"] = "changed-{job}-{timestamp_utc:%Y%m%dT%H%M%SZ}"
    candidates.append(destination)

    compression = deepcopy(valid_config)
    compression["archive"]["compression_level"] = 7
    candidates.append(compression)

    encryption = deepcopy(valid_config)
    encryption["archive"]["format"] = "tar.7z"
    encryption["encryption"] = {
        "mode": "password",
        "password_file": "/etc/vaultkeep/secrets/app.passphrase",
    }
    candidates.append(encryption)

    for candidate in candidates:
        assert calculate_config_fingerprint(JobConfig.model_validate(candidate)) != original


def test_config_fingerprint_format_version_is_included(
    valid_config: dict[str, Any],
) -> None:
    config = JobConfig.model_validate(valid_config)

    assert calculate_config_fingerprint(config, format_version=1) != calculate_config_fingerprint(
        config, format_version=2
    )


def test_hard_linked_files_are_hashed(tmp_path: Path, valid_config: dict[str, Any]) -> None:
    source = tmp_path / "source"
    source.mkdir()
    first = source / "first"
    second = source / "second"
    first.write_text("before", encoding="utf-8")
    try:
        os.link(first, second)
    except OSError:
        pytest.skip("Creating hard links is not available")
    before = _source_digest(valid_config, source)

    second.write_text("after!", encoding="utf-8")
    after = _source_digest(valid_config, source)

    assert before != after


@pytest.mark.parametrize("section", ["retention", "schedule", "hooks", "logging"])
def test_operational_configuration_is_excluded_from_fingerprint(
    valid_config: dict[str, Any], section: str
) -> None:
    original = calculate_config_fingerprint(JobConfig.model_validate(valid_config))
    candidate = deepcopy(valid_config)
    if section == "retention":
        candidate[section]["daily"] += 1
    elif section == "schedule":
        candidate[section]["window"] = "02:00-04:00"
    elif section == "hooks":
        candidate[section]["on_success"] = {"command": ["/bin/true"]}
    else:
        candidate[section]["level"] = "debug"

    assert calculate_config_fingerprint(JobConfig.model_validate(candidate)) == original
