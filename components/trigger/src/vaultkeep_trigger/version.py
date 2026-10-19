"""Version and schema constants for VaultKeep Trigger."""

from __future__ import annotations

TRIGGER_VERSION = "0.2.0"

#: The VaultKeep version whose CLI contract was reviewed for this integration.
REVIEWED_VAULTKEEP_VERSION = "0.2.0"

#: On-disk schema versions. An unknown version blocks dispatch instead of guessing.
INSTANCE_STATE_SCHEMA_VERSION = 1
REQUEST_SCHEMA_VERSION = 1
RECEIPT_SCHEMA_VERSION = 1
LAUNCH_SCHEMA_VERSION = 1
BLOCKED_INSTANCE_SCHEMA_VERSION = 1

#: Internal constants. These are intentionally not user configuration.
BLOCKED_WAIT_SECONDS = 3600
REJECTION_ESCALATION_THRESHOLD = 5
CONTENTION_RETRY_DELAY_SECONDS = 30
DIAGNOSTICS_CAP_BYTES = 256 * 1024
COMPLETED_ATTEMPT_RETENTION = 50
UNCERTAIN_HISTORY_CAP = 20
DEFAULT_NOTIFY_TIMEOUT_MS = 5000
DEFAULT_PREFLIGHT_TIMEOUT_SECONDS = 60
