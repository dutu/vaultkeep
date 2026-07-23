"""GitWildMatch exclusion compilation and matching."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from pathspec import PathSpec


class InvalidExclusionPattern(ValueError):
    """An exclusion cannot be represented by the v1 grammar."""


@dataclass(frozen=True, slots=True)
class ExclusionMatcher:
    """Compiled global and per-source exclusion patterns."""

    _spec: PathSpec

    def matches(self, relative_path: str, *, is_directory: bool) -> bool:
        """Return whether a source-relative path is excluded."""
        candidate = relative_path.rstrip("/")
        if is_directory:
            candidate += "/"
        return self._spec.match_file(candidate)


def compile_exclusions(patterns: Iterable[str]) -> ExclusionMatcher:
    """Compile v1 patterns, rejecting negation and malformed syntax."""
    materialized = tuple(patterns)
    for pattern in materialized:
        if pattern.startswith("!"):
            raise InvalidExclusionPattern(
                f"Negated exclusion pattern {pattern!r} is not supported."
            )
        if "\0" in pattern:
            raise InvalidExclusionPattern(
                f"Exclusion pattern {pattern!r} contains a null character."
            )
    try:
        return ExclusionMatcher(PathSpec.from_lines("gitwildmatch", materialized))
    except (TypeError, ValueError) as error:
        raise InvalidExclusionPattern(
            f"Malformed exclusion pattern {materialized[0]!r}: {error}"
        ) from error
