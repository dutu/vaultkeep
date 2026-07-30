"""GNU TAR member-list construction and structural validation."""

from __future__ import annotations

import os
from pathlib import Path

from vaultkeep.errors import ArchiveVerificationError
from vaultkeep.sources.entries import SourceEntry, SourceEntryType, SourceSnapshot

_SIMPLE_ESCAPES = {
    ord("a"): 0x07,
    ord("b"): 0x08,
    ord("f"): 0x0C,
    ord("n"): 0x0A,
    ord("r"): 0x0D,
    ord("t"): 0x09,
    ord("v"): 0x0B,
    ord("\\"): 0x5C,
}


def tar_member_input(snapshot: SourceSnapshot) -> bytes:
    """Return the complete raw NUL-delimited GNU TAR input list."""
    if not snapshot.entries:
        raise ValueError("Cannot archive an empty source snapshot")
    members = tuple(entry.raw_archive_path for entry in snapshot.entries)
    if any(not member or member.startswith(b"/") for member in members):
        raise ValueError("TAR member paths must be non-empty and relative")
    if len(members) != len(set(members)):
        raise ValueError("TAR member paths must be unique")
    return b"\0".join(members) + b"\0"


def tar_filesystem_input(snapshot: SourceSnapshot) -> bytes:
    """Return the complete raw NUL-delimited filesystem paths read by GNU TAR."""
    if not snapshot.entries:
        raise ValueError("Cannot archive an empty source snapshot")
    paths = tuple(os.fsencode(entry.absolute_path) for entry in snapshot.entries)
    if any(not path or not Path(os.fsdecode(path)).is_absolute() for path in paths):
        raise ValueError("TAR filesystem paths must be non-empty and absolute")
    if len(paths) != len(set(paths)):
        raise ValueError("TAR filesystem paths must be unique")
    return b"\0".join(paths) + b"\0"


def tar_path_transforms(snapshot: SourceSnapshot) -> tuple[str, ...]:
    """Return GNU TAR transforms from filesystem paths to archive member paths."""
    if not snapshot.entries:
        raise ValueError("Cannot archive an empty source snapshot")

    transforms: list[str] = []
    source_indexes = sorted({entry.source_index for entry in snapshot.entries})
    for source_index in source_indexes:
        entries = tuple(entry for entry in snapshot.entries if entry.source_index == source_index)
        mapping = _infer_common_path_mapping(entries)
        if mapping is None:
            transforms.extend(_exact_entry_transforms(entries))
            continue
        filesystem_prefix, archive_prefix = mapping
        transforms.extend(_prefix_transforms(filesystem_prefix, archive_prefix))
    return tuple(transforms)


def expected_tar_members(snapshot: SourceSnapshot) -> tuple[bytes, ...]:
    """Return normalized raw members expected from GNU TAR listing."""
    return tuple(entry.raw_archive_path for entry in snapshot.entries)


def parse_gnu_tar_listing(output: bytes) -> tuple[bytes, ...]:
    """Decode GNU TAR's escape quoting into lossless raw member names."""
    if not output:
        raise ArchiveVerificationError("TAR listing produced no members")
    lines = output.splitlines()
    members = tuple(_decode_escape_quoting(line) for line in lines)
    validate_tar_members(members)
    return members


def validate_tar_members(members: tuple[bytes, ...]) -> None:
    """Reject absolute, traversal, empty, and duplicate TAR member paths."""
    seen: set[bytes] = set()
    for member in members:
        normalized = member[:-1] if member.endswith(b"/") else member
        if not normalized:
            raise ArchiveVerificationError("TAR contains an empty member path")
        if normalized.startswith(b"/"):
            raise ArchiveVerificationError("TAR contains an absolute member path")
        if b".." in normalized.split(b"/"):
            raise ArchiveVerificationError("TAR contains a parent-traversal member path")
        if normalized in seen:
            raise ArchiveVerificationError("TAR contains duplicate member paths")
        seen.add(normalized)


def compare_tar_members(snapshot: SourceSnapshot, actual: tuple[bytes, ...]) -> None:
    """Require listing membership to equal the immutable source snapshot."""
    expected = set(expected_tar_members(snapshot))
    normalized_actual = {member[:-1] if member.endswith(b"/") else member for member in actual}
    if expected != normalized_actual or len(actual) != len(expected):
        raise ArchiveVerificationError("TAR member list does not match the source snapshot")


