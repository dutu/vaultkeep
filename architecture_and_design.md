# Vaultkeep — Reference Architecture and Design Specification

## 1. Document purpose

This document captures the agreed architecture, behavior, configuration model, command-line interface, scheduling approach, retention semantics, installation layout, and implementation boundaries for **Vaultkeep**.

It is intended to serve as:

- the initial repository design document;
- a reference architecture;
- a project continuation document for Codex;
- a basis for implementation tasks, tests, and future decisions;
- a source of truth for expected behavior.

Project naming:

- Display name: `Vaultkeep`
- Repository name: `vaultkeep`
- Python distribution name: `vaultkeep`
- Python package name: `vaultkeep`
- CLI command: `vaultkeep`
- Configuration root: `/etc/vaultkeep`
- State root: `/var/lib/vaultkeep`
- Systemd template units:
  - `vaultkeep@.service`
  - `vaultkeep@.timer`

---

## 2. Product summary

Vaultkeep is a modular Python backup application for Linux systems, initially targeting Debian with systemd.

It creates **independent, directly readable archive files** rather than a proprietary backup repository.

A backup may contain one or more files and directories. Vaultkeep supports:

- multiple source files and directories;
- source exclusions;
- content-based change detection;
- skipping unchanged backups;
- independent archive files;
- optional password protection;
- exact count-based hourly, daily, weekly, monthly, and yearly retention;
- pre-, success-, failure-, and unchanged-hooks;
- configurable archive and directory naming templates;
- optional one-directory-per-backup layout;
- strict configuration validation;
- manual execution;
- one systemd timer per job;
- deterministic schedule spreading across jobs and machines;
- semantic and PEP 440-compatible versioning;
- modular, testable implementation.

Each configuration file represents exactly one backup job.

---

## 3. Primary design goals

### 3.1 Directly readable backups

Backups must not require Vaultkeep for inspection or restoration.

Supported primary formats:

- unencrypted: `.tar.zst`
- password-protected: `.tar.7z`

On Linux, standard tools such as `tar`, `zstd`, and `7z` can be used.

On Windows, 7-Zip can open `.tar.7z` archives by:

1. opening the outer `.7z`;
2. entering the password;
3. extracting or opening the inner `.tar`.

### 3.2 Independent archives

Every retained backup must be self-contained.

Vaultkeep must not depend on:

- incremental archive chains;
- hard-linked snapshot trees;
- deduplicated repositories;
- proprietary indexes required for restoration.

### 3.3 Safe operation on mounted network shares

The normal destination may be:

- a CIFS-mounted Samba share;
- an NFS mount;
- a local filesystem.

Vaultkeep must verify that the expected destination is actually mounted before writing.

It must support an optional destination marker file to avoid accidentally writing into an unmounted local directory.

### 3.4 Strict configuration

A malformed or unknown option must prevent execution.

Vaultkeep must fail before:

- executing hooks;
- hashing source content;
- creating archives;
- applying retention;
- modifying destination content.

### 3.5 Modular implementation

The CLI and workflow must coordinate small, focused modules rather than embedding all behavior into one large script.

---

## 4. Non-goals

Initial versions will not provide:

- a proprietary repository format;
- block-level deduplication;
- incremental archive chains;
- hard-link snapshot trees;
- a GUI;
- cloud-provider APIs;
- direct SMB or NFS protocol clients;
- distributed destination locking;
- a general restore engine;
- an internal scheduler independent of systemd;
- destination-level shared locking.

Mounted filesystems remain the responsibility of the operating system.

---

## 5. Technology decisions

## 5.1 Implementation language

Use Python for the main application.

Reasons:

- strict YAML parsing and validation;
- deterministic retention logic;
- structured manifests;
- reliable subprocess orchestration;
- precise error handling;
- modular testing;
- safer handling of paths and filenames;
- maintainability compared with a large Bash script.

Bash is appropriate only for installation and small operational wrappers.

## 5.2 External utilities

Vaultkeep should orchestrate established system tools rather than reimplement archive formats.

Expected utilities:

- `tar`
- `zstd`
- `7z`
- `findmnt`
- standard filesystem commands
- systemd utilities:
  - `systemctl`
  - `systemd-analyze`

Python may calculate hashes directly through `hashlib`; it does not need to invoke `sha256sum`.

## 5.3 Configuration format

Use YAML.

YAML is preferred over INI because the configuration contains nested and repeated structures:

- multiple sources;
- per-source exclusions;
- global exclusions;
- nested archive options;
- retention tiers;
- hooks;
- scheduling;
- encryption;
- destination templates.

The parser must:

- use safe loading;
- reject duplicate keys;
- reject unknown keys;
- reject incorrect value types;
- avoid permissive type coercion;
- never construct arbitrary Python objects.

A strict typed model should be used, preferably with Pydantic configured with `extra="forbid"` and strict fields.

---

## 6. Versioning

Use PEP 440-compatible versions while retaining Semantic Versioning meaning for stable releases.

Examples:

```text
0.5.0
0.6.0.dev0
0.6.0.dev1
0.6.0a1
0.6.0b1
0.6.0rc1
0.6.0
0.6.1
1.0.0
```

Meaning for stable releases:

- `MAJOR`: incompatible CLI, configuration, archive behavior, or public API changes;
- `MINOR`: backward-compatible functionality;
- `PATCH`: backward-compatible fixes.

Before `1.0.0`, incompatible changes may occur in minor releases, but they must be clearly documented.

The authoritative version must be stored in `pyproject.toml`:

```toml
[project]
name = "vaultkeep"
version = "0.1.0.dev0"
```

At runtime, the installed version must be read with `importlib.metadata`.

