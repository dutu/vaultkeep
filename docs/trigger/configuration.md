# Trigger configuration

Trigger settings belong in the same YAML file as the backup sources, destination,
retention, and schedule. There is no separate Trigger configuration file. Each
monitor reads one job once at startup; validate and restart after edits.

`trigger` is optional and defaults to disabled. `schedule.enabled` and
`trigger.enabled` cannot both be true. Both may be false for manual-only jobs.
Disable the old timer/monitor and drain running work before changing mechanisms.

```yaml
schedule:
  enabled: false
trigger:
  enabled: true
  app: electrum
  data_dir: /home/alice/.electrum
  matcher:
    entrypoints:
      - kind: script
        value: /home/alice/.local/opt/electrum/current/run_electrum
  poll_seconds: 2
  confirm_seconds: 5
```

This fragment supplements a complete job. Copy and customize one of the complete,
disabled examples: [Electrum](../../examples/electrum-trigger.yaml.example),
[Bisq](../../examples/bisq-trigger.yaml.example), or
[Wasabi](../../examples/wasabi-trigger.yaml.example). Use the
[schedule example](../../examples/vaultkeep-schedule.yaml.example) for other jobs.
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
| `app` | absent | `electrum`, `bisq`, or `wasabi`; required when enabled. |
| `data_dir` | absent | App instance's absolute data directory; required when enabled. |
| `matcher` | absent | Strong process identity; required when enabled. |
| `poll_seconds` | `2` | Observation interval in seconds. |
| `confirm_seconds` | `5` | Continuous reliable absence required to confirm shutdown. |
| `deadline_seconds` | absent | Advisory overdue threshold; never terminates a backup. |
| `params` | `{}` | String runtime parameters passed to this same backup job. |
| `notifications` | disabled | Best-effort desktop notification settings. |

Unknown keys are rejected. Booleans must be booleans; timing values must be finite
positive numbers. While disabled, `app`, `data_dir`, and `matcher` are optional; a
minimal block can be `enabled: false`. Identity fields and other settings are still
validated when present, as in the complete disabled application examples. An enabled
block requires `app`, `data_dir`, and a non-empty `matcher.entrypoints` list.

The backup job's `job.id` identifies the monitored instance. Its `logging.level`
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
parameters may override these defaults. Use the same parameter selection for monitor
installation, update, status, disable, and remove. Pass matching `--param` arguments
to manual backup commands. Do not enable two monitors for the same application/data
directory under different parameter selections without reviewing matcher overlap and
backup destination independence.

The canonical job path, `job.id`, and selected parameters determine a 16-hex identity.
Each identity has its own monitor unit, runtime lock, and recovery directory. Moving
or renaming a job, changing `job.id`, or changing parameters creates another identity:
disable/drain the old service and review its recovery evidence first.

## Notifications

`trigger.notifications` accepts `enabled` (default `false`), `command` (default
`notify-send`), and positive integer `timeout_ms` (default `5000`). The command is
invoked directly without a shell. Notifications are optional; delivery failure never
changes the backup outcome. Enabling them includes observed backup phase updates.
With `notify-send`, start, activity, and final result replace one notification per
instance. Custom commands continue to receive title and body as two arguments and
deliver separate messages. See [notifications and session setup](notifications.md).

Requests freeze matcher settings, job paths, parameters and environment, rather than
job contents or executable bytes. Review live file replacements with the same care as
configuration edits. See [recovery](recovery.md#boot-and-configuration-fingerprints).

[User guide](../../README.md#configure-and-validate-trigger)
