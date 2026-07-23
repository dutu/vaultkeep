# Vaultkeep — Current Architecture, Design, and Implementation Specification

## 1. Document purpose

This document is the source of truth for the current architecture, approved behavior, implementation status, configuration model, command-line interface, retention semantics, and implementation boundaries for **Vaultkeep**.

It is intended to serve as:

- the current repository design document;
- the implementation contract for the active release scope;
- a project continuation document for Codex;
- the basis for implementation tasks and acceptance tests;
- the record of explicitly deferred future enhancements.

Requirement language is intentionally definitive:

- statements using `must` or present-tense behavior are active requirements;
- statements under a **Future enhancement** heading are not part of the active release;
- ambiguous modal language is prohibited;
- implementation status is recorded explicitly and updated when code is completed.

## 1.1 Current implementation status

Document status: **v1 design approved; project foundation in progress; product capabilities not implemented**.

Specification revision: **2026-07-23**.

The repository currently contains this specification and IDE metadata. It does not yet contain the Python package, project metadata, tests, examples, systemd units, or installer.

The workspace and repository directory are named `vaultkeep`, matching the approved project name.

Active v1 scope:

- manual `validate`, `run`, `list`, `verify`, and `prune` commands;
- unencrypted `.tar.zst` and password-protected `.tar.7z` archives;
- one directory per backup;
- YAML configuration schema version 1;
- source traversal for regular files, directories, symbolic links, and defined hard-link behavior;
- deterministic source hashing and backup-relevant configuration fingerprints;
- manifests, checksums, atomic finalization, local state recovery, and count-based retention;
- local, CIFS-mounted, and NFS-mounted destination filesystems;
- lifecycle hooks with controlled execution and fixed failure semantics;
- systemd timer management with deterministic schedule spreading;
- a root-run Debian installer with atomic virtual-environment upgrades.

Future enhancements, excluded from v1:

- ACL, extended-attribute, and SELinux-attribute preservation;
- automatic source-change retries;
- encrypted-backup password rotation within an existing job and destination namespace;
- support for package managers or operating systems beyond Debian.

## 1.2 Capability status

| Capability | Release classification | Implementation status |
|---|---|---|
| Strict YAML 1.2 configuration | v1 | Not implemented |
| Source discovery, exclusions, and hashing | v1 | Not implemented |
| `.tar.zst` archive creation and verification | v1 | Not implemented |
| Per-backup-directory destination and manifests | v1 | Not implemented |
| Local state reconciliation and unchanged detection | v1 | Not implemented |
| Count-based retention and dry-run pruning | v1 | Not implemented |
| Manual CLI and operational reporting | v1 | Not implemented |
| CIFS and NFS release validation | v1 | Not implemented |
| Password-protected `.tar.7z` | v1 | Not implemented |
| Lifecycle hooks | v1 | Not implemented |
| Extended filesystem metadata | Future enhancement | Not implemented |
| Systemd scheduling | v1 | Not implemented |
| Installer and atomic upgrades | v1 | Not implemented |

Every change that implements, removes, or reclassifies a capability must update this table in the same commit.

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

Vaultkeep is a modular Python backup application for Debian Linux systems.

It creates **independent, directly readable archive files** rather than a proprietary backup repository.

A backup contains one or more files and directories. The v1 design includes:

- multiple source files and directories;
- source exclusions;
- content-based change detection;
- skipping unchanged backups;
- independent archive files;
- password-protected archives;
- exact count-based hourly, daily, weekly, monthly, and yearly retention;
- configurable archive and directory naming templates;
- one-directory-per-backup layout;
- strict configuration validation;
- manual execution;
- scheduled execution through one systemd timer per job;
- deterministic schedule spreading across machines and jobs;
- controlled lifecycle hooks for preparation, source quiescing, cleanup, and result notification;
- Debian installation and atomic upgrades;
- semantic and PEP 440-compatible versioning;
- modular, testable implementation.

Each configuration file represents exactly one backup job.

---

## 3. Primary design goals

### 3.1 Directly readable backups

Backups must not require Vaultkeep for inspection or restoration.

The v1 archive formats are:

- unencrypted: `.tar.zst`
- password-protected: `.tar.7z`

On Debian, standard tools such as `tar`, `zstd`, and `7z` can inspect and restore v1 archives.

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

The destination is one of:

- a CIFS-mounted Samba share;
- an NFS mount;
- a local filesystem.

Vaultkeep must verify that the expected destination is actually mounted before writing.

It must support a configurable destination marker file to avoid accidentally writing into an unmounted local directory.

### 3.4 Strict configuration

A malformed or unknown option must prevent execution.

Vaultkeep must fail before:

- hashing source content;
- creating archives;
- applying retention;
- modifying destination content.

### 3.5 Modular implementation

The CLI and workflow must coordinate small, focused modules rather than embedding all behavior into one large script.

---

## 4. Non-goals

V1 does not provide:

- a proprietary repository format;
- block-level deduplication;
- incremental archive chains;
- hard-link snapshot trees;
- a GUI;
- cloud-provider APIs;
- direct SMB or NFS protocol clients;
- distributed destination locking;
- a general restore engine;
- a dynamic plugin or extension system;
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

Vaultkeep orchestrates established system tools instead of reimplementing archive formats.

V1 utilities:

- `tar`
- `zstd`
- `7z`
- `findmnt`
- `systemctl`
- `systemd-analyze`
- `rsync` for installation
- standard filesystem commands

Python calculates hashes through `hashlib`; it does not invoke `sha256sum`.

The installer obtains `/usr/bin/7z` from Debian's maintained `7zip` package. V1 invokes that absolute path and does not fall back to legacy `p7zip-full` because its stdin-password behavior is not consistent with Vaultkeep's credential-delivery contract.

## 5.3 Configuration format

Use YAML 1.2.

YAML is selected instead of INI because the configuration contains nested and repeated structures:

- multiple sources;
- per-source exclusions;
- global exclusions;
- nested archive options;
- retention tiers;
- destination templates.

The parser must:

- use safe loading;
- reject duplicate keys;
- reject unknown keys;
- reject incorrect value types;
- avoid permissive type coercion;
- never construct arbitrary Python objects.

The loader uses `ruamel.yaml` in safe, pure-Python YAML 1.2 mode with duplicate keys disabled. Parsed values are validated with Pydantic v2 models configured with `ConfigDict(extra="forbid", strict=True)`.

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

Before `1.0.0`, minor releases can contain incompatible changes. Every incompatible change must be clearly documented.

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

Git tags and GitHub releases use the `v` prefix:

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

### 7.1 Scheduled execution

Scheduled execution uses one systemd timer instance per job.

Example mapping:

```text
vaultkeep@app.service
→ /etc/vaultkeep/jobs/app.yaml

vaultkeep@documents.service
→ /etc/vaultkeep/jobs/documents.yaml
```

The job ID must match the configuration filename stem. V1 job IDs contain only ASCII letters, digits, underscores, and hyphens.

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

All archive paths preserve their absolute location relative to `/`.

Example archive paths:

```text
etc/myapp/...
var/lib/myapp/...
home/user/config.json
```

The archive must not store a leading `/`.

## 8.1 Exclusion semantics

Vaultkeep uses GitWildMatch-style exclusions through the `pathspec` library. V1 does not support negated exclusion patterns.

Rules:

- a pattern without `/` matches a file or directory name at any depth;
- a pattern containing `/` matches a path relative to the source root;
- `**` crosses directory boundaries;
- a trailing `/` indicates a directory and all descendants;
- per-source exclusions apply only to that source;
- global exclusions apply to every source.

The exact matching grammar is part of the public configuration contract and must be covered by table-driven tests.

## 8.2 Source defaults

V1 defaults:

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

Vaultkeep must reject:

- duplicate source paths;
- overlapping sources such as `/etc/myapp` and `/etc/myapp/config`;
- destination paths located inside sources;
- sources located inside the destination;
- exclusions that remove every configured source;
- malformed glob patterns;
- non-absolute paths;
- unreadable sources;
- source type changes during a run.

Overlapping sources are fatal because they can cause duplicate archive entries.

## 8.4 Source entry types

V1 archives:

- regular files;
- directories, including empty directories;
- symbolic links without following their targets;
- hard links while preserving their link relationship when every linked path is included.

V1 rejects sockets, FIFOs, block devices, and character devices with a source error. Sparse-file handling follows GNU TAR behavior and must be tested without silently expanding files beyond available destination capacity.

Paths are represented internally as raw filesystem bytes or with Python's lossless `surrogateescape` handling. Newlines, leading dashes, and non-UTF-8 filenames must not alter command parsing or archive membership.

---

## 9. Change detection

The expected maximum source size is approximately 1 GB.

Full-content hashing is the v1 change-detection method.

Vaultkeep calculates one deterministic source-state digest from:

- normalized relative path;
- entry type;
- regular-file contents;
- symbolic-link target;
- selected metadata.

V1 metadata included in the digest:

- mode/permissions;
- numeric user ID;
- numeric group ID.

V1 metadata excluded from the digest:

- access time;
- change time;
- modification time, unless configured otherwise.

Identical file contents and relevant metadata produce the same digest regardless of traversal order.

Entries must be sorted deterministically before digest calculation.

