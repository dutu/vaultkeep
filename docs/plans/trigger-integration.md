# Trigger integration

## Scope

Vaultkeep ships one Python distribution and one `vaultkeep` entry point. Shutdown
monitoring lives under `src/vaultkeep/trigger/`; it is configured and activated per
backup job. Electrum, Bisq 1 and Wasabi retain their existing process-model and
consistency limitations. Scheduling and shutdown triggering are mutually exclusive
for a job; both can be disabled for manual-only use.

## Packaging and configuration

Use the root `pyproject.toml`, production dependency lock and development lock.
`psutil` is a normal dependency of the unified package. No standalone Trigger project,
entry point, dependency lock, duplicate version, or separately invoked test suite
remains. Runtime versions come from the Vaultkeep distribution metadata.

The job YAML contains an optional `trigger` section with enablement, app, data
directory, strong matcher, timing, notification settings and selected runtime
parameters. It replaces the separate TOML and refers to the same backup job rather
than another configuration path. Strict schema validation rejects simultaneous
`schedule.enabled` and `trigger.enabled`. Existing jobs without `trigger` remain valid.

Ship complete disabled examples for scheduled/manual jobs and each supported app.
The README provides installation, configuration, explicit activation, everyday
operation, maintenance and removal procedures. Detailed references stay
under `docs/trigger/`.

## Per-job monitoring and workers

Trigger supports non-root user-mode execution only. Operational commands require
`--user`; root execution fails before configuration, state or service actions.
Monitor and worker services include the explicit flag. Root-mode backups and timers
retain their existing behavior. Help/version inspection does not require user mode.

Each selected job has a generated systemd user monitor service, a private runtime
lock, and a private recovery namespace. Derive the namespace using the existing
16-hex Vaultkeep identity hash of canonical config path, job ID and runtime parameters.
Name monitor units `vaultkeep-trigger@<job-id>-<identity>.service`.

Use `<account-home>/.local/state/vaultkeep-trigger/jobs/<identity>` for durable state
and `<XDG_RUNTIME_DIR>/vaultkeep-trigger/jobs/<identity>/monitor.lock` for its lifetime
lock. Each monitor reconciles only its own store; duplicate monitors for an identity
are refused. The shared worker template receives `<identity>_<attempt-uuid>` and
validates both parts before reading that identity's frozen request.

Keep the existing lifecycle, final process recheck, durable request/launch/receipt
protocol, no-replay rules and per-job locking. Backup owns execution results and
progress; Trigger retains the outcome needed for dispatch recovery. Monitor stop,
restart, disable or removal never terminates workers. The backup subprocess uses the
installation's own CLI and preserves the original user's runtime lock namespace.

`vaultkeep --user trigger` supports per-job foreground monitoring,
install, update, disable and remove. `vaultkeep status` independently combines Backup,
Trigger and Schedule information, with one human/JSON interpretation. Installation of the application never enables
monitoring; activation is an explicit per-job operation. Generated monitor services
have clear ownership markers and do not adopt unrelated units or local overrides.

## Installer compatibility

Install one project and its hash-locked dependencies in one versioned release.
Install inactive core timer/service templates and the shared user worker template.
Remove the public `--with-trigger` option and component-version coordination.

Write schema-3 deployment and ownership metadata without component selection; release
directories use `<version>`. Read and validate old schema-1 and schema-2 deployments
against their original artifact allowlists and paths. A combined-deployment upgrade
removes its managed standalone command and global monitor template transactionally.
Retain the previous deployment. Rollback restores old links, templates and metadata.

Preserve user YAML, old TOML and all recovery evidence. Block per-job monitor runtime
preflight while legacy shared recovery JSON remains unreviewed. Migration is explicit:
drain/reconcile old work, preserve an evidence archive, transfer instance settings to
job YAML, retire old user units, archive reviewed legacy state, then activate selected
jobs independently. Do not relabel or replay uncertain work.

## Validation

Run one pytest suite for backup and Trigger behavior. Cover YAML defaults and strict
validation, mutual exclusion, identity/lock/state isolation, worker routing, per-job
service ownership and activation, legacy evidence refusal, and unchanged lifecycle
and recovery semantics. Installer tests cover fresh installation, legacy core and
combined upgrades, idempotent verification/repair, conflicts, quiescence, rollback,
signals and uninstall preservation. Build the unified wheel and source archive,
rebuild the extracted archive, validate all examples, and exercise installed CLI and
worker contracts in disposable environments.

Live Debian/systemd lifecycle, installed wallet identity/writer review, supported
architecture execution and network restore checks remain deployment/release gates.
See [validation](trigger-validation.md) and [development/testing](../development/testing.md).