The command:

```bash
vaultkeep --version
```

must output only the version:

```text
0.1.0.dev0
```

It must not include:

- the application name;
- a `v` prefix;
- explanatory text;
- logging output.

Git tags and GitHub releases should use the `v` prefix:

```text
v0.1.0.dev0
v0.1.0
v1.0.0
```

---

## 7. Job model

One configuration file represents one job.

Example layout:

```text
/etc/vaultkeep/
├── jobs/
│   ├── app.yaml
│   ├── system-config.yaml
│   └── documents.yaml
└── secrets/
    ├── app.passphrase
    └── documents.passphrase
```

All jobs use the same executable.

Manual execution:

```bash
vaultkeep --config /etc/vaultkeep/jobs/app.yaml run
```

Scheduled execution uses one systemd timer instance per job.

Example mapping:

```text
vaultkeep@app.service
→ /etc/vaultkeep/jobs/app.yaml

vaultkeep@documents.service
→ /etc/vaultkeep/jobs/documents.yaml
```

The job ID should normally match the configuration filename stem.

---

## 8. Source selection

Vaultkeep must support:

- one source directory;
- multiple source directories;
- individual files;
- any combination of files and directories;
- global exclusions;
- per-source exclusions.

Example:

```yaml
sources:
  - path: /etc/myapp

  - path: /var/lib/myapp
    exclude:
      - cache/
      - "*.tmp"

  - path: /home/user/config.json

exclude:
  - "**/.cache/**"
```

All archive paths should preserve their absolute location relative to `/`.

Example archive paths:

```text
etc/myapp/...
var/lib/myapp/...
home/user/config.json
```

The archive must not store a leading `/`.

## 8.1 Exclusion semantics

Vaultkeep should support glob-style exclusions.

Recommended rules:

- a pattern without `/` matches a file or directory name at any depth;
- a pattern containing `/` matches a path relative to the source root;
- `**` may cross directory boundaries;
- a trailing `/` indicates a directory and all descendants;
- per-source exclusions apply only to that source;
- global exclusions apply to every source.

The exact matching grammar must be documented and tested.

## 8.2 Source defaults

Recommended defaults:

```yaml
follow_symlinks: false
cross_filesystems: false
ignore_missing: false
```

Behavior:

- `follow_symlinks: false`
  - archive the symlink itself;
  - do not follow its target.

- `cross_filesystems: false`
  - do not descend into mounted filesystems below a source unless explicitly enabled.

- `ignore_missing: false`
  - a configured missing source is a fatal error.

## 8.3 Source validation

Vaultkeep must reject or warn about:

- duplicate source paths;
- overlapping sources such as `/etc/myapp` and `/etc/myapp/config`;
- destination paths located inside sources;
- sources located inside the destination;
- exclusions that remove every configured source;
- malformed glob patterns;
- non-absolute paths;
- unreadable sources;
- source type changes where relevant.

Overlapping sources should be fatal by default because they can cause duplicate archive entries.

---

## 9. Change detection

The expected maximum source size is approximately 1 GB.

Therefore, full-content hashing should be the default.

Vaultkeep should calculate one deterministic source-state digest from:

- normalized relative path;
- entry type;
- regular-file contents;
- symbolic-link target;
- selected metadata.

Recommended metadata included by default:

- mode/permissions;
- numeric user ID;
- numeric group ID.

Recommended metadata excluded by default:

- access time;
- change time;
- modification time, unless configured otherwise.

The key objective is that identical file contents and relevant metadata produce the same digest regardless of traversal order.

Entries must be sorted deterministically before digest calculation.

## 9.1 Optional metadata support

Future or optional settings may include:

- modification time;
- ACLs;
- extended attributes;
- SELinux attributes.

These settings must affect both:

- source-state hashing;
- archive creation behavior.

## 9.2 Unchanged behavior

If the newly calculated source digest matches the last successful source digest:

- do not create an archive;
- do not apply retention;
- do not modify the last successful state;
- return success;
- record the result as `unchanged`;
- run the unchanged hook, if configured.

Exit status should be `0`.

## 9.3 Consistency verification

To reduce the risk of archiving a changing source:

1. calculate the source digest;
2. create the archive;
3. calculate the source digest again;
4. compare both digests.

If the source changed while archiving:

- the backup must fail;
- the partial archive must be removed;
- the previous state must remain unchanged;
- the failure hook must run.

A future option may allow retrying the process a limited number of times.

---

## 10. Archive formats

## 10.1 Unencrypted backup

Default unencrypted format:

```text
backup-name.tar.zst
```

Pipeline:

```text
source entries
→ tar
→ zstd
→ temporary destination file
→ verification
→ atomic finalization
```

## 10.2 Password-protected backup

Default password-protected format:

```text
backup-name.tar.7z
```

Pipeline:

```text
source entries
→ tar
→ 7z compression and AES-256 encryption
→ temporary destination file
→ verification
→ atomic finalization
```

The inner TAR preserves Linux filesystem semantics better than a direct 7z archive.

Header encryption should be enabled so filenames are not visible without the password.

## 10.3 Credential handling

The password must not be placed:

- directly in the main job configuration;
- on the command line;
- in logs;
- in the destination;
- in the manifest.

Use a separate root-readable file:

```text
/etc/vaultkeep/secrets/app.passphrase
```

Recommended permissions:

```text
owner: root
group: root
mode: 0600
```

Vaultkeep must validate:

- that the file exists;
- that it is readable;
- that permissions are sufficiently restrictive;
- that it is not empty.

## 10.4 Archive integrity

Each archive should have a SHA-256 checksum sidecar:

```text
app.tar.7z
app.tar.7z.sha256
```

