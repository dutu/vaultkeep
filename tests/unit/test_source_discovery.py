"""Tests for deterministic source traversal and exclusions."""

from __future__ import annotations

import os
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from vaultkeep.config import JobConfig
from vaultkeep.errors import SourceDiscoveryError
from vaultkeep.sources import SourceEntryType, discover_sources
from vaultkeep.sources import discovery as discovery_module
from vaultkeep.sources.exclusions import compile_exclusions


def _job(
    valid_config: dict[str, Any],
    sources: list[dict[str, object]],
    *,
    global_exclude: list[str] | None = None,
    source_options: dict[str, bool] | None = None,
) -> JobConfig:
    candidate = deepcopy(valid_config)
    candidate["sources"] = sources
    candidate["exclude"] = global_exclude or []
    if source_options is not None:
        candidate["source_options"] = source_options
    return JobConfig.model_validate(candidate)


@pytest.mark.parametrize(
    ("pattern", "path", "is_directory", "expected"),
    [
        ("*.tmp", "nested/file.tmp", False, True),
        ("*.tmp", "nested/file.txt", False, False),
        ("cache/", "cache", True, True),
        ("cache/", "nested/cache", True, True),
        ("docs/*.txt", "docs/readme.txt", False, True),
        ("docs/*.txt", "nested/docs/readme.txt", False, False),
        ("**/.cache/**", "a/.cache/file", False, True),
    ],
)
def test_gitwildmatch_contract(
    pattern: str,
    path: str,
    is_directory: bool,
    expected: bool,
) -> None:
    matcher = compile_exclusions((pattern,))

    assert matcher.matches(path, is_directory=is_directory) is expected


def test_traversal_includes_empty_directories_and_applies_exclusions(
    tmp_path: Path, valid_config: dict[str, Any]
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "keep.txt").write_text("keep", encoding="utf-8")
    (source / "discard.tmp").write_text("discard", encoding="utf-8")
    (source / "empty").mkdir()
    cache = source / "cache"
    cache.mkdir()
    (cache / "data.bin").write_bytes(b"cache")
    nested = source / "nested"
    nested.mkdir()
    (nested / "-leading").write_text("leading", encoding="utf-8")
    newline_path = nested / "line\nbreak"
    if os.name != "nt":
        newline_path.write_text("newline", encoding="utf-8")

    config = _job(
        valid_config,
        [{"path": str(source), "exclude": ["cache/"]}],
        global_exclude=["*.tmp"],
    )
    snapshot = discover_sources(config)
    paths = [entry.absolute_path for entry in snapshot.entries]

    assert source in paths
    assert source / "keep.txt" in paths
    assert source / "empty" in paths
    assert nested / "-leading" in paths
    if os.name != "nt":
        assert newline_path in paths
    assert source / "discard.tmp" not in paths
    assert cache not in paths
    assert cache / "data.bin" not in paths
    assert [entry.raw_archive_path for entry in snapshot.entries] == sorted(
        entry.raw_archive_path for entry in snapshot.entries
    )


def test_individual_file_can_be_excluded(tmp_path: Path, valid_config: dict[str, Any]) -> None:
    source = tmp_path / "only.tmp"
    source.write_text("content", encoding="utf-8")
    config = _job(
        valid_config,
        [{"path": str(source)}],
        global_exclude=["*.tmp"],
    )

    with pytest.raises(SourceDiscoveryError, match="removed every"):
        discover_sources(config)


def test_missing_source_is_fatal_by_default(tmp_path: Path, valid_config: dict[str, Any]) -> None:
    config = _job(valid_config, [{"path": str(tmp_path / "missing")}])

    with pytest.raises(SourceDiscoveryError, match="does not exist"):
        discover_sources(config)


def test_missing_source_can_be_ignored_when_another_source_exists(
    tmp_path: Path, valid_config: dict[str, Any]
) -> None:
    existing = tmp_path / "existing.txt"
    existing.write_text("content", encoding="utf-8")
    config = _job(
        valid_config,
        [{"path": str(tmp_path / "missing")}, {"path": str(existing)}],
        source_options={
            "follow_symlinks": False,
            "cross_filesystems": False,
            "ignore_missing": True,
        },
    )

    snapshot = discover_sources(config)

    assert [entry.absolute_path for entry in snapshot.entries] == [existing]


