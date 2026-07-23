"""Local state, reconstruction, and unchanged-detection APIs."""

from vaultkeep.state.atomic import write_local_state
from vaultkeep.state.identity import job_identity_hash, job_state_path
from vaultkeep.state.local_state import (
    ReconciliationStatus,
    StateReconciliation,
    calculate_credential_fingerprint,
    load_local_state,
    reconcile_local_state,
)
from vaultkeep.state.models import (
    BackupStateRecord,
    CredentialFingerprint,
    HookOutcomeState,
    LastRunState,
    LastSuccessfulBackup,
    LocalState,
)
from vaultkeep.state.transitions import (
    state_after_created,
    state_after_failed,
    state_after_unchanged,
)
from vaultkeep.state.unchanged import ChangeDecision, ChangeReason, evaluate_unchanged

__all__ = [
    "BackupStateRecord",
    "ChangeDecision",
    "ChangeReason",
    "CredentialFingerprint",
    "HookOutcomeState",
    "LastRunState",
    "LastSuccessfulBackup",
    "LocalState",
    "ReconciliationStatus",
    "StateReconciliation",
    "calculate_credential_fingerprint",
    "evaluate_unchanged",
    "job_identity_hash",
    "job_state_path",
    "load_local_state",
    "reconcile_local_state",
    "state_after_created",
    "state_after_failed",
    "state_after_unchanged",
    "write_local_state",
]
