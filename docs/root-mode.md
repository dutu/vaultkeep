# Vaultkeep root mode guide

Root mode is the administrator-managed Vaultkeep workflow. It is the right mode for system backups, protected application data, root-owned secrets, mounted backup shares, and administrator-controlled lifecycle hooks.

Root mode uses the centrally installed `vaultkeep` command and system locations:

| Purpose | Location |
|---|---|
| Job configurations | `/etc/vaultkeep/jobs` |
| Secrets | `/etc/vaultkeep/secrets` |
| Local state | `/var/lib/vaultkeep/jobs` |
| Temporary encrypted-archive workspaces | `/var/lib/vaultkeep/tmp` |
| Locks | `/run/lock/vaultkeep` |
| Systemd units and drop-ins | `/etc/systemd/system` |

Root mode is independent from user mode. It does not read user-mode jobs below `~/.config/vaultkeep`, and user-mode timers do not modify root-mode timer units or registries.

For the shared job schema, destination template rules, command surface, retention behavior, restore notes, and troubleshooting reference, see the [shared user guide](../README.md#usage).

## Install or update Vaultkeep

Root installs Vaultkeep and its Debian package dependencies once for the host:

```bash
git clone https://github.com/dutu/vaultkeep.git /path/to/vaultkeep
cd /path/to/vaultkeep
git checkout <release-tag>

sudo ./install.sh install --dry-run
sudo ./install.sh install
```

For updates:

```bash
cd /path/to/vaultkeep
git fetch --tags --prune
git checkout <new-release-tag>

sudo ./install.sh update --dry-run
sudo ./install.sh update
```

The root installation provides the shared `/usr/local/bin/vaultkeep` command that both root mode and user mode use.

## Create a root-mode job

Copy an installed example to a real job file:

```bash
sudo cp \
  /etc/vaultkeep/jobs/vaultkeep-schedule.yaml.example \
  /etc/vaultkeep/jobs/app.yaml
```

Edit it as root:

```bash
sudo nano /etc/vaultkeep/jobs/app.yaml
```

The filename stem and `job.id` must match:

```yaml
job:
  id: app
```

Use absolute source and destination paths. For mounted destinations, mount the share through the operating system and set `destination.mount_point` to the actual mount path. The mount point can be `destination.root` itself or one of its parents.

For CIFS shares, set `destination.expected_fstype: cifs` to reject a local directory
exposed through an LXC bind mount when the host share is unavailable. See the
[destination mount checks](../README.md#destination-mount-checks) for verification commands
and the optional destination marker.

## Validate and run manually

Schema-only validation can run without root:

```bash
vaultkeep --config /etc/vaultkeep/jobs/app.yaml validate --schema-only
```

Complete validation and real backup operations run with sudo:

```bash
sudo vaultkeep --config /etc/vaultkeep/jobs/app.yaml validate
sudo vaultkeep --config /etc/vaultkeep/jobs/app.yaml run
sudo vaultkeep --config /etc/vaultkeep/jobs/app.yaml list
sudo vaultkeep --config /etc/vaultkeep/jobs/app.yaml verify
sudo vaultkeep --config /etc/vaultkeep/jobs/app.yaml prune --dry-run
sudo vaultkeep --config /etc/vaultkeep/jobs/app.yaml prune
```

## Configure root-mode automatic backups

Root-mode automatic backups use systemd timers. Shutdown Trigger is user-mode only
and rejects root execution. To use it, create a user-mode job and run
`vaultkeep --user trigger` as the application owner, without sudo. See the
[Trigger guide](../README.md#optional-trigger). Root-mode scheduled jobs and
user-mode Trigger jobs can run independently on the same host.

Enable scheduling in the job:

```yaml
schedule:
  enabled: true
  interval: daily
  window: "01:00-05:00"
  persistent: true
```

Validate the enabled schedule, then install its system timer explicitly. Before
installation, validation reports `Timer: not installed` and `setup incomplete`;
fix any prerequisite failures before continuing. Re-run validation after setup
to check system timer health. See [validation results](../README.md#validate-a-job-configuration).

```bash
sudo vaultkeep --config /etc/vaultkeep/jobs/app.yaml validate
sudo vaultkeep --config /etc/vaultkeep/jobs/app.yaml schedule install
sudo vaultkeep --config /etc/vaultkeep/jobs/app.yaml validate
```

Manage the timer:

```bash
sudo vaultkeep --config /etc/vaultkeep/jobs/app.yaml schedule status
sudo vaultkeep --config /etc/vaultkeep/jobs/app.yaml schedule next
sudo vaultkeep --config /etc/vaultkeep/jobs/app.yaml schedule update
sudo vaultkeep --config /etc/vaultkeep/jobs/app.yaml schedule disable
sudo vaultkeep --config /etc/vaultkeep/jobs/app.yaml schedule remove
```

Manage all root-mode jobs:

```bash
sudo vaultkeep schedules list
sudo vaultkeep schedules validate
sudo vaultkeep schedules sync --dry-run
sudo vaultkeep schedules sync
```

## Secrets and hooks

Root-mode encrypted backups use root-owned password files:

```text
/etc/vaultkeep/secrets/app.passphrase
owner: root
group: root
mode: 0600
```

Root-mode hooks are trusted administrator code, configured as shell command strings and executed with `/bin/sh -c`. Vaultkeep checks that the shell and its path are root-owned and not writable by group or other users. Administrators must securely manage the commands and scripts invoked by the shell; those commands are not individually checked by Vaultkeep. See [lifecycle hooks](../README.md#lifecycle-hooks) for examples and execution behavior.
