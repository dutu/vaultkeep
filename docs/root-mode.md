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

Copy the disabled example:

```bash
sudo cp \
  /etc/vaultkeep/jobs/example.yaml.disabled \
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

Use absolute source and destination paths. For mounted destinations, mount the share through the operating system and set `destination.require_mount: true`.

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

Enable scheduling in the job:

```yaml
schedule:
  enabled: true
  interval: daily
  window: "01:00-05:00"
  persistent: true
```

Install the system timer:

```bash
sudo vaultkeep --config /etc/vaultkeep/jobs/app.yaml timer install
```

Manage the timer:

```bash
sudo vaultkeep --config /etc/vaultkeep/jobs/app.yaml timer status
sudo vaultkeep --config /etc/vaultkeep/jobs/app.yaml timer next
sudo vaultkeep --config /etc/vaultkeep/jobs/app.yaml timer update
sudo vaultkeep --config /etc/vaultkeep/jobs/app.yaml timer disable
sudo vaultkeep --config /etc/vaultkeep/jobs/app.yaml timer remove
```

Manage all root-mode jobs:

```bash
sudo vaultkeep timers list
sudo vaultkeep timers validate
sudo vaultkeep timers sync --dry-run
sudo vaultkeep timers sync
```

## Secrets and hooks

Root-mode encrypted backups use root-owned password files:

```text
/etc/vaultkeep/secrets/app.passphrase
owner: root
group: root
mode: 0600
```

Root-mode hooks are trusted administrator code. Hook paths, hook executables, and shebang interpreters must be root-owned and not writable by group or other users.