def test_symlink_is_preserved_without_following(
    tmp_path: Path, valid_config: dict[str, Any]
) -> None:
    target = tmp_path / "target.txt"
    target.write_text("target", encoding="utf-8")
    link = tmp_path / "link"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("Creating symlinks is not available")
    config = _job(valid_config, [{"path": str(link)}])

    entry = discover_sources(config).entries[0]

    assert entry.entry_type is SourceEntryType.SYMLINK
    assert entry.link_target == os.readlink(link)


def test_symlink_can_be_followed(tmp_path: Path, valid_config: dict[str, Any]) -> None:
    target = tmp_path / "target.txt"
    target.write_text("target", encoding="utf-8")
    link = tmp_path / "link"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("Creating symlinks is not available")
    config = _job(
        valid_config,
        [{"path": str(link)}],
        source_options={
            "follow_symlinks": True,
            "cross_filesystems": False,
            "ignore_missing": False,
        },
    )

    entry = discover_sources(config).entries[0]

    assert entry.entry_type is SourceEntryType.FILE
    assert entry.link_target is None


def test_hard_link_relationship_is_captured(tmp_path: Path, valid_config: dict[str, Any]) -> None:
    source = tmp_path / "source"
    source.mkdir()
    first = source / "first"
    second = source / "second"
    first.write_text("content", encoding="utf-8")
    try:
        os.link(first, second)
    except OSError:
        pytest.skip("Creating hard links is not available")
    config = _job(valid_config, [{"path": str(source)}])

    entries = {
        entry.absolute_path: entry
        for entry in discover_sources(config).entries
        if entry.entry_type is SourceEntryType.FILE
    }

    assert entries[first].device == entries[second].device
    assert entries[first].inode == entries[second].inode


def test_directory_symlink_cycle_is_rejected(tmp_path: Path, valid_config: dict[str, Any]) -> None:
    source = tmp_path / "source"
    source.mkdir()
    loop = source / "loop"
    try:
        loop.symlink_to(source, target_is_directory=True)
    except OSError:
        pytest.skip("Creating directory symlinks is not available")
    config = _job(
        valid_config,
        [{"path": str(source)}],
        source_options={
            "follow_symlinks": True,
            "cross_filesystems": False,
            "ignore_missing": False,
        },
    )

    with pytest.raises(SourceDiscoveryError, match="cycle"):
        discover_sources(config)


def test_cross_filesystem_directory_is_included_without_descending(
    tmp_path: Path,
    valid_config: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    mounted = source / "mounted"
    mounted.mkdir(parents=True)
    child = mounted / "child.txt"
    child.write_text("content", encoding="utf-8")
    original_device_id = discovery_module._device_id

    def different_device(path: Path, status: os.stat_result) -> int:
        device = original_device_id(path, status)
        return device + 1 if path == mounted else device

    monkeypatch.setattr(discovery_module, "_device_id", different_device)
    config = _job(valid_config, [{"path": str(source)}])

    paths = [entry.absolute_path for entry in discover_sources(config).entries]

    assert mounted in paths
    assert child not in paths


@pytest.mark.skipif(os.name == "nt", reason="Raw byte filenames are POSIX-only")
def test_non_utf8_filename_round_trips_through_raw_sort_key(
    tmp_path: Path, valid_config: dict[str, Any]
) -> None:
    source_bytes = os.fsencode(tmp_path)
    raw_name = b"non-utf8-\xff"
    descriptor = os.open(
        os.path.join(source_bytes, raw_name),
        os.O_WRONLY | os.O_CREAT,
        0o600,
    )
    os.close(descriptor)
    config = _job(valid_config, [{"path": str(tmp_path)}])

    snapshot = discover_sources(config)

    assert any(entry.raw_archive_path.endswith(raw_name) for entry in snapshot.entries)


@pytest.mark.skipif(os.name == "nt", reason="FIFOs are not available on Windows")
def test_unsupported_entry_type_is_rejected(tmp_path: Path, valid_config: dict[str, Any]) -> None:
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)
    config = _job(valid_config, [{"path": str(fifo)}])

    with pytest.raises(SourceDiscoveryError, match="Unsupported source entry type"):
        discover_sources(config)
