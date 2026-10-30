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

Jobs use strict YAML 1.2 with `config_version: 2`. Unknown properties and duplicate
mapping keys are rejected. Use booleans (`true`/`false`) and integers as their actual
YAML types, and quote times and patterns. The YAML filename stem must match `job.id`, except when Trigger is enabled and
Schedule is disabled. Trigger identifies its service by canonical YAML path; a changed
`job.id` requires an explicit safety-checked `trigger update`.
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
| `consistency` | No | `files` (default) or `application_offline`. |
| `application` | Conditional | Shared application name, owner, directory, and matcher. |
| `trigger` | No | Disabled by default; [complete Trigger settings](trigger/configuration.md). |
| `desktop_notifications` | No | Automatic desktop alerts, disabled by default; [settings and setup](notifications.md). |
| `external_notifier` | Conditional | Executable for explicit `vaultkeep notify` calls; [settings](#external-notifications). |
| `hooks` | No | Six optional phases; each defaults to `null`. |
| `logging` | Yes | Required `level`, optional `include_command_output`. |

Each [job example](../README.md#create-a-backup-job) shows every supported parameter,
with optional settings commented out where appropriate. The
[annotated general example](../examples/vaultkeep-schedule.yaml) explains
their values. This complete manual-only job illustrates the shared structure:

```yaml
config_version: 2

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

## Consistency and shared application identity

`consistency.mode` is `files` by default. It preserves source discovery, hashing,
final source comparison, archive structure/stream tests, and checksums, without
inspecting application processes. `application_offline` additionally requires reliable
stopped observations before source discovery, after `before_archive`, and after all
source reads but before `after_archive`. Running, ambiguous, or unavailable process
information fails the backup. These boundaries cannot prove clean shutdown or exclude
short restarts between observations.

```yaml
consistency:
  mode: application_offline
application:
  name: bisq
  data_dir: /home/alice/.local/share/Bisq
  matcher:
    entrypoints:
      - kind: java_main
        value: bisq.desktop.app.BisqAppMain
```

`application` is required for offline policy or an enabled Trigger, and optional
otherwise. A present identity requires `name`, `data_dir`, and `matcher`. Supported
names are `bisq`, `electrum`, and `wasabi`: `name` selects the application's
process-data-directory rules; `matcher` identifies its processes. The name cannot
be inferred reliably from arbitrary script paths, JARs, or Java main classes.

Omit `application.user` for user-mode jobs. Vaultkeep resolves the effective
execution UID through the operating-system account database, as it does for the
owning systemd user manager and installer refresh. YAML ownership, configuration
location, `HOME`, `USER`, and `SUDO_USER` do not select the account. An explicit
`application.user` is unsupported in user mode. Full validation reports the
property and absolute configuration path before looking up the supplied account.
Schema-only validation checks structure without resolving execution accounts.

Root mode requires root privileges and defaults to root ownership. To inspect another user's
application in a root-mode manual or scheduled offline backup, set
`application.user: alice` explicitly. Trigger remains non-root user mode only.
The account must exist; its canonical data directory must be owned by that account.
Home paths expand against the resolved account, including root backups targeting
another user. The engine and Trigger share the same
matcher and process provider. See [matcher options](trigger/configuration.md#matcher).

Trigger settings contain only enablement, dispatch parameters, and monitoring timing.
With `files`, Trigger still confirms shutdown before dispatch; the engine performs
file checks without enforcing continued process absence. Manual, scheduled, and
Trigger backups all enforce the selected policy. Each run retains one effective
configuration; a worker rejects changes to its frozen policy/application identity.

Schema and manifest/layout version 2 require fresh configuration, destination, and
local state. There is no version-1 migration or legacy marker interpretation.
Recognized unsupported entries block destructive cleanup; preserve old repositories
separately rather than relabeling their manifests.

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

A demonstrated checksum or archive-structure problem records a
`.vaultkeep-suspect` marker with its original reason and detection time. `list`
shows the `SUSPECT` reason directly below the backup directory. An unresolved
suspect is protected and forces a fresh archive in the same compatible history.
After reliable creation, required state persistence, and successful hooks, the engine
records the replacement ID and may remove the superseded suspect. Suspects do not
fill ordinary retention slots. Ordinary cleanup failures preserve the new archive and report
`run: created` with an actionable warning; retry with explicit `prune` or a later
archive-creating run. `UNCHANGED` never performs cleanup.
Corruption found in the newly committed archive fails the run, including when
discovered during cleanup.

Replacement compatibility requires the same configured source coverage and
consistency policy; offline backups also require the same application name, owner,
and data directory. Changing compression, retention, archive format, or password-file
location does not define a different replacement class. Changed coverage or policy
preserves the incompatible history. A missing or suspect replacement makes the old
suspect unresolved again. Deletion rechecks the replacement's current SHA-256
against both the immutable manifest and sidecar under the shared destination lock.
Replacement validity does not depend on wall-clock ordering. Both `manifest_version: 2`
and `identity_version: 1` in its replacement identity are required; missing or invalid
versions make the entry malformed and block destructive work in that history.
`list`, `status`, and `prune --dry-run` inspect metadata without reading archives or
writing markers. `verify` performs full archive verification and may mark demonstrated
corruption; wrong passwords, unavailable tools, and transient I/O errors do not by
themselves establish corruption.

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

Mutating `run`, `verify`, and actual `prune` acquire an exclusive file lock on
`<destination.root>/.vaultkeep.lock` before inspecting or modifying that history.
The lock is independent of YAML path, job ID, execution account, and local mount
path: all writers accessing the same physical history must reach the same lock
file. Different history directories can be processed concurrently.

Supported Linux local filesystems are ext2/3/4, XFS, Btrfs, ZFS, tmpfs, and overlay.
Mounted CIFS/SMB (`cifs`, `smb3`) and NFS (`nfs`, `nfs4`) shares require server-backed
locking as described below. Other filesystems are rejected for mutation. Subtree
mounts, bind-subtree aliases, and mounted Btrfs subvolumes are supported; mounts
nested inside a destination remain unsupported. Mount topology must remain stable
during execution.

For `per_file` jobs, each child history has its own `.vaultkeep.lock`. Backup,
verification, and pruning lock one child at a time through discovery, archive
checks, marker updates, and cleanup, then release it before proceeding. A job
targeting that child directly uses the same lock. Busy or failed histories are
reported while independent siblings continue; the overall command reports failure.
The batch does not reserve all children together. The parent directory's writable
probe uses a short-lived parent lock and does not reserve its child histories.

Vaultkeep creates `.vaultkeep.lock` when absent, with permissions subject to the
creating process's umask. The history directory must permit creation, or an
administrator must provision the file first. Every backup account needs read/write
access through filesystem permissions, server ACLs, or a shared group. Existing
contents and permissions are preserved. The file remains after success or failure
and is reused; its presence or contents do not establish that a writer is active.
Never remove or replace it while writers may run. Symlinks, hard links, and
non-regular lock files are rejected. Marker and filesystem guards precede lock-file
creation and the writable-destination probe. Lock contention fails without waiting;
a later invocation can retry. Read-only inventory and prune previews create no locks.

### CIFS and NFS locking

Every cooperating writer must access the same physical history directory and lock
file through the same server locking domain. Whole-share and share-subdirectory
mounts are supported, including different local mount paths. Separate SMB/NFS
exports or different protocols exposing the same data may use independent lock
domains and must not be used concurrently for the same history. Access to the
share/export root is not required.

Keep server locking enabled. CIFS `nobrl`, NFS `nolock`, and NFS
`local_lock=all`/`local_lock=flock` are rejected. Lock/open failures stop the operation
without a fallback to machine-local locks. All clients and the server must support
the protocol's locks; local tests cannot establish cross-machine server behavior.
Server recovery, permissions and mount topology must remain suitable throughout
the operation. Backup durability also depends on the server honoring flush requests.
See the Linux [flock reference](https://man7.org/linux/man-pages/man2/flock.2.html),
[CIFS mount options](https://man7.org/linux/man-pages/man8/mount.cifs.8.html), and
[NFS mount options](https://man7.org/linux/man-pages/man5/nfs.5.html).

Linux NFS rejects `renameat2(RENAME_NOREPLACE)`. While the history lock is
held, Vaultkeep falls back to checking that the final path is absent and then
performing an ordinary atomic directory rename. Existing nonempty backup
directories cannot be replaced. A non-cooperating process racing to create an
empty target directory between those operations can have that empty directory
replaced. Other rename errors still fail. Local/CIFS commits retain the native
no-overwrite operation. Full validation acquires the destination lock for its
write/rename/remove probe, and reuses the exact history lock during a backup run.

### Marker and filesystem checks

`destination.root` must be an existing directory. Two optional safety checks can
be configured independently: use either, both, or neither.

- `marker_file` confirms that a designated marker exists as a regular file below
  `destination.root`. Use a relative path without `..` components. Vaultkeep does
  not check its contents or create it automatically. A missing or inaccessible
  marker blocks destination writes.
- `expected_fstype` verifies the actual filesystem backing `destination.root` as
  seen by Vaultkeep, including inside a container. Use the exact, case-sensitive
  kernel filesystem name reported by `findmnt`, as described below. A matching
  type does not establish shared-lock suitability.

#### `expected_fstype` values

For destinations supported by mutating operations, use one of these reported values:

| Filesystem | `expected_fstype` |
| --- | --- |
| ext2, ext3, ext4 | `ext2`, `ext3`, or `ext4`, respectively |
| XFS | `xfs` |
| Btrfs | `btrfs` |
| ZFS | `zfs` |
| tmpfs | `tmpfs` |
| Overlay filesystem | `overlay` |
| CIFS/SMB share | `cifs` or `smb3`, matching the reported value |
| NFS share | `nfs` or `nfs4`, matching the reported value |

Inspect the destination from the same host/container and mount namespace that runs
Vaultkeep:

```sh
findmnt --kernel --uniq --noheadings --raw --output FSTYPE --target /mnt/backups/app
```

For example, if this prints `cifs`, set `expected_fstype: cifs`. These names select
the filesystem check, not the network protocol version or mount options. Values
such as `smb`, `SMB3`, or `NFSv4` do not substitute for the reported name.

The schema accepts a single nonempty name containing letters, digits, `.`, `_`, or
`-`; it is not a fixed enumeration. Other names can be used for inspection, but
setting `expected_fstype` does not enable mutation on an unsupported filesystem.
Lists and comma-separated alternatives are not supported. Omit the field or set
it to YAML `null` to skip this optional type check; destination locking and the
other safety checks still apply.

Use a marker and filesystem type together when a missing mount could expose an
underlying fallback directory:

```yaml
destination:
  root: /mnt/backups/app
  name_template: "backup-{job}-{timestamp_utc:%Y%m%dT%H%M%SZ}"
  expected_fstype: ext4
  marker_file: .vaultkeep-destination
```

1. Inspect the filesystem from the environment running Vaultkeep:

   ```sh
   findmnt --uniq --target /mnt/backups/app --output TARGET,SOURCE,FSTYPE
   ```

   Confirm the intended device or share and filesystem. Subdirectory mounts are
   supported; mounts nested inside the destination are rejected for mutation.

2. Create the marker only after confirming that destination:

   ```sh
   touch /mnt/backups/app/.vaultkeep-destination
   ```

   Keep the marker absent from any underlying fallback directory.

3. Validate the job, then execute it to check mutation-time locking:

   ```sh
   sudo vaultkeep --config /etc/vaultkeep/jobs/app.yaml validate
   sudo vaultkeep --config /etc/vaultkeep/jobs/app.yaml run
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
integers at least zero; zero disables a tier and at least one tier must be enabled
for validation, backup execution, and pruning. Read-only `list` can inspect an
all-zero policy as disabled without assigning retention decisions.
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
external_notifier:
  command: telegram-notify
  timeout_seconds: 15

hooks:
  before_check:
    command: '/usr/local/sbin/prepare-app-backup --job "{job}"'
    timeout_seconds: 300

  before_archive: null
  after_archive: null
  on_success:
    command: 'vaultkeep notify --level info "Backup completed for {job} on {hostname}"'
    timeout_seconds: 60
  on_failure:
    command: 'vaultkeep notify --level error "Backup failed for {job} on {hostname}: $VAULTKEEP_ERROR"'
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
- `on_success`: required hook after archive commit and state persistence, before recording suspect replacements and running cleanup;
- `on_failure`: notification after a workflow failure;
- `on_unchanged`: notification after an unchanged run.

Hook failures stop the current workflow phase. If `before_check` fails, source discovery does not start. If `before_archive` fails, archive creation does not start; Vaultkeep still attempts `after_archive` so partially applied quiescing can be released, then runs `on_failure`. If archive creation fails after `before_archive` succeeds, Vaultkeep also attempts `after_archive` and then runs `on_failure`.

The external notification examples use [best-effort delivery](#external-notifications). Other hooks can use any installed command. Commands without an absolute path are found using the fixed `PATH` of `/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin`.

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
    vaultkeep notify --level error
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
    vaultkeep notify --level error \
      "Backup failed for {job} on $HOSTNAME (IPv4: $IP): $VAULTKEEP_ERROR"
  timeout_seconds: 60
```

This reports a local interface address. If several interfaces have addresses, it uses the first one listed. If the lookup returns no address, the notification uses `unavailable`. The shell's `${IP:-unavailable}` expansion and the single-quoted `awk` program are preserved alongside Vaultkeep's `{job}` placeholder. Choose one command per phase when adapting these examples.

### External notifications

`desktop_notifications` controls automatic desktop alerts for Backup and Trigger.
`external_notifier` only configures an external executable; it does not schedule or
send anything automatically. `vaultkeep notify` explicitly invokes that executable,
including when called from an ordinary lifecycle hook. See the
[complete notification example](notifications.md) for all three together.

```yaml
external_notifier:
  command: telegram-notify
  timeout_seconds: 15
```

| Field | Requirement/default |
| --- | --- |
| `command` | Required executable name resolved through the execution `PATH`, or an absolute executable path. Whitespace, shell syntax, pipelines, embedded arguments, and relative paths containing `/` are rejected. |
| `timeout_seconds` | Optional finite positive number, at most 3600; default `15`. |

Unknown fields are rejected. The executable need not be installed for job
validation to succeed. Install and configure the chosen executable independently;
Vaultkeep provides no credentials, provider settings, or Telegram API integration.
Put executable arguments after `notify`, for example `vaultkeep notify --level info
"Backup completed for {job}"`. Arguments and their order are preserved exactly.

The section may be omitted when no supported direct hook invocation uses
`vaultkeep notify`. It may also be configured without any hooks. A recognizable
direct invocation without `external_notifier` is a configuration error at
`hooks.<phase>.command`, for every lifecycle phase. Validation recognizes literal
`vaultkeep` and absolute paths ending in `/vaultkeep`, supported global options
before `notify`, and simple sequences, pipelines, and shell groups. Quoted message
text is not treated as a command. This check does not execute or expand shell code.

Static detection is a safety check, not a security boundary or a complete shell
parser. Scripts, aliases, wrappers such as `sh -c`, constructed commands, complex
quoting, and commands after a here-document may not be recognized. Runtime
`vaultkeep notify` always requires valid `external_notifier` configuration,
including indirect calls. Validation never inserts settings or rewrites hooks.

1. Install/update Vaultkeep and install/configure the selected executable separately.
   From the job's execution account, verify access using the hook search path
   (replace `telegram-notify` when using another provider):

   ```sh
   env PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
     /bin/sh -c 'command -v vaultkeep telegram-notify logger'
   ```

   Expect a path for each command. Hooks have a fixed environment and do not inherit
   interactive shell settings. `logger` provides system-log warnings from hooks.

2. Configure `external_notifier` and copy the desired hooks from the examples.
   Retain `timeout_seconds: 60` on those hooks. The helper uses its own configured
   timeout, then allows up to 1 second for termination and up to 1 more second
   after killing remaining process-group members. Failure logging also gets a
   bounded 2-second `logger` attempt. Keep the hook timeout above these budgets.
   Hook execution and nonzero-result handling remain unchanged. The helper returns
   0 for expected delivery failures; CLI/configuration mistakes remain errors.

3. Apply the [configuration update procedure](../README.md#update-a-job-configuration).
   Hooks use `VAULTKEEP_CONFIG` automatically. Start backups with an absolute
   `--config` path: hooks run from `/`, and the existing hook environment preserves
   the supplied configuration path. Test manually without backup runtime
   requirements, putting Vaultkeep options before the command:

   ```sh
   vaultkeep --user --config ~/.config/vaultkeep/jobs/app.yaml notify --level info "Test notification"
   ```

   Use the real job path; omit `--user` and use `sudo` for root-mode manual calls.
   An explicit `--config` overrides `VAULTKEEP_CONFIG`. Delivery failures warn on
   stderr. Hook failures to deliver are also logged with the `vaultkeep` tag:

   ```sh
   journalctl -t vaultkeep -n 50 --no-pager
   ```

   Use `sudo` if required to read the journal. System logging is best-effort and
   requires `logger` and a working system log service. Hook stderr follows normal
   output-capture settings. Diagnostics exclude notification arguments and provider
   output. There are no retries, queues, delivery histories, or extra configuration
   files. Exit code 0 does not establish remote delivery. See the
   [command reference](cli.md#external-notifications) for arguments and exit codes.

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