The digest encoding is versioned and collision-safe. Every field is type-tagged and length-prefixed before hashing. The digest includes the digest-format version and the source-entry type; it does not rely on ambiguous concatenation.

The same immutable `SourceSnapshot` entry list drives hashing and archive creation. Archive tools must not independently recurse through source directories.

Vaultkeep also calculates a backup-relevant configuration fingerprint. It includes source paths, exclusions, source options, destination identity, archive format, encryption mode, password-file path, and metadata policy. It excludes password contents, logging, retention counts, hooks, and scheduling. A changed fingerprint forces a new backup even when the source digest is unchanged.

For password-protected jobs, Vaultkeep also records a local credential-generation fingerprint containing the password file's device, inode, size, nanosecond modification time, and nanosecond change time. This local fingerprint is never copied into a destination manifest and never contains a digest of the password.

When that local fingerprint changes and the destination contains a valid encrypted backup for the job, Vaultkeep tests the newest such backup with the current password before unchanged detection or archive creation. A successful test establishes credential continuity and updates the local fingerprint. A failed test is a verification error and blocks the run: v1 does not mix passwords in one job and destination namespace. When no valid encrypted backup exists, the current fingerprint becomes the initial established value. Intentional password rotation requires a new job ID and destination namespace.

## 9.1 Future enhancement — additional metadata

The following metadata is excluded from v1:

- modification time;
- ACLs;
- extended attributes;
- SELinux attributes.

When implemented, each setting must affect both:

- source-state hashing;
- archive creation behavior.

## 9.2 Unchanged behavior

If the newly calculated source digest matches the last successful source digest:

- do not create an archive;
- do not apply retention;
- do not modify the last successful backup ID, path, timestamp, source digest, or configuration fingerprint;
- atomically record the `unchanged` run result, `last_unchanged_at_utc`, and an established credential-generation fingerprint;
- return success;

Exit status is `0`.

## 9.3 Consistency verification

To detect common cases of archiving a changing source:

1. calculate the source digest;
2. create the `.tar.zst` stream or the inner TAR for `.tar.7z`;
3. calculate the source digest again;
4. compare both digests.

For `.tar.7z`, encryption starts only after the second source digest matches. Source changes during 7z compression cannot alter the already completed inner TAR.

If the source changed while archiving:

- the backup must fail;
- the partial archive must be removed;
- the previous successful-backup state must remain unchanged.

This process is best-effort consistency detection for a live filesystem, not a filesystem snapshot. Applications requiring a point-in-time image must supply a stable source, such as an LVM, ZFS, or application-native snapshot.

Automatic retries are a future enhancement. V1 fails immediately when the source changes.

---

## 10. Archive formats

## 10.1 Common TAR input contract

Both archive formats use GNU TAR and the same immutable `SourceSnapshot`.

Vaultkeep converts every selected absolute source entry to its member path relative to `/`, sorts those paths as raw filesystem bytes, and supplies the complete NUL-delimited list to GNU TAR through standard input. TAR runs with `/` as its working directory and with `--null`, `--verbatim-files-from`, `--no-recursion`, and `--files-from=-`. It never performs independent directory recursion. This preserves unusual filenames, prevents leading-dash interpretation, includes empty directories explicitly, and keeps hashing and archive membership aligned.

The v1 TAR format is GNU. This matches the Debian-only runtime, supports GNU sparse handling and lossless non-UTF-8 path bytes, and remains readable by GNU TAR and 7-Zip. Archive members never have a leading slash. Duplicate member names are fatal before TAR starts.

`archive.compression_level` is an optional strict integer. It defaults to `6`; valid values are `1` through `19` for `tar.zst` and `1` through `9` for `tar.7z`. Vaultkeep maps it to the selected tool's numeric compression-level switch.

## 10.2 Unencrypted backup

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

## 10.3 Password-protected backup

Default password-protected format:

```text
backup-name.tar.7z
```

Pipeline:

```text
source entries
→ private local temporary TAR
→ source consistency verification
→ 7z compression and AES-256 encryption with encrypted headers
→ temporary destination file
→ verification
→ deletion of the local plaintext TAR
→ atomic finalization
```

The inner TAR preserves Linux filesystem semantics better than a direct 7z archive.

Header encryption is mandatory so filenames are not visible without the password.

The inner TAR is named `{job}.tar` inside the 7z archive. Vaultkeep creates it below:

```text
/var/lib/vaultkeep/tmp/<job-id>-<job-identity-hash>/<backup-id>/<job>.tar
```

Every component below `/var/lib/vaultkeep/tmp` is root-owned and mode `0700`; the TAR file is mode `0600`. Runtime preflight verifies free local space of at least the estimated TAR size plus the larger of 64 MiB or 10 percent of that estimate before reading source content. The estimate accounts for TAR block and header overhead and treats the full logical size of sparse files as required space.

The plaintext TAR is always unlinked in a `finally` cleanup path. Secure physical erasure is not guaranteed on journaling filesystems, copy-on-write filesystems, or SSDs. V1 password protection therefore protects the retained destination archive but does not claim that plaintext never reaches local storage.

Cleanup of the plaintext TAR is a pre-commit requirement. If deletion fails, Vaultkeep does not finalize the encrypted backup and reports the exact remaining path.

Vaultkeep invokes Debian's `7z` binary with argument-list subprocess execution, a private working directory, `-t7z`, `-mhe=on`, `-sccUTF-8`, and a bare `-p` switch. It sends the password plus one newline through the child process's standard-input pipe. The password is never included in argv, the environment, logs, manifest data, or hook context.

## 10.4 Credential handling

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

Required permissions:

```text
owner: root
group: root
mode: 0600
```

Vaultkeep must validate:

- the path is absolute;
- the file exists and is a regular file rather than a symbolic link;
- the file is owned by root with group root and mode exactly `0600`;
- every parent directory is not writable by group or other users;
- the file can be opened with no-follow semantics;
- the decoded UTF-8 passphrase is not empty;
- the passphrase contains no null, carriage-return, or embedded newline characters.

The password file contains one UTF-8 passphrase line. Vaultkeep removes exactly one trailing line-feed byte and preserves every other character, including leading and trailing spaces.

V1 uses one unchanged password for every encrypted backup in a job and destination namespace. Password rotation within that namespace is a future enhancement. To change the password in v1, create a new job ID and destination namespace; retain the old password file for restoring and verifying the old namespace.

## 10.5 Archive integrity

Each archive has a SHA-256 checksum sidecar:

```text
app.tar.zst
app.tar.zst.sha256
app.tar.7z
app.tar.7z.sha256
```

The checksum must cover the final archive file.

The sidecar format is one lowercase hexadecimal SHA-256 digest, two spaces, the archive basename, and a trailing newline.

Vaultkeep verifies every archive before finalization.

V1 verification:

- archive command completed successfully;
- final temporary file exists;
- file is non-empty;
- SHA-256 calculation succeeds;
- for `.tar.zst`, `zstd --test` validates the complete compressed stream;
- for `.tar.zst`, TAR listing succeeds and every member path is relative, contains no `..` component, and is unique;
- for `.tar.7z`, `7z test` reads the password through a pipe and validates the complete encrypted archive;
- for `.tar.7z`, listing without a password cannot reveal the encrypted inner member name;
- for `.tar.7z`, `7z list` confirms exactly one inner member named `{job}.tar`;
- for `.tar.7z`, `7z x -so` streams the inner TAR to `tar` without writing a second plaintext copy;
- the streamed inner TAR member list passes the same relative-path, traversal, and uniqueness checks as `.tar.zst`;
- second source digest matches the first.

---

## 11. Destination layout

Vaultkeep stores every backup in its own directory.

## 11.1 One directory per backup

Example configuration:

```yaml
destination:
  root: /mnt/backups/myapp
  directory_template: "backup-{job}-{timestamp_utc:%Y%m%dT%H%M%SZ}-{backup_id}"
  archive_template: "{job}.tar.zst"
```

Example result:

```text
/mnt/backups/myapp/
├── backup-myapp-20260723T090000Z-550e8400e29b41d4a716446655440000/
│   ├── myapp.tar.zst
│   ├── myapp.tar.zst.sha256
│   └── backup.json
├── backup-myapp-20260723T130000Z-6ba7b8109dad41d180b400c04fd430c8/
│   ├── myapp.tar.zst
│   ├── myapp.tar.zst.sha256
│   └── backup.json
└── unrelated-directory/
```

Unrelated entries must be ignored.

## 11.2 Template fields

V1 template fields are:

```text
{job}
{hostname}
{timestamp}
{timestamp_utc}
{source_hash}
{format}
{backup_id}
```

Datetime fields use Python `strftime` notation:

```text
{timestamp_utc:%Y%m%dT%H%M%SZ}
```

Source-hash fields support precision-based truncation:

```text
{source_hash:.12}
```

## 11.3 Template constraints

Templates must:

- use only supported placeholders;
- produce a non-empty name;
- not contain `/`;
- not contain `..`;
- not contain null bytes;
- not contain control characters;
- not escape the configured destination root;
- include a timestamp in the effective backup name or directory name;
- include `{backup_id}` in the v1 directory name.