The checksum must cover the final archive file.

Vaultkeep should support a verification step before finalization.

Minimum verification:

- archive command completed successfully;
- final temporary file exists;
- file is non-empty;
- checksum can be calculated;
- archive listing/test succeeds where supported;
- second source digest matches the first.

---

## 11. Destination layout

Vaultkeep must support both:

- flat archive storage;
- one directory per backup.

## 11.1 One directory per backup

Example configuration:

```yaml
destination:
  root: /mnt/backups/myapp
  per_backup_directory: true
  directory_template: "backup-{job}-{timestamp_utc:%Y%m%dT%H%M%SZ}"
  archive_template: "{job}.tar.7z"
```

Example result:

```text
/mnt/backups/myapp/
├── backup-myapp-20260723T090000Z/
│   ├── myapp.tar.7z
│   ├── myapp.tar.7z.sha256
│   └── backup.json
├── backup-myapp-20260723T130000Z/
│   ├── myapp.tar.7z
│   ├── myapp.tar.7z.sha256
│   └── backup.json
└── unrelated-directory/
```

Unrelated entries must be ignored.

## 11.2 Flat layout

Example:

```yaml
destination:
  root: /mnt/backups/myapp
  per_backup_directory: false
  archive_template: "{job}-{timestamp_utc:%Y%m%dT%H%M%SZ}.tar.7z"
```

Result:

```text
/mnt/backups/myapp/
├── myapp-20260723T090000Z.tar.7z
├── myapp-20260723T090000Z.tar.7z.sha256
├── myapp-20260723T130000Z.tar.7z
├── myapp-20260723T130000Z.tar.7z.sha256
└── unrelated-file.txt
```

## 11.3 Template fields

Initial supported template fields should include:

```text
{job}
{hostname}
{timestamp}
{timestamp_utc}
{source_hash}
{format}
```

Datetime format syntax may use Python `strftime` notation:

```text
{timestamp_utc:%Y%m%dT%H%M%SZ}
```

Optional source hash truncation may be supported:

```text
{source_hash:.12}
```

## 11.4 Template constraints

Templates must:

- use only supported placeholders;
- produce a non-empty name;
- not contain `/`;
- not contain `..`;
- not contain null bytes;
- not contain control characters;
- not escape the configured destination root;
- include a timestamp in the effective backup name or directory name.

Template parsing must be reversible so Vaultkeep can discover and timestamp existing backups.

## 11.5 Destination discovery

Discovery rules:

1. list only immediate children of the destination root;
2. ignore hidden temporary entries;
3. strictly match names against the configured template;
4. ignore all nonmatching entries;
5. for matching backup directories, require the expected files;
6. validate the manifest;
7. return normalized backup records;
8. warn about malformed matching entries;
9. never apply retention to unrelated content.

Matching-but-malformed entries should be ignored by retention and reported as warnings.

They should not be automatically deleted.

---

## 12. Atomic backup creation

Temporary entries must never match final templates.

Example temporary directory:

```text
.partial-vaultkeep-myapp-20260723T090000Z-48291/
```

For per-backup directories:

1. create temporary directory under destination root;
2. create archive inside it;
3. create checksum;
4. create manifest;
5. verify archive and source consistency;
6. fsync where practical;
7. atomically rename the directory to its final name.

For flat layout:

1. write archive with a `.partial` or hidden temporary name;
2. create temporary checksum and manifest;
3. verify;
4. atomically rename files into their final names in a controlled sequence.

Per-backup directories provide stronger all-or-nothing visibility and are preferred.

---

## 13. Manifest and state

## 13.1 Backup manifest

Each backup directory should contain `backup.json`.

Example:

```json
{
  "manifest_version": 1,
  "application": "vaultkeep",
  "application_version": "0.1.0.dev0",
  "job": "myapp",
  "created_at": "2026-07-23T09:00:00+03:00",
  "created_at_utc": "2026-07-23T06:00:00Z",
  "source_digest": "sha256:...",
  "archive": "myapp.tar.7z",
  "archive_digest": "sha256:...",
  "archive_format": "tar.7z",
  "encrypted": true,
  "hostname": "homeserv"
}
```

The manifest must not contain secrets.

## 13.2 Local state

Job state should be stored locally, not only on the destination.

Recommended path:

```text
/var/lib/vaultkeep/jobs/<job-id>/state.json
```

State should include:

- state format version;
- last successful source digest;
- last successful backup timestamp;
- last backup path;
- last result;
- application version.

State updates must be atomic.

The destination manifests remain the primary source for retention discovery.

The local state is primarily used for efficient unchanged detection and operational history.

---

## 14. Retention model

Retention is count-based and Restic-like.

Example:

```yaml
retention:
  hourly: 24
  daily: 3
  weekly: 7
  monthly: 12
  yearly: 3
```

Meaning:

- keep the newest backup from each of the newest 24 distinct hourly buckets containing backups;
- keep the newest backup from each of the newest 3 distinct calendar-day buckets;
- keep the newest backup from each of the newest 7 distinct calendar-week buckets;
- keep the newest backup from each of the newest 12 distinct calendar-month buckets;
- keep the newest backup from each of the newest 3 distinct calendar-year buckets.

The final retained set is the union of all tier selections.

One archive may satisfy multiple tiers.

Example:

```text
backup A → hourly + daily + weekly + monthly
backup B → hourly only
backup C → yearly
backup D → selected by no tier, delete
```

## 14.1 Important retention properties

Retention counts are not elapsed-age limits.

Therefore:

- `hourly: 24` means 24 hourly recovery points, not necessarily only the last 24 clock hours;
- a backup is not deleted merely because time passes;
- if no new backups are created, retention does not remove old backups;
- there is no separate `keep-last` requirement because every enabled tier already retains the newest backup;
- at least one retention tier must be greater than zero.

A backup may be deleted only when:

- it is selected by no enabled retention tier;
- it is a fully valid Vaultkeep backup;
- the retention plan has been calculated successfully.

## 14.2 Bucket definitions

Recommended bucket definitions:

- hourly: local calendar year, month, day, hour;
- daily: local calendar date;
- weekly: ISO week-year and ISO week number;
- monthly: calendar year and month;
- yearly: calendar year.

The timezone used for bucketing should be configurable or derived from the backup timestamp.

Default: the system local timezone at backup creation.

The manifest must preserve both local and UTC timestamps.

## 14.3 Selection rule

Within each bucket, retain the newest backup.

Tier processing:

1. group valid backups by bucket;
2. choose newest archive in each bucket;
3. sort buckets newest first;
4. select the configured number of buckets;
5. combine selections across tiers;
6. calculate keep and delete sets;
7. present or log the plan;
8. delete only after a complete valid plan exists.

The retention module must not delete files directly.

It should return a `RetentionPlan`.

Deletion is performed by the destination module.

---

## 15. Hooks

Supported hooks:

```yaml
hooks:
  before_check: null
  before_archive: null
  on_success: null
  on_failure: null
  on_unchanged: null
```

Semantics:

- `before_check`
  - runs before source hashing;
  - useful for preparing dumps or snapshots.

- `before_archive`
  - runs only after changes are detected;
  - runs immediately before archive creation.

- `on_success`
  - runs after archive finalization and retention complete successfully.

- `on_failure`
  - runs after any failed run where execution had started.

- `on_unchanged`
  - runs when the job completes successfully without creating an archive.

Hooks should support:

- executable path;
- optional argument list;
- timeout;
- controlled environment;
- captured stdout and stderr;
- explicit failure policy where appropriate.

Recommended environment variables:

```text
VAULTKEEP_JOB
VAULTKEEP_CONFIG
VAULTKEEP_SOURCE_DIGEST
VAULTKEEP_DESTINATION
VAULTKEEP_ARCHIVE
VAULTKEEP_BACKUP_DIRECTORY
VAULTKEEP_RESULT
VAULTKEEP_STAGE
VAULTKEEP_ERROR
VAULTKEEP_VERSION
```

Secrets must not be exposed in hook environment variables.

Default behavior:

- pre-hook failure fails the backup;
- success-hook failure should produce a distinct non-zero exit status;
- failure-hook failure must not hide the original error;
- unchanged-hook failure should be reported distinctly.

---

## 16. Configuration validation

Validation must be strict and multi-stage.

## 16.1 Parse validation

Check:

- valid YAML;
- root is a mapping;
- duplicate keys are rejected;
- unsupported YAML constructs are rejected.

## 16.2 Schema validation

Check:

- required sections exist;
- required properties exist;
- values have exact expected types;
- unknown properties are fatal;
- unsupported config versions are fatal;
- enum values are valid;
- integer ranges are valid;
- booleans are actual booleans.

Example invalid values:

```yaml
hourly: "24"
hourly: -1
hourly: 2.5
enabled: "yes"
```

## 16.3 Semantic validation

Check cross-field relationships:

- one or more sources are configured;
- source paths are absolute;
- destination root is absolute;
- destination is not inside a source;
- sources do not overlap;
- `directory_template` is present when per-backup directories are enabled;
- effective naming includes a timestamp;
- template placeholders are supported;
- archive extension matches archive and encryption mode;
- password file is required when encryption is enabled;
- password file must not be set when encryption is disabled;
- at least one retention tier is greater than zero;
- `at` and `window` are mutually exclusive;
- weekly schedules have a valid weekday;
- monthly schedules have a valid day;
- hook definitions are structurally valid.

## 16.4 Runtime validation

Before a real backup, check:

- sources exist;
- sources are readable;
- destination exists;
- destination is mounted as expected;
- destination is writable;
- destination marker exists, when configured;
- required commands exist;
- password file exists and has acceptable permissions;
- hook executables exist and are executable;
- state directory is writable;
- temporary paths can be created;
- system clock and timezone are usable.

## 16.5 Unknown properties

Unknown properties must always be fatal.

Example:

```yaml
hooks:
  on_succes: /path/to/hook
```

Expected error:

```text
Configuration error: hooks.on_succes
Unknown property. Did you mean: hooks.on_success?
```

There should be no permissive production mode.

## 16.6 Error reporting

Validation should collect multiple errors where practical.

Example:

```text
Configuration contains 3 errors:

1. retention.hourly
   Expected a non-negative integer, received "24".

2. destination.directory_template
   Unknown placeholder: {date}.

3. hooks.on_succes
   Unknown property. Did you mean: hooks.on_success?
```

## 16.7 Validation commands

```bash
vaultkeep --config job.yaml validate
vaultkeep --config job.yaml validate --schema-only
```

`validate` performs schema, semantic, and runtime validation.

`validate --schema-only` does not require sources or destinations to be mounted.

---

## 17. CLI design

Global version command:

```bash
vaultkeep --version
```

Output:

```text
0.1.0.dev0
```

Main commands:

```bash
vaultkeep --config job.yaml run
vaultkeep --config job.yaml validate
vaultkeep --config job.yaml validate --schema-only
vaultkeep --config job.yaml list
vaultkeep --config job.yaml prune
vaultkeep --config job.yaml prune --dry-run
```

Timer commands:

```bash
vaultkeep --config job.yaml timer install
vaultkeep --config job.yaml timer update
vaultkeep --config job.yaml timer remove
vaultkeep --config job.yaml timer enable
vaultkeep --config job.yaml timer disable
vaultkeep --config job.yaml timer status
vaultkeep --config job.yaml timer next
```

Bulk timer commands:

```bash
vaultkeep timers list
vaultkeep timers sync
vaultkeep timers sync --dry-run
vaultkeep timers validate
vaultkeep timers enable-all
vaultkeep timers disable-all
```

The jobs directory may default to:

```text
/etc/vaultkeep/jobs
```

The CLI should permit an override for testing or nonstandard installations.

## 17.1 Manual and scheduled behavior

Manual and scheduled execution must invoke the same `run` workflow.

There must not be a separate scheduled execution path.

## 17.2 Logging

Default behavior:

- human-readable output on interactive terminals;
- useful structured fields in systemd journal;
- no secret values;
- clear result summary;
- verbosity controlled through CLI flags.

Potential flags:

```text
--quiet
--verbose
--debug
```

`--version` must bypass normal logging.

---

## 18. Scheduling

Scheduling is handled by systemd.

Users should not need to write or manage timer units manually.

The installer installs shared templates:

```text
vaultkeep@.service
vaultkeep@.timer
```

Vaultkeep manages per-job timer instances and drop-ins.

## 18.1 Simple user-facing schedule

Recommended structure:

```yaml
schedule:
  enabled: true
  interval: daily
  window: "01:00-05:00"
```

Supported intervals initially:

```text
hourly
daily
weekly
monthly
```

Examples:

Hourly:

```yaml
schedule:
  enabled: true
  interval: hourly
  window: "00:05-00:55"
```

Daily:

```yaml
schedule:
  enabled: true
  interval: daily
  window: "01:00-05:00"
```

Weekly:

```yaml
schedule:
  enabled: true
  interval: weekly
  day: sunday
  window: "01:00-06:00"
```

Monthly:

```yaml
schedule:
  enabled: true
  interval: monthly
  day: 1
  window: "01:00-06:00"
```

Exact scheduling may be supported as an alternative:

```yaml
schedule:
  enabled: true
  interval: daily
  at: "03:30"
```

`at` and `window` are mutually exclusive.

## 18.2 Deterministic staggering

Vaultkeep should choose a stable execution time inside the configured window based on:

```text
machine identity + job ID
```

Recommended input:

- `/etc/machine-id`;
- normalized job ID.

The deterministic hash maps the job to an offset inside the window.

Properties:

- the same job on the same machine normally runs at the same time;
- different jobs on one machine are distributed;
- jobs on different machines are distributed;
- execution times do not change randomly on every run;
- shared storage peak loads are reduced.

Example:

```text
homeserv / system-config → 01:47
homeserv / app           → 03:12
nas / system-config      → 04:26
```

## 18.3 Default windows

Recommended defaults:

```text
hourly:
  00:05-00:55 within every hour

daily:
  01:00-05:00

weekly:
  Sunday, 01:00-06:00

monthly:
  day 1, 01:00-06:00
```

A minimal schedule may therefore be:

```yaml
schedule:
  interval: daily
```

## 18.4 Timer behavior

Generated timers should use:

- calendar scheduling;
- persistence after downtime;
- an appropriate accuracy;
- the stable time calculated by Vaultkeep.

Because Vaultkeep calculates the stable execution point itself, users do not need to configure systemd random-delay settings.

The generated timer should be inspectable through standard systemd tools.

## 18.5 Timer lifecycle helper

`timer install` should:

1. validate the configuration;
2. calculate the timer schedule;
3. create the per-instance drop-in;
4. reload systemd;
5. enable and start the timer;
6. verify the timer;
7. show the next run.

`timer update` should recalculate and rewrite the generated timer.

`timer remove` should:

1. stop and disable the timer;
2. remove only Vaultkeep-owned generated files;
3. reload systemd;
4. leave the job configuration unchanged.

`timers sync` should:

- scan configured jobs;
- validate all jobs;
- create or update enabled timers;
- disable/remove generated timers for deleted or disabled jobs;
- never modify unrelated units;
- support `--dry-run`.

Vaultkeep should maintain an ownership registry:

```text
/var/lib/vaultkeep/systemd-instances.json
```

---

## 19. Systemd units

## 19.1 Service template

Conceptual unit:

```ini
[Unit]
Description=Vaultkeep backup job %i
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/local/bin/vaultkeep --config /etc/vaultkeep/jobs/%i.yaml run
User=root
Group=root
```

Hardening options should be evaluated during implementation.

Because backup jobs may need access to arbitrary system files and mounted shares, overly restrictive sandboxing can break valid jobs.

## 19.2 Timer template

The timer template may contain common defaults, with job-specific schedule values supplied through generated drop-ins.

Each job should have an independent timer instance.

---

## 20. Locking

Each job must have a local lock to prevent overlapping runs of the same configuration.

The lock identity should be derived from:

- canonical config path;
- job ID.

Recommended path:

```text
/run/lock/vaultkeep/<job-id>.lock
```

Different jobs may run concurrently.

No destination-level shared lock is required.

If the same job is already running:

- the second invocation should exit without starting another backup;
- return a distinct exit code;
- log the existing lock condition.

---

## 21. Modular architecture

Recommended package structure:

```text
vaultkeep/
├── __init__.py
├── __main__.py
├── cli/
│   ├── parser.py
│   ├── commands.py
│   └── output.py
├── application/
│   ├── run_job.py
│   ├── validate_job.py
│   ├── prune_job.py
│   └── timers.py
├── config/
│   ├── loader.py
│   ├── models.py
│   ├── duplicate_keys.py
│   └── versions.py
├── validation/
│   ├── schema.py
│   ├── semantic.py
│   └── runtime.py
├── sources/
│   ├── discovery.py
│   ├── exclusions.py
│   ├── entries.py
│   └── hashing.py
├── archive/
│   ├── base.py
│   ├── tar_zstd.py
│   ├── tar_7z.py
│   └── verification.py
├── destination/
│   ├── filesystem.py
│   ├── templates.py
│   ├── discovery.py
│   ├── finalize.py
│   └── pruning.py
├── retention/
│   ├── buckets.py
│   ├── planner.py
│   └── models.py
├── hooks/
│   ├── runner.py
│   └── context.py
├── state/
│   ├── manifest.py
│   ├── local_state.py
│   └── atomic.py
├── scheduling/
│   ├── model.py
│   ├── staggering.py
│   ├── systemd.py
│   └── registry.py
├── system/
│   ├── commands.py
│   ├── mounts.py
│   ├── locking.py
│   └── filesystem.py
├── logging/
│   └── setup.py
└── errors.py
```

This structure is conceptual. Avoid splitting modules into files that contain only trivial wrappers.

## 21.1 Layering

```text
CLI
↓
Application workflows
↓
Domain modules
↓
Filesystem and subprocess adapters
```

Rules:

- CLI contains no backup business logic.
- Application workflows coordinate modules.
- Retention does not scan or delete files.
- Destination discovery does not decide retention.
- Archive writers do not know scheduling behavior.
- Hook runner does not parse configuration directly.
- Modules communicate through typed data structures.

## 21.2 Core typed models

Potential domain models:

```text
JobConfig
SourceConfig
SourceEntry
SourceSnapshot
ArchiveResult
BackupRecord
RetentionPolicy
RetentionPlan
HookContext
ScheduleConfig
TimerPlan
ValidationIssue
```

---

## 22. Main workflow

The `run` workflow should remain concise:

```text
parse CLI
→ load config
→ validate schema and semantics
→ acquire job lock
→ runtime preflight
→ run before_check hook
→ discover source entries
→ calculate source digest
→ compare with last successful digest
→ if unchanged:
     run unchanged hook
     return success
→ run before_archive hook
→ create temporary backup
→ verify archive
→ calculate source digest again
→ verify source consistency
→ write checksum and manifest
→ atomically finalize
→ discover valid backups
→ calculate retention plan
→ prune obsolete backups
→ atomically update local state
→ run success hook
→ return success
```

Failure path:

```text
capture original error
→ remove partial artifacts
→ preserve previous state
→ run failure hook
→ report original and hook errors separately
→ return mapped exit code
```

---

## 23. Exit codes

Recommended initial exit code map:

```text
0   Success, including unchanged
2   Invalid command-line arguments
3   Invalid configuration
4   Source error
5   Destination or mount error
6   Pre-hook failure
7   Archive creation failure
8   Verification or source-consistency failure
9   Retention failure
10  Job lock already held
11  Success-hook failure
12  Failure-hook failure
13  Unchanged-hook failure
14  Timer management failure
15  State or manifest failure
```

The application should also expose a structured internal error hierarchy.

Exit-code stability becomes part of the public CLI contract after `1.0.0`.

---

## 24. Default configuration file

The default example configuration should contain every supported parameter.

Optional parameters should be commented out, with brief and clear explanations.

The example should be safe by default:

- it must not point to sensitive real sources;
- it must not enable a timer automatically;
- it must not enable encryption without a password file;
- it should be installed as a disabled example;
- it should pass schema-only validation.

Recommended installed file:

```text
/etc/vaultkeep/jobs/example.yaml.disabled
```

Example:

```yaml
# Vaultkeep job configuration.
# Copy this file to /etc/vaultkeep/jobs/<job-id>.yaml and edit it.
# The file name without .yaml should match job.id.

config_version: 1

job:
  # Unique job identifier. Use letters, numbers, underscores, and hyphens.
  id: example

sources:
  # Each source may be a file or directory. Paths must be absolute.
  - path: /path/to/source

    # Exclusions apply only to this source.
    # exclude:
    #   - cache/
    #   - "*.tmp"
    #   - "**/node_modules/**"

# Exclusions applied to all sources.
# exclude:
#   - "*.swp"
#   - "**/.cache/**"

source_options:
  # Archive symlinks themselves instead of following their targets.
  follow_symlinks: false

  # Do not descend into another mounted filesystem below a source.
  cross_filesystems: false

  # Fail when a configured source is missing.
  ignore_missing: false

  # Include modification time in change detection.
  # include_mtime: false

  # Preserve and hash POSIX ACLs.
  # include_acls: false

  # Preserve and hash extended attributes.
  # include_xattrs: false

destination:
  # Root directory for this job. It may be a local, CIFS, or NFS mount.
  root: /mnt/backups/example

  # Store every backup in its own directory.
  per_backup_directory: true

  # Required when per_backup_directory is true.
  directory_template: "backup-{job}-{timestamp_utc:%Y%m%dT%H%M%SZ}"

  # Archive file name inside each backup directory.
  archive_template: "{job}.tar.zst"

  # Optional marker that must exist below destination.root.
  # marker_file: ".vaultkeep-target"

  # Require destination.root to be a mount point.
  require_mount: true

archive:
  # Supported values: tar.zst, tar.7z
  format: tar.zst

  # Compression level depends on the selected format.
  # compression_level: 6

  # Verify the completed archive before finalization.
  verify: true

encryption:
  # Supported values: none, password
  mode: none

  # Required when mode is password.
  # password_file: /etc/vaultkeep/secrets/example.passphrase

  # Hide archive filenames inside the encrypted 7z container.
  # encrypt_headers: true

retention:
  # Keep the newest backup from each of the newest N hourly buckets.
  hourly: 24

  # Keep the newest backup from each of the newest N daily buckets.
  daily: 7

  # Keep the newest backup from each of the newest N ISO-week buckets.
  weekly: 8

  # Keep the newest backup from each of the newest N monthly buckets.
  monthly: 12

  # Keep the newest backup from each of the newest N yearly buckets.
  yearly: 3

hooks:
  # Runs before source hashing.
  # before_check:
  #   command: /usr/local/lib/vaultkeep/example-before-check
  #   timeout: 5m

  # Runs only when changes were detected, before archive creation.
  # before_archive:
  #   command: /usr/local/lib/vaultkeep/example-before-archive
  #   timeout: 5m

  # Runs after archive creation and retention succeed.
  # on_success:
  #   command: /usr/local/lib/vaultkeep/example-success
  #   timeout: 1m

  # Runs after a failed run.
  # on_failure:
  #   command: /usr/local/lib/vaultkeep/example-failure
  #   timeout: 1m

  # Runs when the source is unchanged and no archive is created.
  # on_unchanged:
  #   command: /usr/local/lib/vaultkeep/example-unchanged
  #   timeout: 1m

schedule:
  # When false, timer sync will not enable this job.
  enabled: false

  # Supported values: hourly, daily, weekly, monthly
  interval: daily

  # Run at a stable time selected inside this window.
  window: "01:00-05:00"

  # Alternative to window: run at one exact time.
  # at: "03:30"

  # Required only for weekly schedules.
  # day: sunday

  # Required only for monthly schedules.
  # day: 1

  # Run a missed schedule after the machine becomes available again.
  persistent: true

logging:
  # Supported values: error, warning, info, debug
  level: info

  # Include subprocess output in normal logs.
  # include_command_output: false
```