def snapshot_uses_followed_symlinks(snapshot: SourceSnapshot) -> bool:
    """Return whether GNU TAR must dereference explicitly listed symlinks."""
    return any(
        entry.followed_symlink and entry.entry_type is not SourceEntryType.SYMLINK
        for entry in snapshot.entries
    )


def _infer_common_path_mapping(entries: tuple[SourceEntry, ...]) -> tuple[str, str] | None:
    absolute_paths = tuple(Path(entry.absolute_path) for entry in entries)
    common = Path(os.path.commonpath(tuple(os.fspath(path) for path in absolute_paths)))
    candidates = (common, *common.parents)
    for filesystem_prefix in candidates:
        archive_prefix = _archive_prefix_for_candidate(entries[0], filesystem_prefix)
        if archive_prefix is None:
            continue
        if all(
            _archive_path_for_candidate(entry, filesystem_prefix, archive_prefix)
            == entry.archive_path
            for entry in entries
        ):
            return os.fspath(filesystem_prefix), archive_prefix
    return None


def _archive_prefix_for_candidate(entry: SourceEntry, filesystem_prefix: Path) -> str | None:
    try:
        relative = entry.absolute_path.relative_to(filesystem_prefix)
    except ValueError:
        return None
    relative_parts = _posix_parts(relative)
    archive_parts = tuple(entry.archive_path.split("/"))
    if not relative_parts:
        return entry.archive_path
    if len(archive_parts) < len(relative_parts):
        return None
    if tuple(archive_parts[-len(relative_parts) :]) != relative_parts:
        return None
    return "/".join(archive_parts[: -len(relative_parts)])


def _archive_path_for_candidate(
    entry: SourceEntry,
    filesystem_prefix: Path,
    archive_prefix: str,
) -> str | None:
    try:
        relative = entry.absolute_path.relative_to(filesystem_prefix)
    except ValueError:
        return None
    relative_path = relative.as_posix()
    if relative_path == ".":
        return archive_prefix
    if archive_prefix:
        return f"{archive_prefix}/{relative_path}"
    return relative_path


def _posix_parts(path: Path) -> tuple[str, ...]:
    value = path.as_posix()
    if value == ".":
        return ()
    return tuple(value.split("/"))


def _exact_entry_transforms(entries: tuple[SourceEntry, ...]) -> tuple[str, ...]:
    transforms: list[str] = []
    for entry in entries:
        source = os.fspath(entry.absolute_path)
        archive = entry.archive_path
        transforms.append(f"s#^{_escape_basic_regex(source)}$#{_escape_replacement(archive)}#")
    return tuple(transforms)


def _prefix_transforms(filesystem_prefix: str, archive_prefix: str) -> tuple[str, ...]:
    escaped_source = _escape_basic_regex(filesystem_prefix)
    if archive_prefix:
        escaped_archive = _escape_replacement(archive_prefix)
        return (
            f"s#^{escaped_source}$#{escaped_archive}#",
            f"s#^{escaped_source}/#{escaped_archive}/#",
        )
    return (f"s#^{escaped_source}/##",)


def _escape_basic_regex(value: str) -> str:
    escaped = []
    for character in value:
        if character in "\\.[]^$*#":
            escaped.append("\\" + character)
        else:
            escaped.append(character)
    return "".join(escaped)


def _escape_replacement(value: str) -> str:
    return value.replace("\\", "\\\\").replace("&", "\\&").replace("#", "\\#")


def _decode_escape_quoting(value: bytes) -> bytes:
    decoded = bytearray()
    index = 0
    while index < len(value):
        current = value[index]
        if current != ord("\\"):
            decoded.append(current)
            index += 1
            continue
        index += 1
        if index == len(value):
            raise ArchiveVerificationError("TAR listing ends with an incomplete escape")
        escaped = value[index]
        simple = _SIMPLE_ESCAPES.get(escaped)
        if simple is not None:
            decoded.append(simple)
            index += 1
            continue
        if ord("0") <= escaped <= ord("7"):
            end = index
            while end < min(index + 3, len(value)) and ord("0") <= value[end] <= ord("7"):
                end += 1
            decoded.append(int(value[index:end], 8))
            index = end
            continue
        raise ArchiveVerificationError("TAR listing contains an unsupported escape")
    return bytes(decoded)