`{backup_id}` prevents directory-name collisions when two backups receive the same second-resolution timestamp, the system clock moves backward, or existing backup directories are copied into the destination. It also binds the final directory name to the manifest identity. Vaultkeep never overwrites an existing final directory and never resolves a collision by appending an inferred numeric suffix.

The backup manifest is authoritative for backup identity and timestamps. Discovery validates that a directory name matches the configured template; it does not reconstruct authoritative metadata from the name.

## 11.4 Destination discovery

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

Matching-but-malformed entries are reported as errors and are never automatically deleted. Their presence blocks destructive pruning while still allowing `list` and `prune --dry-run`.

---

## 12. Atomic backup creation

Temporary entries must never match final templates.

Example temporary directory:

```text
.partial-vaultkeep-myapp-550e8400e29b41d4a716446655440000/
```

Backup creation:

1. create temporary directory under destination root;
2. create archive inside it;
3. create checksum;
4. create manifest;
5. verify archive and source consistency;
6. flush and `fsync` the archive, checksum, manifest, and temporary directory;
7. atomically rename the directory to its final name.

The final directory rename is the backup commit point. Before this point, failures remove partial artifacts and leave the previous successful state unchanged. After this point, the backup is committed and must be reported as created even if retention, local-state persistence, or later operations fail.

Temporary and final directories are on the same destination filesystem. Finalization must never overwrite an existing path. Each manifest contains a collision-resistant backup ID, and every final directory name includes that backup ID.

After writing files, Vaultkeep flushes file data and directory metadata where the destination filesystem supports it. The documented durability guarantee for CIFS and NFS is limited to behavior verified by the integration-test matrix.

---

## 13. Manifest and state

## 13.1 Backup manifest

Each backup directory contains `backup.json`.

Example:

```json
{
  "manifest_version": 1,
  "application": "vaultkeep",
  "application_version": "0.1.0.dev0",
  "backup_id": "019c...",
  "job": "myapp",
  "created_at": "2026-07-23T09:00:00+03:00",
  "created_at_utc": "2026-07-23T06:00:00Z",
  "source_digest": "sha256:...",
  "config_fingerprint": "sha256:...",
  "archive": "myapp.tar.zst",
  "archive_digest": "sha256:...",
  "archive_format": "tar.zst",
  "encrypted": false,
  "hostname": "homeserv"
}
```

The manifest must not contain secrets.

The manifest is immutable after the backup directory commit point. It records facts about archive creation and does not contain `unchanged_at` or other later run-history fields. An unchanged run must not rewrite a committed manifest.

`backup_id` is a lowercase 32-character UUIDv4 hexadecimal value generated once after change detection and reused in the temporary directory, final directory, manifest, local state, and operational output.

## 13.2 Local state

Job state is stored locally as a cache and operational record, not as the sole authority for backup existence.

State path:

```text
/var/lib/vaultkeep/jobs/<job-id>-<job-identity-hash>/state.json
```

`job-identity-hash` is the first 16 hexadecimal characters of SHA-256 over the canonical configuration path, a null separator, and the normalized job ID.

State includes:

- state format version;
- last successful source digest;
- last successful configuration fingerprint;
- current established local credential-generation fingerprint for password-protected jobs;
- last successful backup ID;
- last successful backup timestamp;
- last backup path;
- `last_unchanged_at_utc`, or null when no unchanged run has confirmed the current last successful backup;
- last result;
- last run timestamp and result, stored separately from the last successful backup;
- last-run hook phase, duration, exit, timeout, and truncation outcomes without captured output;
- application version.

State updates must be atomic.

Destination manifests are the source of truth for backup discovery and retention.

After a successful new backup, Vaultkeep sets `last_unchanged_at_utc` to null. After an unchanged run, it sets the field to the run timestamp in UTC. A failed run preserves the preceding value. State reconstructed from destination manifests starts with the field set to null because immutable manifests contain no unchanged-run history.

`last_unchanged_at_utc` means that source digest, configuration fingerprint, credential continuity where applicable, and the referenced destination backup passed the unchanged checks at that time. It does not mean that Vaultkeep performed a full archive-content verification.

Before returning `unchanged`, Vaultkeep verifies that the state matches the current configuration fingerprint, matches the current credential-generation fingerprint for a password-protected job, and references a valid backup in the current destination. Missing, corrupt, incompatible, or stale local state is reconstructed from destination manifests. Because manifests contain no credential-generation fingerprint, reconstruction for a password-protected job tests the newest valid encrypted backup with the current password before recording the current local fingerprint.

## 13.3 Missing or unusable state recovery

Vaultkeep executes without an existing `state.json`. A missing, empty, malformed, incompatible, or stale state file is a cache miss rather than a fatal application error.

Recovery is automatic and requires no CLI flag:

1. discover and classify entries in the configured destination;
2. select the newest complete, valid backup for the job using `created_at_utc` and `backup_id`;
3. reconstruct source digest, configuration fingerprint, backup ID, backup timestamp, archive path, and application version from its manifest;
4. for a password-protected job, test the selected encrypted backup with the current password and establish the current credential-generation fingerprint;
5. initialize non-reconstructable operational fields, hook outcomes, and `last_unchanged_at_utc` to null;
6. atomically write the reconstructed local state;
7. continue the normal digest and configuration comparison.

When no valid destination backup exists, Vaultkeep initializes empty local state and continues toward creation of a new backup. When valid destination state exists but does not match the current source digest or configuration fingerprint, Vaultkeep creates a new backup. When reconstruction cannot prove that returning `unchanged` is safe, Vaultkeep creates a new backup instead of skipping.

State absence never causes failure by itself. Independent safety failures still block execution, including an unavailable or unreadable destination, an unwritable state directory, or failure to establish encrypted credential continuity.

Reconstruction never modifies a destination archive or manifest. The run reports that local state was reconstructed.

---

## 14. Retention model

Vaultkeep uses count-based, calendar-bucket retention.

Each retention value defines how many distinct recovery points to keep for that tier:

```yaml
retention:
  hourly: 24
  daily: 7
  weekly: 8
  monthly: 12
  yearly: 3
```

Vaultkeep evaluates enabled tiers from coarsest to finest:

```text
yearly → monthly → weekly → daily → hourly
```

For each enabled tier, Vaultkeep:

1. limits candidates to the retention horizon established by the next enabled coarser tier;
2. groups eligible valid backups into that tier's calendar buckets;
3. selects the newest backup from each bucket;
4. retains the configured number of newest buckets;
5. establishes the horizon for the next enabled finer tier.

After every enabled tier is evaluated, Vaultkeep combines all tier selections and marks for deletion only backups selected by no tier.

A single backup can satisfy several tiers. For example, the newest backup of a day can also be the weekly and monthly recovery point.

Retention values are counts, not maximum ages. `hourly: 24` retains recovery points from the newest 24 eligible distinct hours that contain backups. Those hours can span more than 24 elapsed hours when backups are infrequent, but cannot extend beyond the horizon established by the next enabled coarser tier.

Backups are not deleted merely because time passes. If the source remains unchanged and no new backup is created, existing backups remain. The automatic `run` workflow evaluates retention only after a new backup has been successfully finalized. The explicit `prune` command evaluates the current policy without creating a backup.

A tier value of `0` disables that tier. At least one tier must be greater than zero. A separate `keep-last` setting does not exist because every enabled tier retains the newest valid backup.

Changing retention configuration does not force a backup because retention is excluded from the configuration fingerprint. Operators apply a changed retention policy with `prune` or inspect it first with `prune --dry-run`.

## 14.1 Bucket definitions

V1 bucket definitions:

- hourly: local calendar year, month, day, hour, and UTC offset;
- daily: local calendar date;
- weekly: ISO week-year and ISO week number;
- monthly: calendar year and month;
- yearly: calendar year.

V1 derives the bucketing timezone from the offset-aware local timestamp stored in the manifest. Including the UTC offset in the hourly key distinguishes a repeated daylight-saving hour.

The manifest preserves both local and UTC timestamps. “Newest” is ordered by `created_at_utc`; equal timestamps use the lowercase `backup_id` as a deterministic tie-breaker.

## 14.2 Cascading tier horizons

A finer tier cannot retain a backup older than the range represented by the next enabled coarser tier.

The tier order is:

```text
hourly < daily < weekly < monthly < yearly
```

After selecting a coarser tier, Vaultkeep identifies its oldest retained bucket. A candidate for the next enabled finer tier is eligible only when the candidate's timestamp, projected into the coarser tier's bucket type, is equal to or newer than that oldest retained bucket.

Examples:

- when `daily` is enabled, `hourly` cannot select a backup from a local date older than the oldest retained daily bucket;
- when `weekly` is the next enabled coarser tier, `daily` cannot select a backup from an ISO week older than the oldest retained weekly bucket;
- when `monthly` is the next enabled coarser tier, `weekly` cannot select a backup whose timestamp falls in a month older than the oldest retained monthly bucket;
- when `yearly` is the next enabled coarser tier, `monthly` cannot select a backup from a year older than the oldest retained yearly bucket.

A disabled tier establishes no horizon and is skipped when locating the next enabled coarser tier. An enabled tier with no enabled coarser tier is limited only by its configured bucket count. This preserves configurations with only one enabled tier.

The cascading rule limits only selection by finer tiers. A recovery point already selected by a coarser tier remains retained through the final union.

