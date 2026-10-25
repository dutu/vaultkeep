"""Metadata-only backup inventory presentation."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from vaultkeep.backup.workflow import BackupInventory
from vaultkeep.cli.output import Console, format_size, format_table, safe_text
from vaultkeep.diagnostics import concise


def _display_path(path: Path) -> str:
    """Keep paths identifiable even with control characters or limited encodings."""
    text = str(path)
    if any(not char.isprintable() or char == "\\" for char in text):
        text = json.dumps(text, ensure_ascii=False)
    encoding = sys.stdout.encoding or "ascii"
    return text.encode(encoding, errors="backslashreplace").decode(encoding)


def print_inventory(inventory: BackupInventory, *, output: Console, verbose: bool = False) -> None:
    """Render discovered facts without performing any further filesystem reads."""
    destination = inventory.destination
    lines = ["Vaultkeep — Backups", "", f"Destination: {_display_path(destination)}", ""]
    if inventory.backups:
        rows = [
            (
                backup.manifest.created_datetime_utc.strftime("%Y-%m-%d %H:%M:%S"),
                format_size(backup.archive_size),
                "yes" if backup.manifest.encrypted else "no",
                _display_path(
                    backup.archive_path if verbose else backup.archive_path.relative_to(destination)
                ),
            )
            for backup in inventory.backups
        ]
        lines += format_table(
            ("Created (UTC)", "Size", "Encrypted", "Archive"), rows, stream=sys.stdout
        )
    else:
        lines += ["No backups found."]

    if verbose and inventory.backups:
        lines += ["", "Metadata:"]
        for backup in inventory.backups:
            manifest = backup.manifest
            lines += [
                f"  Directory: {_display_path(backup.directory)}",
                f"    Manifest: {_display_path(backup.manifest_path)}",
                f"    Manifest version: {manifest.manifest_version}",
                f"    Backup ID: {manifest.backup_id}",
                f"    Job: {manifest.job}",
                f"    Created (stored local time): {manifest.created_at}",
                f"    Format: {manifest.archive_format}",
                f"    Size (bytes): {backup.archive_size}",
                f"    Hostname: {manifest.hostname}",
                f"    Vaultkeep version: {manifest.application_version}",
                f"    Source digest (stored): {manifest.source_digest}",
                f"    Configuration fingerprint: {manifest.config_fingerprint}",
                f"    Archive digest (stored): {manifest.archive_digest}",
                f"    Checksum sidecar: {_display_path(backup.checksum_path)}",
            ]

    if inventory.malformed:
        lines += ["", "Warnings:"]
        for entry in inventory.malformed:
            label = _display_path(entry.path.relative_to(destination))
            # Retain the complete diagnostic in verbose mode. The normal report
            # removes repeated directory prefixes and bounds validation details.
            reason = entry.reason.replace(str(entry.path) + "/", "").replace(str(entry.path), "")
            reason = concise(reason.splitlines()[0].rstrip(": "))
            lines += [f"  {label}: {reason}"]
            if verbose:
                lines += [f"    {line}" for line in entry.reason.splitlines()]

    if inventory.backups or inventory.malformed:
        lines += [""]
        if inventory.malformed:
            lines += [
                f"Backups:         {len(inventory.backups)}",
                f"Invalid entries: {len(inventory.malformed)}",
            ]
        else:
            lines += [f"Backups: {len(inventory.backups)}"]

    for line in lines:
        output.print(safe_text(line, sys.stdout))
