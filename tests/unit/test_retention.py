"""Tests for calendar retention selection and cascading horizons."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

from vaultkeep.config.models import RetentionConfig
from vaultkeep.retention import RetentionBackup, plan_retention


def _backup(identifier: int, created_at: datetime) -> RetentionBackup:
    return RetentionBackup(
        backup_id=f"{identifier:032x}",
        directory=Path(f"/backups/{identifier}"),
        created_at=created_at,
        created_at_utc=created_at.astimezone(UTC),
    )


def _retention(**counts: int) -> RetentionConfig:
    values = {"hourly": 0, "daily": 0, "weekly": 0, "monthly": 0, "yearly": 0}
    values.update(counts)
    return RetentionConfig(**values)


def test_retention_unions_tier_selections() -> None:
    first = _backup(1, datetime(2026, 7, 1, 10, tzinfo=timezone(timedelta(hours=3))))
    second = _backup(2, datetime(2026, 7, 2, 10, tzinfo=timezone(timedelta(hours=3))))
    third = _backup(3, datetime(2026, 7, 2, 11, tzinfo=timezone(timedelta(hours=3))))

    plan = plan_retention((first, second, third), _retention(daily=2, hourly=1))

    assert {backup.backup_id for backup in plan.retain} == {first.backup_id, third.backup_id}
    assert plan.delete == (second,)


def test_coarser_horizon_limits_finer_selection() -> None:
    older = _backup(1, datetime(2026, 6, 1, 9, tzinfo=UTC))
    june_later = _backup(2, datetime(2026, 6, 30, 9, tzinfo=UTC))
    july = _backup(3, datetime(2026, 7, 1, 9, tzinfo=UTC))

    plan = plan_retention((older, june_later, july), _retention(monthly=1, daily=10))

    assert {backup.backup_id for backup in plan.retain} == {july.backup_id}


def test_hourly_bucket_distinguishes_repeated_offset_hour() -> None:
    early = _backup(1, datetime(2026, 10, 25, 3, tzinfo=timezone(timedelta(hours=3))))
    late = _backup(2, datetime(2026, 10, 25, 3, tzinfo=timezone(timedelta(hours=2))))

    plan = plan_retention((early, late), _retention(hourly=2))

    assert {backup.backup_id for backup in plan.retain} == {early.backup_id, late.backup_id}