A finer tier's configured count is a maximum. The tier retains fewer recovery points when its coarser horizon contains fewer eligible buckets than the configured count.

## 14.3 Participation and deletion safety

Only complete, valid backups recognized through the configured directory template and manifest participate in retention.

Vaultkeep ignores unrelated and hidden temporary destination entries. A directory that matches the configured template but contains a malformed or incomplete backup is never automatically deleted and blocks destructive pruning until an operator resolves it. `list` and `prune --dry-run` still report the complete state.

A backup is eligible for deletion only when:

- it is selected by no enabled retention tier;
- it is a complete, valid Vaultkeep backup;
- every destination entry has been classified successfully;
- the complete retention plan has been calculated successfully.

The retention module returns a `RetentionPlan` and never deletes files directly. The destination module presents or logs the complete plan and performs only its authorized deletions.

---

## 15. Lifecycle hooks

V1 supports one command for each lifecycle phase:

```yaml
hooks:
  before_check: null
  before_archive: null
  after_archive: null
  on_success: null
  on_failure: null
  on_unchanged: null
```

The `hooks` section is optional. Omitting it is equivalent to setting every phase to `null`.

A configured hook is a strict object:

```yaml
hooks:
  before_check:
    command:
      - /usr/local/sbin/prepare-app-backup
      - --job
      - app
    timeout_seconds: 300
```

`command` is a non-empty list of strings. The first element is an absolute executable path. `timeout_seconds` is an integer from 1 through 3600 and defaults to `300`.

V1 does not support shell command strings, pipelines, redirection, variable expansion, configurable hook environments, multiple commands per phase, or configurable failure policies. An administrator combines operations in a separately managed executable when a phase requires more than one action.

## 15.1 Phase semantics

- `before_check`
  - runs after runtime preflight and before source discovery or hashing;
  - prepares dumps, snapshots, or other source material.

- `before_archive`
  - runs only after change detection determines that a backup is required;
  - runs after the backup ID and intended final paths are assigned and before archive source reads begin;
  - quiesces an application or performs final preparation;
  - does not establish a new source baseline: a modification to selected source content or hashed metadata causes source-consistency failure.

- `after_archive`
  - is armed before `before_archive` is invoked or, when no `before_archive` exists, before archive source reads begin;
  - receives exactly one execution attempt when armed: immediately after `before_archive` failure or after the source-read and second-digest phase ends;
  - also runs after archive-creation or source-consistency failure;
  - releases a quiesced application or removes temporary preparation state before destination commit.

- `on_success`
  - runs after archive finalization, committed-backup state persistence, and retention complete successfully.

- `on_failure`
  - runs once after a caught workflow failure when hook lifecycle execution has started;
  - runs after `after_archive` when that hook was armed.

- `on_unchanged`
  - runs after unchanged detection and before the final unchanged result is recorded.

When `before_check` creates state that requires cleanup, the job configures the cleanup executable in `on_success`, `on_failure`, and `on_unchanged`. Vaultkeep does not infer or synthesize that cleanup.

Hook lifecycle execution starts only after CLI, schema, semantic, runtime, hook-executable, and lock validation succeed. Invalid configuration, failed runtime preflight, or lock contention does not execute any hook. Operating-system termination, process kill, kernel failure, and power loss cannot guarantee terminal-hook execution.

## 15.2 Execution contract

Vaultkeep executes hooks directly with an argument-list subprocess call and `shell=False`.

Before execution, Vaultkeep resolves and validates the executable:

- the configured path is absolute;
- every configured and resolved path component is owned by root;
- every traversed directory is not writable by group or other users;
- every symbolic link is root-owned and resolves to a path that passes the same checks;
- the resolved target exists, is a regular executable file, and is owned by root;
- the target is not writable by group or other users;
- a script's absolute shebang interpreter and interpreter path pass the same checks;
- indirect `/usr/bin/env` shebangs are rejected.

Hooks inherit Vaultkeep's root privileges and are trusted administrator code.

Execution uses:

- working directory `/`;
- standard input connected to `/dev/null`;
- a new process group;
- a fixed minimal environment;
- captured standard output and standard error;
- a limit of 1 MiB for each captured stream.

Output beyond the limit is discarded and reported as truncated. Normal logs contain hook phase, executable path, duration, exit status, timeout status, and truncation status. Captured output is logged only when `logging.include_command_output: true`; hook authors are responsible for keeping enabled output free of secrets.

On timeout or Vaultkeep cancellation, Vaultkeep sends `SIGTERM` to the complete hook process group, waits 10 seconds, sends `SIGKILL` when processes remain, and reaps the group before continuing.

## 15.3 Hook environment

The fixed base environment contains:

```text
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
LANG=C.UTF-8
```

Vaultkeep adds:

```text
VAULTKEEP_JOB
VAULTKEEP_CONFIG
VAULTKEEP_BACKUP_ID
VAULTKEEP_SOURCE_DIGEST
VAULTKEEP_DESTINATION
VAULTKEEP_ARCHIVE
VAULTKEEP_BACKUP_DIRECTORY
VAULTKEEP_RESULT
VAULTKEEP_STAGE
VAULTKEEP_FAILED_STAGE
VAULTKEEP_ERROR
VAULTKEEP_VERSION
```

`VAULTKEEP_STAGE` is the current hook name. `VAULTKEEP_RESULT` is `running`, `created`, `unchanged`, or `failed`. `VAULTKEEP_FAILED_STAGE` identifies the primary failed workflow stage and is empty when no primary failure exists. Values that do not exist at a phase are empty strings. Archive and backup-directory values are intended final absolute paths rather than temporary paths. `VAULTKEEP_ERROR` contains a concise sanitized error summary.

Passwords, password-file paths, password-derived values, inherited environment variables, and arbitrary configuration values are absent from the hook environment.

Hook arguments are visible through the operating-system process list. Hook command lists must not contain passwords, tokens, or other secrets; a hook reads required secrets from a separately managed root-readable file.

## 15.4 Failure semantics

- A non-zero exit, timeout, signal, or launch failure from `before_check` or `before_archive` fails the pre-commit workflow with hook exit code `11`.
- `after_archive` failure fails the pre-commit workflow with exit code `11` when no earlier error exists. When an earlier error exists, that error and its exit code remain primary and the cleanup-hook failure is reported as secondary.
- `on_failure` failure never replaces the primary workflow error or triggers another hook.
- `on_success` failure leaves the committed backup and completed retention actions intact, records a post-commit hook failure, and returns exit code `11`.
- `on_unchanged` failure leaves the last successful backup state unchanged, records an unchanged-result hook failure, and returns exit code `11`.
- Failure of `on_success` or `on_unchanged` does not invoke `on_failure`.

Hook configuration is excluded from the backup-relevant configuration fingerprint. Changing a hook does not by itself create a new backup.

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
- `directory_template` is present;
- effective naming includes a timestamp;
- template placeholders are supported;
- archive extension matches `tar.zst` or `tar.7z`;
- `tar.zst` requires `encryption.mode: none` and forbids `password_file`;
- `tar.7z` requires `encryption.mode: password` and an absolute `password_file`;
- `archive.compression_level` is an integer in the range for the selected format;
- at least one retention tier is greater than zero;
- the `schedule` section is present for every job;
- `schedule.enabled` is a boolean;
- the schedule interval is hourly, daily, weekly, or monthly;
- exactly one of `schedule.at` and `schedule.window` is set;
- schedule weekday and month-day fields match the selected interval;
- each configured hook is null or contains only `command` and `timeout_seconds`;
- each hook command is a non-empty list of strings whose first element is absolute;
- hook command elements contain no null characters;
- hook timeouts are integers from 1 through 3600;
- future-enhancement fields from metadata extensions are absent.

Future configuration versions add the cross-field rules for their associated features when those features are implemented.

## 16.4 Runtime validation

Before a real backup, check:

- the effective user is root;
- the configuration is a regular root-owned file that is not writable by group or other users;
- sources exist;
- sources are readable;
- destination exists;
- destination is mounted as expected;
- destination is writable;
- destination marker exists, when configured;
- required commands exist;
- every configured hook executable and resolved parent path passes section 15 ownership, type, mode, and executability checks;
- `/usr/bin/7z` belongs to Debian's `7zip` package and passes Vaultkeep's stdin-password and encrypted-header compatibility check for `tar.7z`;
- password-file security requirements pass for `tar.7z`;
- `/var/lib/vaultkeep/tmp` is secure, writable, and has sufficient free space for `tar.7z`;
- state directory is writable;
- temporary paths can be created;
- system clock and timezone are usable.

## 16.5 Unknown properties

Unknown properties must always be fatal.

Example:

```yaml
logging:
  levle: info
```

Expected error:

```text
Configuration error: logging.levle
Unknown property. Did you mean: logging.level?
```

There is no permissive production mode.

## 16.6 Error reporting

Validation collects all independently detectable errors in one pass. Runtime checks that depend on a prior successful check stop when their prerequisite fails.

Example:

```text
Configuration contains 3 errors:

1. retention.hourly
   Expected a non-negative integer, received "24".

2. destination.directory_template
   Unknown placeholder: {date}.

3. logging.levle
   Unknown property. Did you mean: logging.level?
```

