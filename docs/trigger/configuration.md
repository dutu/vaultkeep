# Configuration

Trigger reads one TOML file **once at startup**. Edits require a monitor restart,
preceded by `check-config --runtime`. There is no live reload, SIGHUP reload, or
config-file watcher.

Default location: `<absolute-XDG_CONFIG_HOME>/vaultkeep-trigger/config.toml`, falling
back to `<account-home>/.config/vaultkeep-trigger/config.toml` when `XDG_CONFIG_HOME`
is unset, empty, or relative. `--config PATH` overrides it for `monitor` and
`check-config`; place the option before the subcommand. A disabled example lives at
[`examples/config.toml`](../../components/trigger/examples/config.toml).

The configuration schema is closed: unknown keys are rejected at every level.

## `[trigger]`

| Key | Default | Meaning |
| --- | --- | --- |
| `poll_seconds` | `2` | Monitor loop sleep and default per-instance poll interval. |
| `confirm_seconds` | `5` | Continuous reliable absence required before a shutdown is confirmed. |
| `deadline_seconds` | absent | Optional advisory overdue deadline; absent means no deadline. |
| `log_level` | `INFO` | One of `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`. |
| `vaultkeep` | required | Absolute path to the VaultKeep executable. |

All timing values must be finite positive numbers (booleans are rejected).
An instance's poll override controls when it becomes due, but cannot make the main
loop wake faster than the global interval. Synchronous service probes and scheduling
also add latency. Deadlines are advisory and never terminate a backup.

## `[trigger.notifications]`

| Key | Default | Meaning |
| --- | --- | --- |
| `enabled` | `false` | Enable best-effort desktop notifications. |
| `command` | `notify-send` | Nonempty executable name/path; invoked directly, without shell arguments. |
| `timeout_ms` | `5000` | Delivery timeout, a positive integer in milliseconds. |

See [notifications](notifications.md).

## `[[instance]]`

| Key | Default | Meaning |
| --- | --- | --- |
| `id` | required | Unique safe filename component matching `[A-Za-z0-9][A-Za-z0-9_-]{0,63}`. |
| `app` | required | `electrum`, `bisq`, or `wasabi`. |
| `enabled` | `false` | Disabled instances are structurally validated but not dispatched. |
| `data_dir` | required | Absolute instance data-directory path; canonicalized when comparing process evidence. |
| `matcher` | required | Application entrypoint, with optional executable and supporting predicates (see [process detection](process-detection.md)). |
| `job` | required | `{ config = "<absolute job.yaml>", params = { KEY = "VALUE" } }`; `params` defaults to an empty string-to-string mapping. |
| `poll_seconds` | inherits | Optional per-instance override. |
| `confirm_seconds` | inherits | Optional per-instance override. |
| `deadline_seconds` | inherits | Optional per-instance override; v1 has no switch to disable an inherited global deadline. |

Instance IDs must be unique; `enabled` must be a boolean. There may be zero instances.
Disabled entries still need structurally valid required fields, matchers, and paths;
their job files and installed process models are not probed. `check-config` always
checks that the configured VaultKeep executable is a runnable file.

Path fields accept `~` or a leading `~/`, expanded from the operating-system account
home (not `$HOME`); paths must then be absolute. `~otheruser` is not expanded.
`$VAR`, command substitution, and shell expressions are not evaluated. The parser does
not reject every shell-looking string inside an absolute path: such text remains
literal. There is no arbitrary shell-command/template execution setting. File
existence is not a structural path check; enabled jobs are validated separately.

### Job selection

`job.config` is the YAML job file selected by the external VaultKeep CLI, not a Trigger
profile. Parameters are passed in sorted key order as repeated `--param KEY=VALUE`
arguments; values must be TOML strings. For example:

```toml
[instance.job]
config = "/home/user/.config/vaultkeep/jobs/wasabi-main.yaml"
params = { NETWORK = "mainnet" }
```

The source job must actually support these parameters. Schema-only validation does
not resolve parameter-template semantics. See [validation limits](cli.md#configuration-checks).

Changing configuration does not rewrite an active worker's saved request. The
[recovery rules](recovery.md#boot-and-configuration-fingerprints) describe pending-work
invalidation and frozen attempts. Review job-file edits and executable replacements
with the same care: requests freeze their paths and parameters, not file contents or
an immutable binary copy.

Removed settings that do not exist in v1: `state_dir`, `max_concurrent`,
`conflict_groups`, `requires_instances`, `consistency`, `online_note`, and per-instance
notification fields.

The Trigger state root is fixed at `<account-home>/.local/state/vaultkeep-trigger` and
cannot be configured; see [service operation](service-operation.md).

[User guide](../../README.md#configure-and-validate-trigger)
