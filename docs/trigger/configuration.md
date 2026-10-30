# Trigger configuration

Trigger settings belong in the same YAML file as the backup sources, destination,
retention, and schedule. There is no separate Trigger configuration file. Each
monitor reads one job once at startup; validate and run `trigger update` after edits.

`trigger` is optional and defaults to disabled. `schedule.enabled` and
`trigger.enabled` cannot both be true. Both may be false for manual-only jobs.
Disable the old timer/monitor and drain running work before changing mechanisms.

```yaml
schedule:
  enabled: false
application:
  name: electrum
  data_dir: /home/alice/.electrum
  matcher:
    entrypoints:
      - kind: script
        value: /home/alice/.local/opt/electrum/current/run_electrum

trigger:
  enabled: true
  poll_seconds: 2
  confirm_seconds: 5
```

This fragment supplements a complete job. Copy and customize one of the complete,
disabled examples: [Electrum](../../examples/electrum-trigger.yaml),
[Bisq](../../examples/bisq-trigger.yaml), or
[Wasabi](../../examples/wasabi-trigger.yaml). Use the
[schedule example](../../examples/vaultkeep-schedule.yaml) for other jobs.
Process support and directory-identification limitations are documented in
[process detection](process-detection.md); examples do not certify installed app versions.

Application-specific setup and multiple-instance guidance:

| Application | Configuration guide |
| --- | --- |
| Bisq 1 GUI/API daemon | [Bisq Trigger configuration](apps/bisq.md) |
| Electrum | [Electrum Trigger configuration](apps/electrum.md) |
| Wasabi GUI/daemon | [Wasabi Trigger configuration](apps/wasabi.md) |

## Fields

| Key | Default | Meaning |
| --- | --- | --- |
| `enabled` | `false` | Opt this job into shutdown monitoring; service activation is explicit. |
| `poll_seconds` | `2` | Observation interval in seconds. |
| `confirm_seconds` | `5` | Continuous reliable absence required to confirm shutdown. |
| `deadline_seconds` | absent | Advisory overdue threshold; never terminates a backup. |
| `params` | `{}` | String runtime parameters passed to this same backup job. |

Unknown keys are rejected. Booleans must be booleans; timing values must be finite
positive numbers. Application identity is configured once in top-level `application`:
`name`, `data_dir`, and `matcher` are required when that section exists. `name` selects
application-specific directory detection. Omit `user`: the monitor and worker use
their execution account. An explicit `application.user` is unsupported in user mode.
An enabled Trigger requires `application`, as does `consistency.mode: application_offline`.
See [shared consistency settings](../configuration.md#consistency-and-shared-application-identity).

The backup job's `job.id` identifies the monitored instance. When Trigger is enabled
and Schedule disabled, `job.id` may differ from the YAML filename; changing it requires
`trigger update`. Its `logging.level`
controls monitor logging. The monitor invokes this installation's own `vaultkeep`
entry point; there is no separately configured core executable.

## Matcher

| Key | Meaning |
| --- | --- |
| `entrypoints` | Required non-empty list of `{kind, value}` identities. |
| `exe_realpath` | Optional absolute interpreter/executable identity. |
| `argv_tokens` | Additional exact argv tokens; each 1–4096 characters. |
| `cmdline_regex` | Optional valid regex, at most 200 characters. |
| `env_eq` | Allowed environment predicates: Electrum `ELECTRUMDIR`, Wasabi `WASABI_DATADIR`; none for Bisq. |

Each `entrypoints` item requires `kind` (`script`, `jar`, or `java_main`) and
`value`. Entries are alternatives: any one can establish the application
identity. Entries must be unique. All other matcher fields apply in addition to
the chosen entrypoint.

Script/jar entrypoints, executable paths, and data directories must be absolute or
start with `~/`. Home expansion uses the operating-system account record, not an
arbitrary `HOME`. Shell expressions and environment-variable expansion are not
performed. Executable identity alone is insufficient; at least one entrypoint is required.
See [process detection](process-detection.md) for ambiguity and writer coverage.

## Parameters and identity

For a shared job with a destination such as `/mnt/backups/{NETWORK}`:

```yaml
trigger:
  # Other trigger fields omitted here.
  params:
    NETWORK: mainnet
```

Parameter names and values follow the normal `--param KEY=VALUE` rules. Command-line
parameters override these defaults for installation/update and desired-state checks.
Pass matching `--param` arguments to manual backups; manual and Schedule parameterization
is unchanged. Parameters can render destination paths such as `/srv/backups/{repo}/bisq`.

There is one managed Trigger per canonical YAML path, regardless of parameters.
The service name is `vaultkeep-trigger@<yaml-filename>-<path-hash>.service`; YAML
contents, `job.id`, and parameter values do not affect it. See
[service management](../cli.md#service-management) for escaping and path hashing.

The backup execution identity still includes the canonical path, `job.id`, and
effective parameters. Changing those execution settings requires `trigger update`,
which verifies the previous identity is safe to retire before switching. The installed
selection is persisted in the unit independently of YAML; unexpected restarts cannot
silently switch identities. Old recovery directories remain untouched. Disable/remove
use installed metadata and do not need the original parameters or readable YAML.

Moving or renaming YAML changes the service identity. Remove the old installation
using its original canonical path, then install at the new path. It is not an automatic
rename or migration.

## Notifications

The job's top-level `notifications` section independently enables `backup.enabled`
and `trigger.enabled` (both default `false`). Shared `command` (default `notify-send`)
and positive integer `timeout_ms` (default `5000`) apply to both. Trigger alerts cover
application-state changes, dispatch, and recovery. Backup owns execution start, phases,
and results for every initiation method. Neither category requires the other.
See [notifications and session setup](../notifications.md).

Requests freeze the effective consistency policy, complete application identity,
resolved owner UID, job selection, and environment. The child compares that frozen
policy/identity with its own single loaded snapshot before executing. Other job
contents and executable bytes are not frozen. See [recovery](recovery.md#boot-and-configuration-fingerprints).

[User guide](../../README.md#configure-and-validate-trigger)