## 16.7 Validation commands

```bash
vaultkeep --config job.yaml validate
vaultkeep --config job.yaml validate --schema-only
```

`validate` performs schema, semantic, and runtime validation.

`validate --schema-only` does not require sources or destinations to be mounted.

Command-specific runtime validation is:

- `run`: sources, destination, mount, marker, state path, `tar`, and the command required by the selected archive format;
- `list`: destination, mount, marker, and manifest access; source existence is not checked;
- `verify`: all `list` checks plus archive readability, `tar`, `zstd` for `.tar.zst`, and `7z` plus password validation for `.tar.7z`;
- `prune`: all `list` checks plus destination write access unless `--dry-run` is set.

Timer-management commands additionally require root, systemd as the active system manager, systemd version 247 or newer, a managed configuration path, `systemctl`, and `systemd-analyze`.

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
vaultkeep --config job.yaml verify
vaultkeep --config job.yaml prune
vaultkeep --config job.yaml prune --dry-run
```

`--version` and `validate --schema-only` run without root privileges. Runtime validation, `run`, `list`, `verify`, `prune`, and every timer command require effective user ID `0` in the installed v1 product. Tests use explicit temporary path overrides and do not weaken this production check.

Timer commands:

```bash
vaultkeep --config /etc/vaultkeep/jobs/app.yaml timer install
vaultkeep --config /etc/vaultkeep/jobs/app.yaml timer update
vaultkeep --config /etc/vaultkeep/jobs/app.yaml timer status
vaultkeep --config /etc/vaultkeep/jobs/app.yaml timer next
vaultkeep --config /etc/vaultkeep/jobs/app.yaml timer disable
vaultkeep --config /etc/vaultkeep/jobs/app.yaml timer remove
```

Bulk timer commands:

```bash
vaultkeep timers list
vaultkeep timers sync
vaultkeep timers sync --dry-run
vaultkeep timers validate
```

The jobs directory defaults to:

```text
/etc/vaultkeep/jobs
```

The CLI provides a jobs-directory override for testing and nonstandard installations.

## 17.1 Manual and scheduled workflow parity

Manual and scheduled execution invoke the same `run` workflow.

There must not be a separate scheduled execution path.

## 17.2 Logging

Default behavior:

- human-readable output on interactive terminals;
- useful structured fields in systemd journal;
- no secret values;
- clear result summary;
- verbosity controlled through CLI flags.

V1 flags:

```text
--quiet
--verbose
--debug
```

`--version` must bypass normal logging.

---

## 18. Scheduling

Scheduling is handled by systemd, with one timer instance per backup job.

Users do not need to write or manage timer units manually.

The installer installs shared templates:

```text
vaultkeep@.service
vaultkeep@.timer
```

Vaultkeep manages per-job timer instances and drop-ins.

## 18.1 Job schedule

Configuration:

```yaml
schedule:
  enabled: true
  interval: daily
  window: "01:00-05:00"
  persistent: true
```

V1 intervals:

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
  persistent: true
```

Daily:

```yaml
schedule:
  enabled: true
  interval: daily
  window: "01:00-05:00"
  persistent: true
```

Weekly:

```yaml
schedule:
  enabled: true
  interval: weekly
  day: sunday
  window: "01:00-06:00"
  persistent: true
```

Monthly:

```yaml
schedule:
  enabled: true
  interval: monthly
  day: 1
  at: "03:30"
  persistent: true
```

`at` selects an exact local wall-clock time:

```yaml
schedule:
  enabled: true
  interval: daily
  at: "03:30"
```

`at` and `window` are mutually exclusive.

Exactly one of `at` or `window` is required. `persistent` defaults to `true`.

The `schedule` section is required even for manual-only jobs. `enabled: false` prevents timer installation and causes `timers sync` to disable an existing instance without discarding its generated schedule.

For daily, weekly, and monthly intervals, `at` and window endpoints use local `HH:MM` wall-clock time. V1 windows must end later than they start on the same day; windows crossing midnight are invalid.

For hourly intervals, `HH:MM` values are offsets within every hour. The hour component must be `00`, so `"00:05-00:55"` means from minute 5 through minute 55 of every hour.

Weekly schedules require a weekday name. Monthly schedules require a day from 1 through 28 so every calendar month contains the configured day. Other interval/day combinations are invalid.

## 18.2 Calendar rendering

Vaultkeep normalizes schedule values and renders local-time systemd calendar expressions:

| Interval | Required fields | `OnCalendar` base |
|---|---|---|
| `hourly` | `at` or `window`; hour component `00` | `*-*-* *:MM:00` |
| `daily` | `at` or `window` | `*-*-* HH:MM:00` |
| `weekly` | weekday plus `at` or `window` | `<Weekday> *-*-* HH:MM:00` |
| `monthly` | day 1–28 plus `at` or `window` | `*-*-DD HH:MM:00` |

For `at`, the rendered time comes from `at`. For `window`, the rendered time comes from the window start and `RandomizedDelaySec` equals the difference between the end and start. Calendar expressions contain no timezone suffix and therefore use the host's configured local timezone.

## 18.3 Deterministic window staggering

For `window`, systemd chooses a stable execution offset based on:

```text
machine ID + system-manager user ID + timer unit name
```

The timer unit name contains the normalized job ID. Vaultkeep maps the window start to `OnCalendar=`, its duration to `RandomizedDelaySec=`, and enables `FixedRandomDelay=yes`.

Properties:

- the same job on the same machine runs at the same stable offset;
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

## 18.4 Default schedules

Defaults used when a new example is generated:

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

## 18.5 Timer behavior

Generated timer drop-ins use:

- `OnCalendar=` for calendar scheduling;
- `Persistent=` from the job configuration;
- `AccuracySec=1us`;
- `RandomizedDelaySec=` equal to the configured window duration, or zero for `at`;
- `FixedRandomDelay=yes` for `window`, or `no` for `at`.

With `Persistent=true`, systemd triggers one catch-up run after downtime when at least one scheduled activation was missed.

The generated timer is inspectable through standard systemd tools. `systemd-analyze calendar` validates every generated `OnCalendar=` expression. `systemd-analyze verify` validates the service template and a synthetic timer instance consisting of the shared timer template plus a generated validation drop-in; the timer template intentionally contains no standalone schedule.

## 18.6 Timer lifecycle helper

`timer install` performs these operations:

1. validate the configuration;
2. calculate the timer schedule;
3. create the per-instance drop-in;
4. reload systemd;
5. enable and start the timer;
6. verify the timer;
7. show the next run.

`timer install` and `timer update` require `schedule.enabled: true` and refuse to enable an invalid or incomplete job.

`timer update` rewrites the generated drop-in, reloads systemd, preserves the timer's enabled or disabled state, verifies the result, and displays the next run.

`timer install` and `timer update` write a temporary drop-in, validate it, atomically replace the final file, and restore the preceding file and enabled state when a later lifecycle operation fails.

`timer status` reports the instance state, effective unit content, last trigger, and next trigger.

`timer next` reports systemd's effective next trigger and fails with a timer-management error when the instance is not installed.

`timer disable` stops and disables the instance, clears its persistent timestamp so intentional downtime does not produce a catch-up activation, and retains its Vaultkeep-owned drop-in.

`timer remove` performs these operations:

1. stop and disable the timer;
2. clear the timer's persistent timestamp with `systemctl clean --what=state`;
3. remove only Vaultkeep-owned generated files;
4. reload systemd;
5. leave the job configuration unchanged.

`timers sync` performs these operations:

- scan configured jobs;
- validate all jobs;
- create or update enabled timers;
- stop, disable, and clear persistent timestamps for disabled jobs while retaining their generated drop-ins;
- stop, disable, clear persistent timestamps, and remove generated timers for deleted jobs;
- never modify unrelated units;
- support `--dry-run`.

Synchronization uses a plan-then-apply transaction. Any invalid job or ownership-registry error aborts before file or systemd state changes. During apply, drop-ins and the ownership registry use atomic replacement. A failed apply restores the previous generated files, registry, and enabled states, reloads systemd, and exits with timer-management failure.

`timers list` displays every configured job, its enabled setting, installed state, active state, next trigger, and configuration drift. `timers validate` validates every job, renders every schedule, runs systemd calendar and unit verification, checks registry ownership, reports all independently detectable errors, and never mutates files or systemd state. `timers sync --dry-run` displays the exact create, update, disable, remove, and unchanged plan without applying it.

Vaultkeep maintains an ownership registry:

```text
/var/lib/vaultkeep/systemd-instances.json
```

The registry is root-owned, mode `0600`, schema-versioned, and updated atomically. It records the exact generated path and unit name for every managed instance.

Timer commands require a configuration file directly below the managed jobs directory. Its filename stem must equal `job.id`; this creates the exact mapping from `vaultkeep@<job>.timer` to `/etc/vaultkeep/jobs/<job>.yaml`.

---

## 19. Systemd units

## 19.1 Service template

Service template:

```ini
[Unit]
Description=Vaultkeep backup job %i
Wants=network-online.target
After=network-online.target
ConditionPathExists=/etc/vaultkeep/jobs/%i.yaml

[Service]
Type=oneshot
ExecStart=/usr/local/bin/vaultkeep --config /etc/vaultkeep/jobs/%i.yaml run
User=root
Group=root
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
LimitCORE=0
KillMode=control-group
TimeoutStopSec=5min
```

