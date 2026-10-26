"""Metadata-only backup inventory presentation."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from vaultkeep.backup.workflow import BackupInventory
from vaultkeep.cli.output import Console, format_size, format_table, safe_text


def _display_path(path: Path) -> str:
    """Keep paths identifiable even with control characters or limited encodings."""
    text = str(path)
    if any(not char.isprintable() or char == "\\" for char in text):
        text = json.dumps(text, ensure_ascii=False)
    encoding = sys.stdout.encoding or "ascii"
    return text.encode(encoding, errors="backslashreplace").decode(encoding)


def print_inventory(inventory: BackupInventory, *, output: Console) -> None:
    """Render inventory and planner selections without any further filesystem reads."""
    destination = inventory.destination
    policy = inventory.retention.model_dump()
    enabled = any(policy.values())
    retention = (
        ", ".join(f"{count} {tier}" for tier, count in policy.items()) if enabled else "disabled"
    )
    lines = [
        "Vaultkeep — Backups",
        "",
        f"Job:         {inventory.job}",
        f"Destination: {_display_path(destination)}",
        f"Retention:   {retention}",
        "",
    ]
    decisions = inventory.decisions
    if inventory.backups:
        rows = [
            (
                backup.manifest.created_datetime_utc.strftime("%Y-%m-%d %H:%M:%S"),
                format_size(backup.archive_size),
                decisions[backup.directory],
                ", ".join(inventory.retained_by.get(backup.directory) or ()) or "—",
            )
            for backup in inventory.backups
        ]
        details = [
            f"  Backup: {_display_path(backup.directory.relative_to(destination))}"
            for backup in inventory.backups
        ]
        table = format_table(
            ("Created (UTC)", "Size", "Decision", "Tiers"),
            rows,
            stream=sys.stdout,
            details=details,
        )
        reasons = {
            safe_text(detail, sys.stdout): backup.suspect_reason
            for detail, backup in zip(details, inventory.backups, strict=True)
            if backup.suspect_reason
        }
        for line in table:
            lines.append(line)
            if line in reasons:
                lines.append(f"    SUSPECT: {reasons[line]}")
    else:
        lines += ["No backups found."]

    suspects = tuple(backup for backup in inventory.backups if backup.suspect_reason is not None)
    if inventory.malformed or inventory.retention_warnings:
        lines += ["", "Warnings:"]
        for entry in inventory.malformed:
            label = _display_path(entry.path.relative_to(destination))
            reason = entry.reason.replace(str(entry.path) + "/", "").replace(str(entry.path), "")
            diagnostic = reason.splitlines()
            lines += [f"  {label}: {diagnostic[0].rstrip(': ')}"]
            lines += [f"    {line}" for line in diagnostic[1:]]
        for history, reason in inventory.retention_warnings:
            label = _display_path(history.relative_to(destination))
            lines += [f"  Retention preview incomplete for {label}: {reason}"]
        if enabled and inventory.malformed:
            lines += ["  Matching invalid entries block pruning; affected histories are UNKNOWN."]

    summary = f"Backups: {len(inventory.backups)}"
    if suspects:
        summary += f" · Suspect: {len(suspects)}"
    if enabled and inventory.backups:
        summary += f" · Keep: {sum(value == 'KEEP' for value in decisions.values())}"
        summary += f" · Prune: {sum(value == 'PRUNE' for value in decisions.values())}"
        unknown = sum(value == "UNKNOWN" for value in decisions.values())
        if unknown:
            summary += f" · Unknown: {unknown}"
    lines += ["", summary]
    if inventory.malformed:
        lines += [f"Invalid entries: {len(inventory.malformed)}"]
    for line in lines:
        output.print(safe_text(line, sys.stdout))
