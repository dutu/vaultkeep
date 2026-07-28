# Vaultkeep user mode guide

User mode lets a normal user configure, run, and schedule their own Vaultkeep jobs without sudo after Vaultkeep has been centrally installed by root.

User mode is for backups of files the user can already read to destinations the user can already write. It does not grant access to system files, root-owned application data, protected secrets, or system mounts.

User mode uses the shared `/usr/local/bin/vaultkeep` command with the `--user` flag:

```bash
vaultkeep --user --config ~/.config/vaultkeep/jobs/app.yaml run
```

Root mode and user mode are independent. User mode does not use `/etc/vaultkeep/jobs`, `/var/lib/vaultkeep`, `/run/lock/vaultkeep`, or root-mode system timers. A host can have root-mode jobs already installed while one or more users maintain separate user-mode jobs.

For the shared job schema, destination template rules, command surface, retention behavior, restore notes, and troubleshooting reference, see the [shared user guide](../README.md#usage).

## Administrator prerequisite

Root must install Vaultkeep and the required Debian packages first:

```bash
sudo ./install.sh install
```

The root installation installs packages and tools used by both modes, including Python, GNU TAR, Zstandard, Debian's `7zip` package, `rsync`, `util-linux`, and mount utilities.

After that, a normal user can create jobs, run jobs, and manage user timers without sudo.

## User-mode locations

Vaultkeep resolves user-mode paths from the XDG environment when available:

| Purpose | Default location |
|---|---|
| Job configurations | `${XDG_CONFIG_HOME:-~/.config}/vaultkeep/jobs` |
| Password files | `${XDG_CONFIG_HOME:-~/.config}/vaultkeep/secrets` |
| Local state | `${XDG_STATE_HOME:-~/.local/state}/vaultkeep/jobs` |
| Temporary encrypted-archive workspaces | `${XDG_CACHE_HOME:-~/.cache}/vaultkeep/tmp` |
| Locks | `$XDG_RUNTIME_DIR/vaultkeep/locks`, or `${XDG_CACHE_HOME:-~/.cache}/vaultkeep/locks` |
| Systemd user units | `${XDG_CONFIG_HOME:-~/.config}/systemd/user` |
| User timer registry | `${XDG_STATE_HOME:-~/.local/state}/vaultkeep/systemd-instances.json` |

## Create a user-mode job

Create the user job and destination directories:

```bash
mkdir -p ~/.config/vaultkeep/jobs
mkdir -p ~/.local/share/backups/app
```

Create `~/.config/vaultkeep/jobs/app.yaml`:

```yaml
config_version: 1

job:
  id: app

sources:
  - path: /home/alice/Documents
  - path: /home/alice/Projects/app
    exclude:
      - .venv/
      - __pycache__/

exclude:
  - "**/.cache/**"

source_options:
  follow_symlinks: false
  cross_filesystems: false
  ignore_missing: false

destination:
  root: /home/alice/.local/share/backups/app
  name_template: "backup-{job}-{timestamp_utc:%Y%m%dT%H%M%SZ}"
  require_mount: false

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

The filename stem and `job.id` must match. In the example above, both are `app`.

## Validate and run manually

Run schema-only validation:

```bash
vaultkeep --user --config ~/.config/vaultkeep/jobs/app.yaml validate --schema-only
```

Run complete validation and a backup:

```bash
vaultkeep --user --config ~/.config/vaultkeep/jobs/app.yaml validate
vaultkeep --user --config ~/.config/vaultkeep/jobs/app.yaml run
```

Inspect and maintain backups:

```bash
vaultkeep --user --config ~/.config/vaultkeep/jobs/app.yaml list
vaultkeep --user --config ~/.config/vaultkeep/jobs/app.yaml verify
vaultkeep --user --config ~/.config/vaultkeep/jobs/app.yaml prune --dry-run
vaultkeep --user --config ~/.config/vaultkeep/jobs/app.yaml prune
```

## Configure user-mode automatic backups

Enable scheduling in the job:

```yaml
schedule:
  enabled: true
  interval: daily
  window: "01:00-05:00"
  persistent: true
```

Install the systemd user timer:

```bash
vaultkeep --user --config ~/.config/vaultkeep/jobs/app.yaml timer install
```

Manage the user timer:

```bash
vaultkeep --user --config ~/.config/vaultkeep/jobs/app.yaml timer status
vaultkeep --user --config ~/.config/vaultkeep/jobs/app.yaml timer next
vaultkeep --user --config ~/.config/vaultkeep/jobs/app.yaml timer update
vaultkeep --user --config ~/.config/vaultkeep/jobs/app.yaml timer disable
vaultkeep --user --config ~/.config/vaultkeep/jobs/app.yaml timer remove
```

Manage all user-mode jobs:

```bash
vaultkeep --user timers list
vaultkeep --user timers validate
vaultkeep --user timers sync --dry-run
vaultkeep --user timers sync
```

User timers depend on the user's systemd user manager. On many Debian systems, user timers run while the user has an active login session. To run user timers after boot without an active session, an administrator may need to enable lingering:

```bash
sudo loginctl enable-linger alice
```

That is an administrator decision and is outside Vaultkeep's user-mode permissions.

## Password-protected user-mode backups

Create a user-owned password file:

```bash
mkdir -p ~/.config/vaultkeep/secrets
chmod 700 ~/.config/vaultkeep/secrets
nano ~/.config/vaultkeep/secrets/app.passphrase
chmod 600 ~/.config/vaultkeep/secrets/app.passphrase
```

Configure the job:

```yaml
archive:
  format: tar.7z
  compression_level: 6

encryption:
  mode: password
  password_file: /home/alice/.config/vaultkeep/secrets/app.passphrase
```

The password file must be owned by the user running `vaultkeep --user` and must have mode `0600`.

## User-mode hooks

User-mode hooks run as the calling user. Hook paths must be absolute, not writable by group or other users, and owned by either root or the user running Vaultkeep. This allows hooks in secure user-owned paths and also allows root-owned system interpreters such as `/usr/bin/python3`.

Hooks still must not contain passwords or tokens in command arguments because arguments can be visible through the operating-system process list.

## User-mode limitations

User mode cannot:

- install Vaultkeep or Debian package dependencies;
- read files the user cannot read;
- write destinations the user cannot write;
- mount CIFS/NFS shares unless the operating system already permits the user to do so;
- manage root-mode jobs or system timers;
- use root-mode state, secrets, locks, or timer registries.

Use root mode when the backup requires administrator privileges.
