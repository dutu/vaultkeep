"""Count-based calendar-bucket retention with cascading coarser horizons."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from vaultkeep.config.models import RetentionConfig
from vaultkeep.errors import RetentionError

RetentionTier = Literal["hourly", "daily", "weekly", "monthly", "yearly"]
_COARSEST_TO_FINEST: tuple[RetentionTier, ...] = ("yearly", "monthly", "weekly", "daily", "hourly")


@dataclass(frozen=True, slots=True)
class RetentionBackup:
    """Validated backup facts needed to select recovery points."""

    backup_id: str
    directory: Path
    created_at: datetime
    created_at_utc: datetime


@dataclass(frozen=True, slots=True)
class RetentionPlan:
    """A complete, side-effect-free decision for all valid backups."""

    retain: tuple[RetentionBackup, ...]
    delete: tuple[RetentionBackup, ...]
    selections: dict[RetentionTier, tuple[RetentionBackup, ...]]


def plan_retention(
    backups: tuple[RetentionBackup, ...], retention: RetentionConfig
) -> RetentionPlan:
    """Select retained recovery points without deleting or modifying anything."""
    _validate_backups(backups)
    enabled = tuple(tier for tier in _COARSEST_TO_FINEST if getattr(retention, tier) > 0)
    if not enabled:
        raise RetentionError("At least one retention tier must be enabled")

    selections: dict[RetentionTier, tuple[RetentionBackup, ...]] = {}
    horizon_tier: RetentionTier | None = None
    horizon_bucket: tuple[int, ...] | None = None
    for tier in enabled:
        candidates = backups
        if horizon_tier is not None and horizon_bucket is not None:
            candidates = tuple(
                backup
                for backup in backups
                if _bucket_key(horizon_tier, backup.created_at) >= horizon_bucket
            )
        grouped: dict[tuple[int, ...], list[RetentionBackup]] = {}
        for backup in candidates:
            grouped.setdefault(_bucket_key(tier, backup.created_at), []).append(backup)
        retained_buckets = sorted(grouped, reverse=True)[: getattr(retention, tier)]
        selected = tuple(_newest(grouped[bucket]) for bucket in retained_buckets)
        selections[tier] = selected
        if retained_buckets:
            horizon_tier = tier
            horizon_bucket = min(retained_buckets)

    retained_ids = {backup.backup_id for selected in selections.values() for backup in selected}
    ordered = tuple(sorted(backups, key=_newest_key, reverse=True))
    return RetentionPlan(
        retain=tuple(backup for backup in ordered if backup.backup_id in retained_ids),
        delete=tuple(backup for backup in ordered if backup.backup_id not in retained_ids),
        selections=selections,
    )


def _validate_backups(backups: tuple[RetentionBackup, ...]) -> None:
    identifiers: set[str] = set()
    for backup in backups:
        if backup.created_at.tzinfo is None or backup.created_at_utc.tzinfo is None:
            raise RetentionError("Retention timestamps must be timezone-aware")
        if backup.backup_id in identifiers:
            raise RetentionError(f"Duplicate backup ID in retention input: {backup.backup_id}")
        identifiers.add(backup.backup_id)


def _newest(candidates: list[RetentionBackup]) -> RetentionBackup:
    return max(candidates, key=_newest_key)


def _newest_key(backup: RetentionBackup) -> tuple[datetime, str]:
    return backup.created_at_utc, backup.backup_id


def _bucket_key(tier: RetentionTier, created_at: datetime) -> tuple[int, ...]:
    if tier == "hourly":
        offset = created_at.utcoffset()
        if offset is None:
            raise RetentionError("Hourly retention requires an explicit UTC offset")
        return (
            created_at.year,
            created_at.month,
            created_at.day,
            created_at.hour,
            int(offset.total_seconds() // 60),
        )
    if tier == "daily":
        return created_at.year, created_at.month, created_at.day
    if tier == "weekly":
        iso = created_at.isocalendar()
        return iso.year, iso.week
    if tier == "monthly":
        return created_at.year, created_at.month
    return (created_at.year,)