The service runs as root because jobs can require access to system files. Hardening that blocks arbitrary configured source or mounted destination paths is not enabled.

Because backup jobs can require access to arbitrary system files and mounted shares, the hardening policy cannot assume fixed source or destination paths.

## 19.2 Timer template

The timer template contains common `[Unit]` and `[Install]` defaults but no `OnCalendar` value. Job-specific `[Timer]` values are supplied through Vaultkeep-owned instance drop-ins below:

```text
/etc/systemd/system/vaultkeep@<job>.timer.d/schedule.conf
```

Timer template:

```ini
[Unit]
Description=Vaultkeep backup timer %i

[Timer]
Unit=vaultkeep@%i.service
AccuracySec=1us
Persistent=true

[Install]
WantedBy=timers.target
```

Generated window drop-in:

```ini
[Timer]
OnCalendar=
OnCalendar=<rendered-window-start>
RandomizedDelaySec=<window-duration>
FixedRandomDelay=yes
AccuracySec=1us
Persistent=<true-or-false>
```

Generated exact-time drop-ins use `RandomizedDelaySec=0` and `FixedRandomDelay=no`.

Each job has an independent timer instance.

---

## 20. Locking

Each job must have a local lock to prevent overlapping runs of the same configuration.

The lock identity is derived from:

- canonical config path;
- job ID.

Lock path:

```text
/run/lock/vaultkeep/<job-id>-<job-identity-hash>.lock
```

Different jobs can run concurrently.

No destination-level shared lock is required.

V1 requires each job and machine to use a unique destination namespace. Two machines or two job identities must not manage retention in the same destination root. Distributed destination locking is a future enhancement.

If the same job is already running:

- the second invocation exits without starting another backup;
- return a distinct exit code;
- log the existing lock condition.

---

## 21. Modular architecture

V1 package structure:

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
├── state/
│   ├── manifest.py
│   ├── local_state.py
│   └── atomic.py
├── scheduling/
│   ├── model.py
│   ├── systemd.py
│   └── registry.py
├── hooks/
│   ├── models.py
│   ├── validation.py
│   └── runner.py
├── system/
│   ├── commands.py
│   ├── mounts.py
│   ├── locking.py
│   └── filesystem.py
├── logging/
│   └── setup.py
└── errors.py
```

This is the v1 logical package structure. Adjacent responsibilities can share a file when separating them would create only trivial wrappers; the layering rules remain mandatory.

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
- Modules communicate through typed data structures.

## 21.2 Core typed models

Core domain models:

```text
JobConfig
SourceConfig
SourceEntry
SourceSnapshot
ArchiveResult
BackupRecord
RetentionPolicy
RetentionPlan
ScheduleConfig
TimerPlan
HookConfig
HookResult
ValidationIssue
```

---

## 22. Main workflow

The v1 `run` workflow remains concise:

```text
parse CLI
→ load config
→ validate schema and semantics
→ acquire job lock
→ runtime preflight
→ start hook lifecycle
→ run before_check
→ discover source entries
→ validate private local TAR capacity for .tar.7z
→ calculate source digest
→ calculate the backup-relevant configuration fingerprint
→ reconcile local state with destination manifests, reconstructing absent or unusable state
→ establish encrypted credential continuity when required
→ compare with the last successful digest, configuration fingerprint, and credential-generation fingerprint
→ if unchanged:
     run on_unchanged
     atomically record the final run result
     return success
→ allocate backup ID and intended final paths
→ arm after_archive
→ run before_archive
→ create a temporary .tar.zst archive or private local inner TAR
→ calculate source digest again
→ verify source consistency
→ run after_archive
→ for .tar.7z, encrypt the completed inner TAR into a temporary destination archive
→ verify the selected archive format and member structure
→ write checksum and manifest
→ remove the private local inner TAR when present
→ atomically finalize
→ atomically record the committed backup in local state
→ discover valid backups
→ calculate retention plan
→ prune obsolete backups
→ run on_success
→ atomically record the final run result
→ return success
```

Pre-commit failure path:

```text
capture original error
→ run after_archive when armed and not already completed
→ remove partial artifacts
→ preserve previous state
→ run on_failure when hook lifecycle rules permit
→ atomically record the final run result
→ return mapped exit code
```

Post-commit failure path:

```text
preserve the committed backup
→ record or reconstruct committed-backup state
→ run on_failure when hook lifecycle rules permit
→ atomically record the final run result
→ report the failed post-commit stage
→ return the mapped non-zero exit code
```

Retention deletion is idempotent but not transactional. If one deletion succeeds and a later deletion fails, Vaultkeep reports the exact partial result and never describes the already committed backup as failed or absent.

---

## 23. Exit codes

V1 exit code map:

```text
0   Success, including unchanged
2   Invalid command-line arguments
3   Invalid configuration
4   Source error
5   Destination or mount error
7   Archive creation failure
8   Verification or source-consistency failure
9   Retention failure
10  Job lock already held
11  Hook execution failure
14  Timer management failure
15  State or manifest failure
```

The application exposes a structured internal error hierarchy.

Exit-code stability becomes part of the public CLI contract after `1.0.0`.

---

## 24. Default configuration file

The default example configuration contains every v1 parameter.

Configurable parameters that are not required are commented out with brief explanations.

The example is safe by default:

- it must not point to sensitive real sources;
- it contains no future-enhancement fields;
- it is stored as a disabled example;
- it passes schema-only validation.

Repository file:

```text
examples/vaultkeep-job.yaml.disabled
```

Installed path:

```text
/etc/vaultkeep/jobs/example.yaml.disabled
```

Example:

```yaml
# Vaultkeep job configuration.
# Copy this file to /etc/vaultkeep/jobs/<job-id>.yaml and edit it.
# The file name without .yaml must match job.id.

config_version: 1

job:
  # Unique job identifier. Use letters, numbers, underscores, and hyphens.
  id: example

sources:
  # Each source is a file or directory. Paths must be absolute.
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

destination:
  # Root directory for this job. It is a local, CIFS, or NFS filesystem path.
  root: /mnt/backups/example

  # Every backup is stored in its own directory.
  directory_template: "backup-{job}-{timestamp_utc:%Y%m%dT%H%M%SZ}-{backup_id}"

  # Archive file name inside each backup directory.
  archive_template: "{job}.tar.zst"
  # For archive.format tar.7z, use: "{job}.tar.7z"

  # Optional marker that must exist below destination.root.
  # marker_file: ".vaultkeep-target"

  # Require destination.root to be a mount point.
  require_mount: true

archive:
  # Supported values: tar.zst, tar.7z.
  format: tar.zst

  # Compression level for the selected format.
  # compression_level: 6

encryption:
  # Use none with tar.zst and password with tar.7z.
  mode: none

  # Required with tar.7z. The file must be root:root mode 0600.
  # password_file: /etc/vaultkeep/secrets/example.passphrase

retention:
  # Keep the newest backup from each of the newest N eligible hourly buckets.
  hourly: 24

  # Keep the newest backup from each of the newest N eligible daily buckets.
  daily: 7

  # Keep the newest backup from each of the newest N eligible ISO-week buckets.
  weekly: 8

  # Keep the newest backup from each of the newest N eligible monthly buckets.
  monthly: 12

  # Keep the newest backup from each of the newest N yearly buckets.
  yearly: 3

schedule:
  # The installed example never activates a timer.
  enabled: false

  # Supported values: hourly, daily, weekly, monthly.
  interval: daily

  # Exactly one of window or at is required.
  window: "01:00-05:00"
  # at: "03:30"

  # Required for weekly schedules.
  # day: sunday

  # Required for monthly schedules; valid range: 1-28.
  # day: 1

  # Run one catch-up activation after downtime.
  persistent: true

hooks:
  # Each phase is null or one direct argv-based command.
  before_check: null
  before_archive: null
  after_archive: null
  on_success: null
  on_failure: null
  on_unchanged: null

  # Example hook:
  # before_check:
  #   command:
  #     - /usr/local/sbin/prepare-app-backup
  #     - --job
  #     - example
  #   timeout_seconds: 300

logging:
  # Supported values: error, warning, info, debug
  level: info

  # Include subprocess output in normal logs.
  # include_command_output: false