The final schema may evolve, but the principle remains:

> Every supported option appears in the default example. Optional options are commented out and briefly documented.

---

## 25. Installer

Provide an `install.sh` script.

The installer should target Linux hosts with systemd and initially support Debian through `apt`.

Support for `dnf` or `yum` may be retained if dependencies and package names are correct.

## 25.1 Recommended installation paths

```text
/opt/vaultkeep-src
/opt/vaultkeep-venv
/etc/vaultkeep
/etc/vaultkeep/jobs
/etc/vaultkeep/secrets
/var/lib/vaultkeep
/var/lib/vaultkeep/jobs
/usr/local/bin/vaultkeep
/etc/systemd/system/vaultkeep@.service
/etc/systemd/system/vaultkeep@.timer
```

## 25.2 Installer responsibilities

The installer should:

1. require root;
2. require Linux;
3. require systemd;
4. install operating-system dependencies;
5. sync the current checkout into `/opt/vaultkeep-src`;
6. create or replace the virtual environment;
7. install the Python package;
8. create runtime directories;
9. install the default disabled configuration without overwriting existing files;
10. create the secrets directory with restrictive permissions;
11. install shared systemd template units;
12. create or update the CLI symlink;
13. reload systemd;
14. verify `vaultkeep --version`;
15. validate the example configuration with `--schema-only`;
16. run `vaultkeep timers sync` for existing enabled jobs;
17. verify generated timers;
18. report completion.

It should not run an actual backup automatically during installation.

## 25.3 Upgrade safety

A stronger future installer may use versioned virtual environments:

```text
/opt/vaultkeep-venv-0.6.0
/usr/local/bin/vaultkeep
→ /opt/vaultkeep-venv-0.6.0/bin/vaultkeep
```

The symlink should switch only after installation succeeds.

This supports atomic upgrades and easier rollback.

## 25.4 Config preservation

The installer must never overwrite existing job configurations or secrets.

It may install updated examples under a versioned or `.new` name.

It must not silently rewrite user configuration.

Configuration migration belongs in explicit application commands, not automatic installer edits, unless the migration is narrowly defined and safe.

---

## 26. Repository structure

Recommended repository layout:

```text
vaultkeep/
├── pyproject.toml
├── README.md
├── DESIGN.md
├── LICENSE
├── CHANGELOG.md
├── install.sh
├── src/
│   └── vaultkeep/
├── tests/
│   ├── unit/
│   ├── integration/
│   └── fixtures/
├── examples/
│   └── vaultkeep-job.yaml
├── systemd/
│   ├── vaultkeep@.service
│   └── vaultkeep@.timer
├── scripts/
└── docs/
```

`DESIGN.md` may be this document.

---

## 27. Testing strategy

Use `pytest`.

Testing must mirror the modular design.

Core test areas:

```text
configuration parsing
duplicate YAML key rejection
unknown option rejection
strict type validation
cross-field validation
source exclusion matching
source traversal
symlink handling
cross-filesystem behavior
deterministic source hashing
template rendering
template reverse matching
destination discovery
manifest parsing
atomic finalization
archive creation
archive verification
retention bucket grouping
retention union selection
retention deletion plans
hook execution and timeouts
failure cleanup
local locking
timer staggering
systemd drop-in generation
timer registry synchronization
CLI exit codes
version output
```

## 27.1 Retention tests

Retention requires extensive table-driven tests.

Test cases should include:

- multiple backups in one hour;
- missing hours;
- sparse backups spanning years;
- one archive satisfying multiple tiers;
- all tiers set to one;
- only one enabled tier;
- timezone boundary cases;
- ISO week-year boundary;
- leap years;
- month-end;
- daylight-saving changes;
- malformed backup entries;
- unrelated destination directories;
- no deletion when no tier count is exceeded.

