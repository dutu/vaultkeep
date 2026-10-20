# Command-line interface

```text
vaultkeep-trigger [--config PATH] {monitor,check-config,status}
vaultkeep-trigger --version
vaultkeep-trigger --help
vaultkeep-trigger exec-worker <attempt-id>      # internal worker entry point
```

Place `--config PATH` before the subcommand. It selects configuration only for
`monitor` and `check-config`; `status` and workers always use the fixed state root and
do not read current TOML. `--version` prints the Trigger version without reading config.
Use `vaultkeep-trigger check-config --help` for command-specific arguments.

## `monitor`

Runs the foreground monitor: acquire `monitor.lock`, run runtime preflight, reconcile
existing attempts, then observe instances and dispatch eligible requests. `SIGTERM`/`SIGINT`
stop monitoring without terminating running workers. Configuration is read once.

## Configuration checks

The command is `check-config [--runtime]`: omit the flag for static/job schema checks.

Static checks validate the TOML, matchers, and the configured executable, then run
`vaultkeep --user --config <job> [--param KEY=VALUE ...] validate --schema-only` for
each **enabled** instance.

`--runtime` adds read-only `systemctl --user show-environment` reachability checks,
installed template and executable checks, runtime-directory and state-location checks,
and runs full `validate` per enabled instance. It also obtains `vaultkeep --version`,
reports the baseline, and warns when it differs from the reviewed contract. It is the
pre-restart gate. Validation never calls `run`, creates a probe
unit, initializes state, or alters user services. Schema-only checks do not certify
parameter-template semantics, source availability, destination writability, hooks, or
backup consistency.

The monitor acquires its lifetime lock before performing full runtime preflight or
creating persistent state. It creates its runtime lock directory/file as needed.
A second monitor is refused before any VaultKeep validation is spawned.
The separate `check-config --runtime` command checks absent state/runtime subdirectories through existing
parents without creating probes there. Template checks inspect installed unit
files and their fixed `oneshot` worker command; the Trigger console script must exist
at an executable absolute path. Full job validation creates, writes, atomically renames,
and removes a temporary destination probe. It still skips hook-executable checks.

Runtime checks do not inspect wallet identities, identify related writers, detect
cross-instance matcher overlap, or enforce destination independence. VaultKeep's
reviewed contract version is `0.2.0`; a mismatch is warning-only. The version lookup
can report `unknown` when it has no successful result; it does not certify compatibility.
VaultKeep validation/version calls have a 60-second timeout per call. Systemctl calls
have a 15-second timeout each and run synchronously, including during monitor
reconciliation; delayed calls can delay polling. See [safety](consistency.md).

For a custom configuration:

```sh
vaultkeep-trigger --config /absolute/path/config.toml check-config
vaultkeep-trigger --config /absolute/path/config.toml check-config --runtime
```

Job validation failures normally exit 2. A child spawn failure or VaultKeep validation
exit 2 is an integration error (Trigger exit 6). Captured validation output may include
job-specific details; review it before sharing logs.

## `status [<instance-id>]`

Read-only. Reports persisted instance state, the monitor's lock status, observation age,
pending/active IDs, last attempt outcome, submission-rejection count, blocked reporting,
and `review_required` warnings. It does not create
or modify the runtime lock or any state. Ages are printed independently of lock
ownership; there is no separate stale-state health flag or unhealthy exit code. An
unknown instance is a selection error when some instances are recorded. With no
recorded instances, the command prints `state: no recorded instances` and returns 0
even when an instance ID was supplied. Disabled/removed instances' saved records
remain visible; an enabled instance need not have a record before the monitor runs.

Persisted per-instance state-error blocks are shown as `dispatch blocked` with their
diagnostic reason. Corrupt records are reported while other readable instances are
still displayed; any corrupt or incompatible record makes the status query exit 2.

## Exit codes

| Code | Meaning |
| ---: | --- |
| 0 | Command succeeded. Successful-execution vocabulary includes created, unchanged, and result_unknown. |
| 1 | Reserved usage-error vocabulary; normal argparse errors currently exit 2. Unhandled Python errors can also exit 1. |
| 2 | Argument/usage error, Trigger/job configuration or instance-selection error, or corrupt/incompatible state for `status`. |
| 3 | Blocked or unsafe, including a required runtime manager/environment being unavailable. |
| 4 | Internal worker reports backup failure or deferred lock contention. |
| 5 | Reserved outcome-unknown vocabulary; monitor persists this outcome rather than exiting 5, and `status` reports it with exit 0. |
| 6 | Integration error. |

`status` returns 0 when it successfully reports valid state, even when that state
includes backup failures, warnings, or a down monitor. A second monitor refusing startup
exits 3 with a message naming the lock owner. Original VaultKeep exit codes are stored
separately in receipts and are never substituted for a Trigger exit code.

`monitor` returns 0 after an orderly signal-driven stop; individual backup failures
do not stop it with a backup exit code. `exec-worker` is a systemd protocol command,
not a supported manual retry mechanism. If a receipt already exists, it returns 0
without rerunning, regardless of the original outcome. Invalid/unreadable requests
can fail before writing a receipt; preserve evidence for [recovery](recovery.md).

[User guide](../../README.md#configure-and-validate-trigger)