```

Future schema versions add fields only when their features are implemented. The v1 principle is:

> Every supported option appears in the default example. Optional options are commented out and briefly documented.

---

## 25. Debian installer

V1 provides an idempotent `install.sh` script.

The installer targets Debian Linux hosts with systemd and uses `apt`.

The supported v1 operating-system matrix is Debian 12 `bookworm` and Debian 13 `trixie`, on architectures for which the required Debian packages are available. Every supported entry must pass the installer, systemd, archive, upgrade, rollback, and restore integration suites before release.

Support for `dnf` and `yum` is excluded until a future platform-support milestone explicitly adds and tests those package managers.

## 25.1 Installation paths

```text
/opt/vaultkeep-src
/opt/vaultkeep-venv-<version>
/etc/vaultkeep
/etc/vaultkeep/jobs
/etc/vaultkeep/secrets
/var/lib/vaultkeep
/var/lib/vaultkeep/jobs
/var/lib/vaultkeep/tmp
/var/lib/vaultkeep/systemd-instances.json
/usr/local/bin/vaultkeep
/etc/systemd/system/vaultkeep@.service
/etc/systemd/system/vaultkeep@.timer
```

## 25.2 Installer responsibilities

The installer performs these operations:

1. require root;
2. require Debian and read `/etc/os-release`;
3. require systemd as the active system manager at version 247 or newer;
4. run `apt-get update`;
5. install `python3`, `python3-venv`, `tar`, `zstd`, `rsync`, `util-linux`, and `mount`;
6. install Debian's `7zip` package and fail when it has no installable candidate;
7. verify the resolved `python3`, `tar`, `zstd`, `7z`, `findmnt`, `rsync`, `systemctl`, and `systemd-analyze` executables;
8. run a private temporary compatibility check that creates, tests, lists, and streams a header-encrypted archive while supplying a generated test password through standard input;
9. synchronize the current checkout into `/opt/vaultkeep-src`;
10. create a fresh staged versioned virtual environment or verify the matching active deployment;
11. install the Vaultkeep Python package when a new staged virtual environment is required;
12. create configuration, secrets, state, temporary, and registry locations with the required ownership and permissions;
13. install the disabled example configuration without activating it or overwriting an existing example;
14. install the shared systemd service and timer templates;
15. validate the service template and a synthetic timer instance composed from the timer template and a generated validation drop-in with `systemd-analyze verify`;
16. run the staged executable with `--version`;
17. validate the inactive example with the staged executable and `validate --schema-only`;
18. create or atomically update `/usr/local/bin/vaultkeep`;
19. run `systemctl daemon-reload`;
20. run `vaultkeep timers sync` for existing managed jobs;
21. verify generated timers and report their next activations;
22. report the installed version and retained rollback version.

It does not run an actual backup automatically during installation.

The source synchronization uses `rsync --archive --delete` against the exact validated `/opt/vaultkeep-src` target and excludes `.git`, `.idea`, `.venv`, caches, build output, and runtime secrets. The installer must verify that both the source checkout and resolved target are absolute and that the target equals `/opt/vaultkeep-src` before using deletion.

The installer creates directories with these minimum restrictions:

```text
/etc/vaultkeep                 root:root 0755
/etc/vaultkeep/jobs            root:root 0750
/etc/vaultkeep/secrets         root:root 0700
/etc/vaultkeep/jobs/example.yaml.disabled root:root 0640
/var/lib/vaultkeep             root:root 0750
/var/lib/vaultkeep/jobs        root:root 0750
/var/lib/vaultkeep/tmp         root:root 0700
/var/lib/vaultkeep/systemd-instances.json root:root 0600
```

Existing files below jobs and secrets retain their ownership and modes; insecure existing job configurations and secret files are reported and rejected by runtime validation rather than silently rewritten. The installer never modifies hook executables, and runtime validation rejects insecure ones.

`/opt/vaultkeep-src`, every versioned virtual environment, both systemd templates, and the executable symlink are root-owned and not writable by group or other users.

## 25.3 Upgrade safety

The installer creates a new versioned virtual environment for every installed application version:

```text
/opt/vaultkeep-venv-0.6.0
/usr/local/bin/vaultkeep
→ /opt/vaultkeep-venv-0.6.0/bin/vaultkeep
```

Staging uses a unique root-owned directory below `/opt` that cannot equal an active environment path. The installer writes deployment metadata containing the application version and a digest of the synchronized package source. If the active deployment has the same version and source digest, rerunning the installer verifies and reuses it. If the source digest differs while the version is unchanged, installation fails and requires a version bump.

The executable symlink switches only after dependency installation, package installation, staged-executable version verification, example validation, and staged-unit verification succeed.

The previously active virtual environment remains available for rollback. Failed staging never modifies the active symlink. The installer retains the active version and one preceding version; removal of older versioned environments occurs only after resolving their exact paths below `/opt`.

Before replacing installed templates or the executable symlink, the installer records the exact preceding targets and template contents. A failure after the switch restores the preceding symlink, templates, timer registry, generated drop-ins, and enabled states, runs `systemctl daemon-reload`, verifies the restored timers, and exits non-zero. A first installation with no preceding version removes only the files it created during that failed transaction.

## 25.4 Config preservation

The installer must never overwrite existing job configurations or secrets.

Updated examples are installed under a versioned or `.new` name.

It must not silently rewrite user configuration.

Configuration migration belongs in explicit application commands, not automatic installer edits, unless the migration is narrowly defined and safe.

---

## 26. Repository structure

V1 repository layout:

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
│   └── vaultkeep-job.yaml.disabled
├── systemd/
│   ├── vaultkeep@.service
│   └── vaultkeep@.timer
├── scripts/
└── docs/
```