## 27.2 Hashing tests

Test:

- deterministic traversal order;
- content changes;
- file rename;
- permission changes;
- symlink target changes;
- ignored modification times;
- optional inclusion of modification times;
- excluded paths;
- empty directories;
- unusual filenames;
- files changing during hashing.

## 27.3 Integration tests

Use temporary local filesystems for most tests.

Where practical, add optional integration suites for:

- CIFS;
- NFS;
- systemd timers;
- real `tar`;
- real `zstd`;
- real `7z`.

These tests may be skipped unless explicitly enabled.

---

## 28. Security considerations

- Never log passwords.
- Never pass passwords as command-line arguments when avoidable.
- Restrict secret-file permissions.
- Validate destination mount and marker.
- Use safe YAML loading.
- Reject path traversal.
- Do not follow symlinks by default.
- Do not cross filesystems by default.
- Do not delete nonmatching destination content.
- Do not delete malformed backups automatically.
- Use temporary names that cannot match final templates.
- Preserve previous valid state on failure.
- Treat hook scripts as trusted administrator code.
- Quote and pass subprocess arguments without shell interpretation by default.
- Avoid `shell=True`.
- Validate generated systemd instance names.
- Maintain ownership records for generated timer files.

---

## 29. Operational behavior

## 29.1 Successful backup

A successful run should report:

- job ID;
- result;
- source digest;
- final archive path;
- archive size;
- archive checksum;
- retained backup count;
- removed backup count;
- duration.

## 29.2 Unchanged run

An unchanged run should report:

- job ID;
- result: `unchanged`;
- source digest;
- previous backup path;
- no retention performed;
- duration.

Exit status: `0`.

## 29.3 Failed run

A failed run should report:

- job ID;
- failing stage;
- concise error;
- partial cleanup result;
- failure-hook result;
- exit code.

## 29.4 Prune command

`prune` should:

- validate configuration;
- discover backups;
- calculate the retention plan;
- optionally display only with `--dry-run`;
- never require source paths to exist;
- never run archive hooks;
- refuse to delete malformed or unrelated content.

---

## 30. Open implementation decisions

The following decisions remain open and should be resolved during implementation:

1. Exact YAML validation library:
   - Pydantic plus PyYAML;
   - another strict YAML/schema combination.

2. Exact exclusion grammar:
   - Python glob semantics;
   - Gitignore-like semantics;
   - custom documented subset.

3. Archive metadata defaults:
   - whether modification time is excluded from source hashing but still preserved in TAR;
   - ACL and xattr support in the first release.

4. Flat-layout manifest naming:
   - sidecar `.json`;
   - a consistent stem-based naming convention.

5. Hook configuration:
   - simple string command;
   - structured command and argument list only;
   - support for environment additions.

6. Runtime behavior when destination contains a matching but malformed backup:
   - warning only;
   - fail pruning;
   - configurable strict mode.

7. Source-change retry policy:
   - fail immediately;
   - retry once by default;
   - configurable retries.

8. Exact systemd hardening options.

9. Whether initial releases should support only Debian or retain `dnf`/`yum` installation paths.

10. Whether unencrypted `.tar.7z` should also be offered for consistent Windows handling.

---

## 31. Initial implementation milestones

### Milestone 1 — Project foundation

- create repository;
- define `pyproject.toml`;
- add PEP 440 version;
- implement `vaultkeep --version`;
- establish package layout;
- add linting and tests;
- add strict config models.

### Milestone 2 — Validation and source discovery

- YAML duplicate-key rejection;
- strict unknown-field validation;
- semantic checks;
- source traversal;
- exclusions;
- deterministic source entries.

### Milestone 3 — Hashing and state

- full-content digest;
- metadata policy;
- local job state;
- unchanged detection;
- atomic state writes.

### Milestone 4 — Archive creation

- `.tar.zst`;
- `.tar.7z`;
- password-file handling;
- checksums;
- verification;
- cleanup and atomic finalization.

### Milestone 5 — Destination discovery and retention

- naming templates;
- reverse template matching;
- manifests;
- valid backup discovery;
- Restic-style tier retention;
- dry-run prune;
- safe deletion.

### Milestone 6 — Hooks and workflow

- hook execution;
- environment context;
- timeouts;
- error mapping;
- full run workflow.

### Milestone 7 — Scheduling

- systemd templates;
- deterministic window staggering;
- timer lifecycle commands;
- bulk timer sync;
- generated-unit registry.

### Milestone 8 — Installer and documentation

- `install.sh`;
- disabled full example config;
- service installation;
- timer synchronization;
- verification;
- README and operational documentation.

### Milestone 9 — Hardening

- integration tests;
- CIFS/NFS validation;
- permission review;
- failure injection;
- upgrade behavior;
- release process.

---

## 32. Agreed design summary

Vaultkeep will be:

- a Python application;
- installed with a Bash installer;
- configured through one strict YAML file per job;
- versioned with PEP 440 and Semantic Versioning semantics;
- version-sourced from `pyproject.toml`;
- invoked manually or through one managed systemd timer per job;
- scheduled through simple intervals and backup windows;
- deterministically staggered across machines and jobs;
- capable of backing up multiple files and directories with exclusions;
- content-hash based by default;
- able to skip unchanged backups;
- able to create independent `.tar.zst` or password-protected `.tar.7z` archives;
- readable without Vaultkeep;
- safe for mounted CIFS, NFS, or local destinations;
- configurable with flat or per-backup-directory layouts;
- strict about unknown and malformed configuration;
- Restic-like in count-based hourly/daily/weekly/monthly/yearly retention;
- modular and extensively testable;
- conservative about deletion and partial failures.
