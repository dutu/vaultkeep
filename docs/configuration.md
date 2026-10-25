# Configuration reference

Use the [README](../README.md#create-a-backup-job) for job creation and the first
backup. This reference explains the shared YAML format and operational restrictions.
For execution identities, locations, permissions, and user-manager setup, see the
[root-mode](root-mode.md) and [user-mode](user-mode.md) guides.

## Contents

- [Job structure](#job-structure)
- [Sources and exclusions](#sources-and-exclusions)
- [Independent file backups](#back-up-directory-files-separately)
- [Destination names and parameters](#destination-name-templates)
- [Destination safety checks](#destination-safety-checks)
- [Encryption and password files](#password-protected-backups)
- [Retention](#retention)
- [Schedule settings](#schedule-settings)
- [Lifecycle hooks](#lifecycle-hooks)
- [Logging and applying changes](#logging)

## Job structure

Jobs use strict YAML 1.2 with `config_version: 1`. Unknown properties and duplicate
mapping keys are rejected. Use booleans (`true`/`false`) and integers as their actual
YAML types, and quote times and patterns. The YAML filename stem must match `job.id`.
IDs contain 1–128 ASCII letters, digits, underscores, or hyphens.

| Section | Required | Purpose |
| --- | --- | --- |
| `config_version`, `job` | Yes | Schema version and job identity. |
| `backup_mode` | No | `combined` (default) or `per_file`. |
| `sources` | Yes | Nonempty source list and archive path layout. |
| `exclude` | No | Global exclusions; defaults to `[]`. |
| `source_options` | Yes | Traversal options; each flag defaults to `false`. |
| `destination` | Yes | Existing destination root, naming, optional safety checks. |
| `archive`, `encryption` | Yes | Compatible archive format and encryption settings. |
| `retention` | Yes | All five calendar-bucket counts. |
| `schedule` | Yes | Automation flag and calendar when enabled. |
| `trigger` | No | Disabled by default; [complete Trigger settings](trigger/configuration.md). |
| `notifications` | No | Disabled by default; [settings and setup](notifications.md). |
| `hooks` | No | Six optional phases; each defaults to `null`. |
| `logging` | Yes | Required `level`, optional `include_command_output`. |

Each [job example](../README.md#create-a-backup-job) shows every supported parameter,
with optional settings commented out where appropriate. The
[annotated general example](../examples/vaultkeep-schedule.yaml) explains
their values. This complete manual-only job illustrates the shared structure:

```yaml
config_version: 1

job:
  id: app

sources:
  - path: /path/to/source
    archive_path_mode: prefix
    archive_prefix: source
  - path: /path/to/another-source
    archive_path_mode: prefix
    archive_prefix: another-source
    exclude:
      - cache/
      - "*.tmp"

exclude:
  - "**/.cache/**"

source_options:
  follow_symlinks: false
  cross_filesystems: false
  ignore_missing: false

destination:
  root: /path/to/backups/app
  name_template: "backup-{job}-{timestamp_utc:%Y%m%dT%H%M%SZ}"

archive:
  format: tar.zst
  compression_level: 6

encryption:
  mode: none

retention:
  hourly: 24
  daily: 7
  weekly: 8
  monthly: 12
  yearly: 3

schedule:
  enabled: false

hooks:
  before_check: null
  before_archive: null
  after_archive: null
  on_success: null
  on_failure: null
  on_unchanged: null

logging:
  level: info
  include_command_output: false
```

## Sources and exclusions

Each source needs an absolute `path` and an explicit `archive_path_mode`:

- `prefix` stores it under the required `archive_prefix`. An empty prefix stores a
  directory's contents at the archive root; a file prefix names that archive member.
- `preserve` stores the absolute path without its leading `/`; omit `archive_prefix`.

Every archive member must have a unique safe relative path. Duplicate or overlapping
sources and source/destination containment are rejected. Sources can be regular files,
directories, or symlinks; sockets, FIFOs, and device files are unsupported. Hard-link
relationships are preserved when all relevant paths are included. ACLs, extended
attributes, and SELinux attributes are not preserved by the current implementation.

Exclusions use GitWildMatch-style patterns, relative to each source root. Global
`exclude` applies to every source; `sources[].exclude` applies only to that source.
A pattern without `/` matches names at any depth, `**` crosses directories, and a
trailing `/` excludes a directory and its descendants. Negated patterns are rejected.

| `source_options` key | Default | Behavior |
| --- | --- | --- |
| `follow_symlinks` | `false` | Archive the link itself. `true` follows targets. |
| `cross_filesystems` | `false` | Do not descend into mounted filesystems below sources. |
| `ignore_missing` | `false` | Missing configured paths fail the operation instead of being skipped. |

Source changes detected during discovery, hashing, or archival fail the attempt;
Vaultkeep does not automatically retry. These checks do not create a consistent
snapshot of a live application. Use application dumps, securely managed hooks, or
an external snapshot mechanism when needed. Trigger has additional
[consistency limits](trigger/consistency.md).

## Back up directory files separately

To back up Electrum wallets independently, set these fields in a complete job:

```yaml
backup_mode: per_file

sources:
  - path: /home/alice/.electrum/wallets
    archive_path_mode: prefix
    archive_prefix: wallets
    exclude:
      - "*.lock"
      - "*.tmp"

destination:
  root: /mnt/backups/electrum
  name_template: "backup-{job}-{timestamp_utc:%Y%m%dT%H%M%SZ}"
```

Set the source and destination to your actual paths; the destination must already
exist. Keep the complete job's archive, encryption, and retention settings. Follow
[Manual validation, execution, and verification](../README.md#manual) as its owner,
with application writers stopped or another stable-source method in place. Per-file
results appear on stderr. This configuration changes archive grouping, independently
of Manual, Schedule, or Trigger activation.

`per_file` requires exactly one source directory and
`source_options.follow_symlinks: false`. It selects immediate regular files,
applies global and per-source exclusions, and skips subdirectories and symlinks.
New files are included automatically. Unchanged files create no archive; changes
to permissions, ownership, or backup-relevant configuration can also require a
backup. An empty selection finishes as `unchanged` without creating an archive.

Each file has its own destination folder, local state, and retention history. A
folder is named `file-<readable-name>-<sha256-of-original-filename>` so names remain
distinct on case-insensitive shares. Inside it, backups use the usual timestamped
directories and three artifacts. Each archive contains just `wallets/<filename>`
with the configuration above. Existing `tar.zst` and `tar.7z` formats apply; ZIP is
not supported.

Deleting or excluding a source file leaves its history intact. Renaming a file
starts a new history. `list`, `verify`, and `prune` include histories whose sources
are gone; retention is calculated separately for each file. Automatic retention
runs only for files that receive a new backup. A failure for one file does not
stop the remaining files; the command reports each result and exits with code 7
if any file failed. Successful files are checked normally on the next run.

Lifecycle hooks run separately for each selected file, including `before_check`
after the directory's file list has been selected. An empty selection runs no
hooks. Use a timer or the existing shutdown trigger for the complete job; see
[Electrum setup](trigger/apps/electrum.md#separate-wallet-backups).
Omit `backup_mode`, or set it to `combined`, for the default shared archive.

When migrating an existing combined job, use a separate destination root for
per-file backups. Existing combined backups and legacy ZIP archives are
not imported; retain them separately. The first run creates a new baseline for
every selected wallet.

## Destination name templates

`destination.name_template` controls the backup base name used for each new backup. The backup base name is the rendered template value. Vaultkeep renders the template once after source change detection and derives the final artifact names from that rendered base name:

```text
directory: <backup-base-name>
archive:   <backup-base-name>.<archive-format>
checksum:  <backup-base-name>.<archive-format>.sha256
manifest:  <backup-base-name>.json
```

The supported placeholders are exactly:

| Placeholder | Meaning | Formatting |
|---|---|---|
| `{job}` | The configured `job.id`. | No format specifiers. |
| `{hostname}` | The machine hostname returned by the operating system for the run. | No format specifiers. |
| `{timestamp}` | The backup creation timestamp in the machine's local timezone. | Supports Python `strftime` datetime formatting. |
| `{timestamp_utc}` | The same backup creation timestamp converted to UTC. | Supports Python `strftime` datetime formatting. |
| `{source_hash}` | The 64-character lowercase SHA-256 source digest, without the `sha256:` prefix. | May be truncated with precision syntax, for example `{source_hash:.12}`. |
| `{format}` | The selected archive format, either `tar.zst` or `tar.7z`. | No format specifiers. |

At least one of `{timestamp}` or `{timestamp_utc}` is required. Use doubled braces to include a literal brace in the output, for example `{{` or `}}`.

Recommended timestamp form:

```yaml
destination:
  name_template: "backup-{job}-{timestamp_utc:%Y%m%dT%H%M%SZ}"
```

Example with hostname and a shortened source hash:

```yaml
destination:
  name_template: "backup-{job}-{hostname}-{timestamp_utc:%Y%m%dT%H%M%SZ}-{source_hash:.12}"
```

Template output must be a single safe path name below `destination.root`: it cannot be empty, contain `/`, contain `\`, contain `..`, contain null or control characters, or escape the destination root. Template conversions such as `{job!r}`, nested fields, missing runtime parameters, and unsupported format specifiers are rejected.

If a path with the rendered directory name already exists, Vaultkeep uses the first available numbered name: `<backup-base-name>-2`, `<backup-base-name>-3`, and so on. The archive, checksum, and manifest filenames keep the original base name. Existing files, directories, and symbolic links are never overwritten. If a selected path becomes occupied before finalization, the run fails safely; a later run selects an available name. Existing backups with backup-ID directory suffixes remain supported by listing, verification, state reconstruction, and retention.

Do not include `{backup_id}` in `destination.name_template`. Vaultkeep keeps its randomly generated backup ID in the manifest and local state. Hook `command` values support built-in placeholders with shell-aware quoting; see [lifecycle hooks](#lifecycle-hooks).

## Runtime template parameters

Shared job configurations can use explicit runtime parameters in `destination.root` and `destination.name_template`.

Example:

```yaml
destination:
  root: /path/to/backups/{repo}
  name_template: "backup-{job}-{repo}-{timestamp_utc:%Y%m%dT%H%M%SZ}"
```

Run it with:

```text
vaultkeep [--user] --config <job.yaml> --param repo=nas-a run
```

Any placeholder in `destination.root` must be supplied with `--param`. Any placeholder in `destination.name_template` that is not one of the built-in fields above must also be supplied with `--param`. Extra unused parameters are rejected.

Runtime parameter names must match `[A-Za-z_][A-Za-z0-9_]*` and cannot use built-in names such as `job`, `timestamp_utc`, `source_hash`, or `backup_id`. Runtime parameter values are intended for non-secret selectors such as repository names; they may contain only letters, digits, `.`, `_`, `:`, `@`, `+`, and `-`.

Runtime parameter values are part of the effective backup identity. Different parameter values use different destination namespaces, configuration fingerprints, local state files, locks, and timer instances.

## Destination safety checks

`destination.root` must be an existing directory. Two optional safety checks can
be configured independently: use either, both, or neither.

- `marker_file` confirms that a designated marker exists as a regular file below
  `destination.root`. Use a relative path without `..` components. Vaultkeep does
  not check its contents or create it automatically. A missing or inaccessible
  marker blocks destination writes.
- `expected_fstype` verifies the actual filesystem backing `destination.root` as
  seen by Vaultkeep, including inside a container. Use an exact kernel filesystem
  type such as `cifs`, `nfs`, `nfs4`, or `ext4`.

For a CIFS destination, especially one mounted on an LXC host and bind-mounted into
the container, both checks can be useful:

```yaml
destination:
  root: /mnt/backups/app
  name_template: "backup-{job}-{timestamp_utc:%Y%m%dT%H%M%SZ}"
  expected_fstype: cifs
  marker_file: .vaultkeep-destination
```

If an LXC bind mount exposes a host local directory instead of the CIFS share,
the filesystem type differs and Vaultkeep stops before writing to or
managing backups on that destination. A type check does not identify a particular
server or share.

1. Inspect the destination from the environment that runs Vaultkeep
   (inside LXC when applicable):

   ```sh
   findmnt --uniq --target /mnt/backups/app --output TARGET,SOURCE,FSTYPE
   ```

   Confirm the expected server/share and `FSTYPE` of `cifs`. A local filesystem
   such as `ext4` or `zfs` indicates that the intended share is unavailable at that path.

2. After confirming the real share, create the configured marker once:

   ```sh
   touch /mnt/backups/app/.vaultkeep-destination
   ```

   Keep this marker absent from the underlying host local directory. Vaultkeep
   checks that the marker is a file below `destination.root`; it does not check
   its contents or create it automatically.

3. Run full validation with your job configuration:

   ```sh
   sudo vaultkeep --config /etc/vaultkeep/jobs/app.yaml validate
   ```

Filesystem type checks use `findmnt` from the required `util-linux` package and
recognize bind mounts in the running process's mount namespace. Failed, unavailable,
or ambiguous filesystem inspection and filesystem type mismatches stop the operation
with destination error exit code 5. Schema-only validation skips these runtime checks.
Configured safety checks precede the writable-destination probe, which tests writing,
renaming, and removal during full validation and before backup writes. In `per_file`
mode, the parent destination is checked before creating each child directory; child
directories do not need their own marker, and retain the filesystem type check.
A filesystem type check alone does not establish that a NAS is reachable.
These checks confirm conditions at the time of inspection; they do not prevent
mount changes or network failures during an operation.

## Archive formats and encryption settings

| Setting | Values and requirements |
| --- | --- |
| `archive.format` | `tar.zst` or `tar.7z`. |
| `archive.compression_level` | Default `6`; 1–19 for `tar.zst`, 1–9 for `tar.7z`. |
| `encryption.mode` | `none` with `tar.zst`, `password` with `tar.7z`. |
| `encryption.password_file` | Required absolute path for password mode; omit for `none`. |

ZIP is not supported. Both formats contain TAR filesystem content and can be restored
with [standard tools](../README.md#restore-a-backup).

## Password-protected backups

Create a password file without placing the password in command history. The path and ownership rules are mode-specific:

- root mode uses root-owned password files, normally below `/etc/vaultkeep/secrets`;
- user mode uses user-owned password files, normally below `${XDG_CONFIG_HOME:-~/.config}/vaultkeep/secrets`.

The file contains one UTF-8 passphrase line.

Change the archive settings:

```yaml
archive:
  format: tar.7z
  compression_level: 6

encryption:
  mode: password
  password_file: /path/to/app.passphrase
```

The archive filename retains the configured backup base name and changes its derived extension to `.tar.7z`.

Vaultkeep passes the password to `/usr/bin/7z` through a private terminal with echo disabled, after the password prompt appears. It does not place the password in command arguments, environment variables, manifests, or logs.

If 7-Zip does not display its password prompt within 30 seconds, the operation fails
and Vaultkeep stops the archive processes. This deadline applies to the prompt;
archive creation and verification can take longer.

To preserve TAR filesystem semantics, Vaultkeep creates a private plaintext TAR below the mode-specific temporary workspace, encrypts it with AES-256 and encrypted headers, verifies the result, and removes the plaintext TAR before committing the backup. Secure physical erasure is not guaranteed on journaling filesystems, copy-on-write filesystems, or SSDs.

V1 uses one password for a job and destination namespace. Password rotation requires a new job ID, password file, and destination namespace. Retain the old password file while old encrypted backups are needed.

When the password file changes or local backup state must be reconstructed, Vaultkeep tests the newest valid encrypted backup with the current password before deciding whether to create a backup. If the test fails, the run stops before archive creation or automatic retention. Replacing the password file with the same passphrase is supported.

### Create a password file

Use an editor so the passphrase does not enter shell history. For a user-owned job:

```bash
secrets_dir="${XDG_CONFIG_HOME:-$HOME/.config}/vaultkeep/secrets"
install -d -m 700 "$secrets_dir"
(umask 077; touch "$secrets_dir/app.passphrase")
chmod 600 "$secrets_dir/app.passphrase"
nano "$secrets_dir/app.passphrase"
```

For a root-owned job, after the administrator has installed Vaultkeep:

```bash
sudo install -d -m 700 /etc/vaultkeep/secrets
sudo touch /etc/vaultkeep/secrets/app.passphrase
sudo chown root:root /etc/vaultkeep/secrets/app.passphrase
sudo chmod 600 /etc/vaultkeep/secrets/app.passphrase
sudo nano /etc/vaultkeep/secrets/app.passphrase
```

Use the absolute path in `encryption.password_file`. The password file must be a
regular file, never a symlink, with mode exactly `0600`. Root mode requires owner
`root:root`; user mode requires the executing user's UID. Every parent must be a
real directory and must not be writable by group or others. The file is limited to
64 KiB and contains one nonempty UTF-8 passphrase, optionally ending in one newline;
embedded newlines, carriage returns, and null bytes are rejected. Keep a secure,
independent copy of the passphrase for restoration.

## Retention

Set all five counts: `hourly`, `daily`, `weekly`, `monthly`, and `yearly`. Counts are
integers at least zero; zero disables a tier and at least one tier must be enabled.
This is count-based calendar retention, rather than a file-age deletion rule.

Vaultkeep retains the newest backup in each selected bucket, evaluating
`yearly → monthly → weekly → daily → hourly`. Each finer tier is bounded by the
oldest retained bucket of its next enabled coarser tier. Its count is a maximum;
a backup selected by multiple tiers is kept once. Buckets use the manifest's local
calendar timestamp: ISO weeks for weekly retention, and the UTC offset as part of
hourly buckets to distinguish repeated daylight-saving hours. Newest is determined
by the UTC creation timestamp, with backup ID as a deterministic tie-breaker.

Automatic retention runs only after a new archive is finalized, separately for each
newly backed-up file in `per_file` mode. An unchanged run or time passing alone does
not prune. Retention changes do not force a backup; use
[`prune --dry-run`, then `prune`](cli.md#prune-job-backups) to apply them explicitly.
Only recognized, complete valid backups participate. Unrelated, temporary, and
malformed entries are never automatically deleted. A malformed matching backup
blocks pruning plan calculation in both dry-run and apply modes. Use `verify` to
identify malformed entries; preserve evidence before making changes.

## Schedule settings

Manual-only jobs need only `schedule: {enabled: false}`. Trigger and Schedule cannot
both be enabled. [Activate scheduling in README](../README.md#schedule).

| Key | Values and requirements |
| --- | --- |
| `enabled` | Required boolean. Editing it does not stop an installed timer. |
| `interval` | Required when enabled: `hourly`, `daily`, `weekly`, or `monthly`. |
| `at` | Fixed local `HH:MM` time; mutually exclusive with `window`. |
| `window` | Local `HH:MM-HH:MM` period; mutually exclusive with `at`. |
| `day` | Required for weekly (weekday name) or monthly (integer 1–28); omit otherwise. |
| `persistent` | Default `true`; one catch-up activation after downtime. |

Enabled schedules require exactly one of `at` or `window`. Times use 24-hour notation;
a window must end later than it starts on the same day. For hourly schedules, use
hour `00` in both forms, for example `at: "00:15"` (15 minutes past every hour) or
`window: "00:10-00:50"`. Overnight windows are unsupported. Windows spread jobs
**deterministically**, using job, machine, and parameter identity; they are not newly
randomized on every run. The same selection normally gets the same offset.

```yaml
schedule:
  enabled: true
  interval: weekly
  day: sunday
  window: "01:00-06:00"
  persistent: true
```

For monthly execution, use `interval: monthly`, `day: 1`, and either a window or
`at: "03:30"`. See [timer management](cli.md#configure-a-job-timer) for lifecycle,
parameterized instances, enablement, persistent timestamps, and rollback behavior.

## Lifecycle hooks

Hooks run under the job's execution identity: root in root mode, or the calling user in user mode. Each hook phase accepts one hook object. Its `command` is a shell command string executed with `/bin/sh -c`.

```yaml
hooks:
  before_check:
    command: '/usr/local/sbin/prepare-app-backup --job "{job}"'
    timeout_seconds: 300

  before_archive: null
  after_archive: null
  on_success:
    command: 'telegram-notify --level info "Backup completed for {job} on {hostname}"'
    timeout_seconds: 60
  on_failure:
    command: 'telegram-notify --level error "Backup failed for {job} on {hostname}: $VAULTKEEP_ERROR"'
    timeout_seconds: 60
  on_unchanged: null
```

The supported hook fields are:

- `command`: required non-blank shell command string. YAML lists are not accepted. Shell built-ins such as `cd`, `export`, and `printf`, variable expansion, command substitution, pipelines, redirection, and command sequences are supported. Use POSIX shell syntax; Bash-specific syntax requires explicitly invoking Bash.
- `timeout_seconds`: optional integer from 1 through 3600. The default is 300 seconds. If the hook does not finish before the timeout, Vaultkeep terminates the hook process group and treats the hook as failed.

Hook commands support the same built-in values as destination names:

| Placeholder | Value |
| --- | --- |
| `{job}` | Job ID. |
| `{hostname}` | Hostname used for this run. |
| `{timestamp}` | Time in the local timezone; supports formats such as `{timestamp:%Y-%m-%d %H:%M:%S%z}`. |
| `{timestamp_utc}` | The same time in UTC; for example `{timestamp_utc:%Y%m%dT%H%M%SZ}`. |
| `{source_hash}` | Source SHA-256 without the `sha256:` prefix; `{source_hash:.12}` takes the first 12 characters. Empty until hashing completes. |
| `{format}` | Configured archive format: `tar.zst` or `tar.7z`. |

Placeholders expand in unquoted text and shell double quotes. Use double quotes around messages to keep each message as one argument. Shell single quotes keep placeholders literal: `printf '%s' '{job}'` prints `{job}`. The single quotes around the complete YAML `command` value in the example are YAML quoting; they do not prevent shell expansion.

Existing `${VAR}` expressions and shell braces are preserved. Only recognized placeholders are expanded; other brace text stays literal. Placeholder conversions and nested format fields are not supported. Quoted here-document delimiters disable expansion in their bodies, following shell behavior.

For a created backup, timestamps match the backup filename and manifest. Before that timestamp is assigned, hooks use the time the hook lifecycle started; an unchanged run uses its check time. A failure hook receives the source hash if hashing completed before the failure.

Available phases:

- `before_check`: prepare dumps or source material before discovery;
- `before_archive`: quiesce an application after changes are detected;
- `after_archive`: release quiescing or cleanup after source reads;
- `on_success`: notification after backup and retention succeed;
- `on_failure`: notification after a workflow failure;
- `on_unchanged`: notification after an unchanged run.

Hook failures stop the current workflow phase. If `before_check` fails, source discovery does not start. If `before_archive` fails, archive creation does not start; Vaultkeep still attempts `after_archive` so partially applied quiescing can be released, then runs `on_failure`. If archive creation fails after `before_archive` succeeds, Vaultkeep also attempts `after_archive` and then runs `on_failure`.

The notification example requires `telegram-notify` to be installed and configured. Other hooks can use any installed command. Commands without an absolute path are found using the fixed `PATH` of `/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin`.

Hooks start in `/` with no standard input. They receive `LANG=C.UTF-8` and Vaultkeep's context variables, including `VAULTKEEP_JOB`, `VAULTKEEP_RESULT`, `VAULTKEEP_STAGE`, `VAULTKEEP_FAILED_STAGE`, and `VAULTKEEP_ERROR`. Other environment variables and shell startup files are not inherited. Quote variable expansions as in the example above.

| Additional context variable | Value |
| --- | --- |
| `VAULTKEEP_CONFIG` | Selected job YAML path. |
| `VAULTKEEP_BACKUP_ID` | Backup ID when assigned. |
| `VAULTKEEP_SOURCE_DIGEST` | Source digest when available, including its `sha256:` prefix. |
| `VAULTKEEP_DESTINATION` | Effective destination root when available. |
| `VAULTKEEP_ARCHIVE`, `VAULTKEEP_BACKUP_DIRECTORY` | Archive and backup-directory paths when available. |
| `VAULTKEEP_VERSION` | Executing Vaultkeep version. |

Values not yet established for a phase are empty strings.

The hook's result is the shell's exit status. Use `&&` to stop a sequence when an earlier command fails; a pipeline normally reports the last command's status. Each phase starts a separate shell, so `cd` and `export` affect only that hook.

Hook configuration is trusted executable code. Vaultkeep validates the ownership and writability of the shell and its path; commands and scripts invoked by the shell must be managed securely by the job owner. Missing commands and shell syntax errors are reported when the hook runs. Keep passwords and tokens out of command strings and arguments because they can appear in the process list.

Set variables inside a hook when they are needed. For example, `$HOSTNAME` is not supplied by Vaultkeep; assign it before using it alongside the provided `$VAULTKEEP_*` variables:

```yaml
on_failure:
  command: >-
    HOSTNAME=$(hostname) &&
    export HOSTNAME &&
    telegram-notify --level error
    "Backup failed for $VAULTKEEP_JOB on $HOSTNAME: $VAULTKEEP_ERROR"
  timeout_seconds: 60
```

The YAML `>-` joins equally indented lines with spaces, so no trailing backslashes are needed. Keep all command lines at the same indentation. Double quotes allow variable expansion and keep the message as one argument. `export HOSTNAME` makes the variable available to child processes, including scripts called by the hook. Variables set in one hook are not available in another hook.

Call your own scripts with an explicit interpreter. These examples pass the job ID as the script's first argument (`$1`):

```yaml
on_success:
  command: '/bin/sh /usr/local/libexec/vaultkeep/notify-backup.sh "{job}"'
  timeout_seconds: 60

on_failure:
  command: '/bin/bash /usr/local/libexec/vaultkeep/notify-backup.bash "{job}"'
  timeout_seconds: 60
```

Replace these paths with your scripts. Use `/bin/sh` for a POSIX shell script and `/bin/bash` for a script using Bash features. With an explicit interpreter, the script must be readable by the job's execution user; executable permission on the script is not required. Scripts also receive the `$VAULTKEEP_*` environment variables. Vaultkeep expands placeholders in `command`; inside the script, use `$1` or `$VAULTKEEP_JOB` to access the job ID.

For longer commands, YAML `|` preserves newlines, allowing several shell commands in one hook. A trailing `\` continues the same command on the next line; it must be the last character on its line, with no spaces after it. This example uses a pipeline and command substitution to include the first global-scope IPv4 address reported by `ip`. It requires `ip` from `iproute2` and `awk`:

```yaml
on_failure:
  command: |
    HOSTNAME=$(hostname) || HOSTNAME=unknown
    IP=$(ip -4 -o addr show scope global 2>/dev/null |
      awk '{split($4, address, "/"); print address[1]; exit}')
    IP=${IP:-unavailable}
    telegram-notify --level error \
      "Backup failed for {job} on $HOSTNAME (IPv4: $IP): $VAULTKEEP_ERROR"
  timeout_seconds: 60
```

This reports a local interface address. If several interfaces have addresses, it uses the first one listed. If the lookup returns no address, the notification uses `unavailable`. The shell's `${IP:-unavailable}` expansion and the single-quoted `awk` program are preserved alongside Vaultkeep's `{job}` placeholder. Choose one command per phase when adapting these examples.

## Logging

`logging.level` is required: `error`, `warning`, `info`, or `debug`.
`logging.include_command_output` defaults to `false`. Reports and actionable errors
remain visible independently of the level; see [terminal output](cli.md#terminal-output).
Hook execution summaries use `INFO`. To include captured hook stdout/stderr, enable
`include_command_output` and use `info` or `debug`. Each stream is limited to 1 MiB
and decoded as UTF-8 with replacement for invalid bytes. Enable this only when the
output contains no secrets.

## Apply configuration changes

Follow [README's update procedure](../README.md#update-a-job-configuration).
Changes to sources, exclusions, destination identity, archive/encryption settings,
password-file path, or metadata policy affect unchanged detection and can require a
new backup. Retention settings alone do not. A monitor reads its configuration once;
editing YAML alone does not refresh it or stop existing automation. Identity changes
require retiring the old monitor separately; see [Trigger identity](trigger/configuration.md#parameters-and-identity).