This document remains the authoritative design file. A future rename to `DESIGN.md` must update every repository reference in the same change.

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
destination discovery
manifest parsing
atomic finalization
archive creation
archive verification
encrypted inner-TAR cleanup
password-file validation
password-pipe handling
encrypted-header verification
credential-continuity and password-rotation rejection
configuration fingerprinting
local-state reconstruction
missing-state recovery with no destination backups
missing-state recovery to unchanged from a valid destination manifest
missing-state recovery followed by backup on digest or configuration mismatch
corrupt and incompatible state replacement
encrypted-state reconstruction and credential-continuity failure
last-unchanged timestamp transitions
retention bucket grouping
retention union selection
retention deletion plans
failure cleanup
local locking
final-name collision handling
timer schedule rendering
systemd drop-in generation
timer registry synchronization
hook schema and executable validation
hook phase ordering
hook environment isolation
hook timeout and process-group termination
hook output bounds and failure precedence
installer staging and rollback
CLI exit codes
version output
```

## 27.1 Retention tests

Retention requires extensive table-driven tests.

Required cases:

- multiple backups in one hour;
- missing hours;
- sparse backups spanning years;
- one archive satisfying multiple tiers;
- all tiers set to one;
- only one enabled tier;
- finer-tier candidates clipped by the next enabled coarser horizon;
- equality at the oldest retained coarser bucket boundary;
- disabled intermediate tiers skipped when resolving a coarser horizon;
- weekly buckets spanning month and year boundaries under cascading horizons;
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
- excluded paths;
- empty directories;
- unusual filenames;
- files changing during hashing;
- hard links;
- rejected sockets, FIFOs, and device nodes;
- lossless non-UTF-8 path handling;
- digest-format version changes;
- backup-relevant configuration changes.

## 27.3 Integration tests

Use temporary local filesystems for most tests.

V1 integration suites cover:

- CIFS;
- NFS;
- real `tar`;
- real `zstd`;
- Debian's real `7z`;
- real hook executables covering success, non-zero exit, timeout, signal, in-group descendant cleanup, and bounded output;
- systemd service and timer templates;
- timer installation, persistence, disablement, and removal;
- installer execution and upgrade rollback on supported Debian releases.

Real-tool local-filesystem tests run in continuous integration. CIFS, NFS, systemd, and full installer suites can require an explicit Debian VM or container environment, but passing them on every supported Debian release is a v1 release gate.

Encrypted-archive integration tests inspect `/proc/<pid>/cmdline` and the captured environment while `7z` runs to prove that the passphrase is absent. They prove that unauthenticated listing does not reveal the inner TAR name, test the installed Debian `7zip` binary's stdin behavior, exercise credential-continuity failures, and force interruption and command failure at every stage to verify cleanup of the private plaintext TAR.

---

## 28. Security considerations

- Validate destination mount and marker.
- Use safe YAML loading.
- Reject path traversal.
- Do not follow symlinks by default.
- Do not cross filesystems by default.
- Do not delete nonmatching destination content.
- Do not delete malformed backups automatically.
- Use temporary names that cannot match final templates.
- Preserve previous valid state on failure.
- Quote and pass subprocess arguments without shell interpretation by default.
- Avoid `shell=True`.
- Refuse finalization when the final path already exists.
- Treat local state as reconstructable cache rather than proof that a destination backup exists.
- Never log passwords or place them in command-line arguments, environment variables, manifests, checksums, or error messages.
- Pass 7z passwords only through an anonymous child-process input pipe.
- Disable core dumps before reading a password, pass only the intended pipe descriptor to 7-Zip, and close it immediately after use.
- Treat in-process password-memory erasure as best effort; Python and external tools do not provide a verifiable guarantee that every temporary copy is overwritten.
- Restrict password files and the local plaintext-TAR workspace exactly as defined in section 10.
- Remove local plaintext TAR files before the destination commit point and report any cleanup failure.
- Validate generated systemd instance names and modify only files recorded in the ownership registry.
- Resolve and verify every destructive installer target before synchronization or old-version cleanup.
- Treat hook configuration as root-code configuration: require secure config and executable ownership and modes.
- Execute hooks without a shell, inherited environment, inherited standard input, or password-related context.
- Terminate and reap the complete hook process group on timeout or Vaultkeep cancellation.

Hook programs execute as trusted administrator code with Vaultkeep's root privileges.

---

## 29. Operational behavior

Every `run` reports whether local state was loaded, reconstructed, or initialized empty. Reconstruction is reported at normal verbosity and does not change the final `created`, `unchanged`, or `failed` result.

## 29.1 Successful backup

A successful run reports:

- job ID;
- result;
- source digest;
- final archive path;
- archive size;
- archive checksum;
- retained backup count;
- removed backup count;
- configured hook outcomes;
- duration.

## 29.2 Unchanged run

An unchanged run reports:

- job ID;
- result: `unchanged`;
- unchanged confirmation timestamp in UTC;
- source digest;
- previous backup path;
- no retention performed;
- `on_unchanged` outcome when configured;
- duration.

Exit status: `0`.

## 29.3 Failed run

A failed run reports:

- job ID;
- failing stage;
- concise error;
- partial cleanup result;
- whether failure occurred before or after the backup commit point;
- committed backup path when a post-commit stage failed;
- primary and secondary hook failures without replacing the original workflow error;
- exit code.

## 29.4 Prune command

`prune`:

- validates configuration;
- discovers backups;
- calculates the retention plan;
- displays the plan without deletion when `--dry-run` is set;
- never requires source paths to exist;
- refuses to delete malformed or unrelated content.

## 29.5 List command

`list`:

- validates configuration without requiring source paths;
- discovers valid and matching-but-malformed backup entries;
- displays backup ID, creation timestamp, archive path, archive size, source digest, and structural status;
- reports unrelated destination entries only at debug verbosity;
- never verifies full archive content, modifies state, or applies retention.

## 29.6 Verify command

`verify`:

- validates configuration without requiring source paths;
- discovers valid and matching-but-malformed backup entries;
- validates each manifest and its relationship to the directory and archive names;
- recalculates and compares the archive SHA-256 checksum;
- runs full Zstandard stream verification for `.tar.zst`;
- runs full password-based 7-Zip verification for `.tar.7z`;
- validates the outer `.tar.7z` member and streams its inner TAR for inspection;
- validates every TAR member list for duplicate, absolute, and traversal paths;
- reports every verified, failed, and structurally malformed backup;
- never modifies archives, manifests, checksums, state, or retention.

A v1 release also requires automated restore drills for `.tar.zst` and `.tar.7z`. Each drill extracts a generated archive into an empty temporary directory and compares content, paths, links, permissions, numeric ownership where permitted, and modification times with the source fixture.

---

## 30. Resolved v1 decisions

The v1 decisions are closed:

1. YAML is parsed as YAML 1.2 with `ruamel.yaml` safe, pure-Python loading and validated with strict Pydantic v2 models.
2. Exclusions use the documented GitWildMatch subset without negation.
3. TAR preserves modification times, while the source digest excludes modification time. ACLs, extended attributes, and SELinux attributes are excluded.
4. Every backup uses its own directory and contains `backup.json`.
5. V1 lifecycle hooks are `before_check`, `before_archive`, `after_archive`, `on_success`, `on_failure`, and `on_unchanged`; they use direct argument-list execution, a controlled environment, bounded output, timeouts, and fixed failure semantics.
6. A matching but malformed backup blocks destructive pruning and is never automatically deleted.
7. A source change during hashing or archiving fails immediately without an automatic retry.
8. Scheduling uses one systemd timer instance per job, native calendar timers, persistent catch-up, and deterministic fixed random delay for configured windows.
9. Debian is the only v1 target.
10. V1 archive formats are unencrypted `.tar.zst` and password-protected `.tar.7z` containing one inner TAR.
11. V1 installation uses the root-run Debian `install.sh`, versioned virtual environments, atomic executable-symlink replacement, and a retained rollback version.
12. Retention evaluates enabled tiers from yearly through hourly, and each selected coarser tier limits how far back the next enabled finer tier can retain backups.
13. Local state is a disposable cache; missing or unusable state is reconstructed automatically from valid destination manifests and never blocks execution by itself.

Future enhancements are introduced through a new configuration version, implementation, tests, documentation, and an updated status in this specification. Merely documenting a planned behavior does not make it part of the active product.

---

## 31. Initial implementation milestones

### Milestone 1 — Project foundation

Status: **In progress**.

- [x] create repository;
- [x] approve the v1 architecture and scope;
- [ ] define `pyproject.toml`;
- [ ] add PEP 440 version;
- [ ] implement `vaultkeep --version`;
- [ ] establish package layout;
- [ ] add linting and tests;
- [ ] add strict config models.

### Milestone 2 — Validation and source discovery

Status: **Not started**.

- [ ] YAML duplicate-key rejection;
- [ ] strict unknown-field validation;
- [ ] semantic checks;
- [ ] source traversal;
- [ ] exclusions;
- [ ] deterministic source entries.

### Milestone 3 — Hashing and state

Status: **Not started**.

- [ ] full-content digest;
- [ ] metadata policy;
- [ ] local job state;
- [ ] automatic missing, corrupt, incompatible, and stale state reconstruction;
- [ ] unchanged detection;
- [ ] atomic state writes.

### Milestone 4 — V1 archive creation

Status: **Not started**.

- [ ] `.tar.zst`;
- [ ] password-protected `.tar.7z` with one inner TAR;
- [ ] password-file validation and stdin-only 7-Zip credential delivery;
- [ ] credential-continuity enforcement and password-rotation rejection;
- [ ] private local plaintext-TAR lifecycle and failure cleanup;
- [ ] checksums;
- [ ] real `tar`, `zstd`, and `7z` verification;
- [ ] cleanup and atomic finalization.

### Milestone 5 — Destination discovery and retention

Status: **Not started**.

- [ ] naming templates;
- [ ] manifests;
- [ ] valid backup discovery;
- [ ] count-based calendar-bucket retention;
- [ ] cascading coarser-tier retention horizons;
- [ ] dry-run prune;
- [ ] safe deletion.

### Milestone 6 — Complete v1 workflow and CLI

Status: **Not started**.

- [ ] error mapping;
- [ ] full run workflow;
- [ ] `validate`, `run`, `list`, `verify`, and `prune`;
- [ ] operational output and exit-code tests.

### Milestone 7 — Lifecycle hooks

Status: **Not started**.

- [ ] strict hook configuration models;
- [ ] executable ownership, mode, path, and runtime validation;
- [ ] direct subprocess execution with a fixed environment;
- [ ] phase ordering and guaranteed `after_archive` attempts;
- [ ] timeout and process-group termination;
- [ ] bounded output capture and logging controls;
- [ ] failure precedence, exit-code mapping, and state reporting;
- [ ] unit and real-process integration tests.

### Milestone 8 — Scheduling and systemd

Status: **Not started**.

- [ ] shared service and timer templates;
- [ ] native hourly, daily, weekly, and monthly calendar rendering;
- [ ] exact-time and deterministic-window scheduling;
- [ ] persistent catch-up behavior;
- [ ] timer lifecycle commands and ownership registry;
- [ ] all-job list, sync, dry-run, and validation commands;
- [ ] manual and scheduled workflow parity;
- [ ] per-job concurrency lock.

### Milestone 9 — Debian installer

Status: **Not started**.

- [ ] Debian, root, systemd, and package preflight;
- [ ] dependency installation and executable verification;
- [ ] safe checkout synchronization;
- [ ] versioned virtual-environment staging;
- [ ] configuration, secret, state, and temporary directories;
- [ ] inactive example configuration;
- [ ] systemd template installation and verification;
- [ ] atomic executable-symlink switch and rollback;
- [ ] post-install version, schema, timer, and daemon-reload validation.

### Milestone 10 — V1 hardening and release

Status: **Not started**.

- [ ] real `tar`, `zstd`, and `7z` integration tests;
- [ ] CIFS and NFS validation;
- [ ] systemd timer and installer tests on every supported Debian release;
- [ ] crash and failure injection;
- [ ] restore drills;
- [ ] permission and security review;
- [ ] packaging and release process;
- [ ] README and operational documentation.

---

## 32. Active v1 design summary

V1 Vaultkeep is designed as:

- a Python application;
- configured through one strict YAML file per job;
- versioned with PEP 440 and Semantic Versioning semantics;
- version-sourced from `pyproject.toml`;
- invoked manually or through one managed systemd timer instance per job;
- capable of backing up multiple files and directories with exclusions;
- content-hash based;
- able to skip unchanged backups;
- able to reconstruct missing or unusable local state automatically from destination manifests;
- able to create independent unencrypted `.tar.zst` archives;
- able to create password-protected `.tar.7z` archives containing one inner TAR;
- readable without Vaultkeep;
- safe for mounted CIFS, NFS, or local destinations;
- configured with one directory per backup;
- strict about unknown and malformed configuration;
- count-based and calendar-bucketed for hourly/daily/weekly/monthly/yearly retention with coarser-tier horizons limiting finer-tier selections;
- equipped with controlled lifecycle hooks for preparation, source quiescing, cleanup, and result notification;
- equipped with helper commands for timer installation, updates, status, next-run reporting, disabling, removal, validation, and synchronization;
- installed and upgraded by an idempotent root-run Debian installer with an atomic executable switch and retained rollback version;
- modular and extensively testable;
- conservative about deletion and partial failures.

Extended metadata, encrypted-namespace password rotation, non-Debian support, distributed destination locking, and automatic retries remain future enhancements and are not v1 capabilities.
