"""GNU TAR member-list construction and structural validation."""

from __future__ import annotations

from vaultkeep.errors import ArchiveVerificationError
from vaultkeep.sources.entries import SourceEntryType, SourceSnapshot

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
