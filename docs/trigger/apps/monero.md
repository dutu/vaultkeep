# Monero Wallet GUI and RPC Trigger setup

Use one Vaultkeep job for the directory containing a Monero wallet. Either
frontend can arm the job by selecting a wallet in that directory. After that,
close every configured GUI and wallet RPC process owned by the account.
Switching wallets, editing settings, or closing just one frontend leaves the
shutdown guard active.

## Prepare the job

1. [Install Vaultkeep](../../../README.md#installation). Switch to the non-root
   account that owns the wallet and runs both Monero frontends.
2. Copy the [disabled Monero example](../../../examples/monero-trigger.yaml) and
   open it for editing.

   ```bash
   jobs_dir="${XDG_CONFIG_HOME:-$HOME/.config}/vaultkeep/jobs"
   install -d -m 700 "$jobs_dir"
   job_config="$jobs_dir/monero-main.yaml"
   if [ ! -e "$job_config" ]; then
       install -m 600 /opt/vaultkeep/current/src/examples/monero-trigger.yaml "$job_config"
   fi
   nano "$job_config"
   ```
3. Set `application.data_dir` to the wallet file's containing directory. Set
   `sources[].path` to the files to archive, normally that same directory.
   The example assumes the selected wallet file is
   `/home/user/Monero/wallets/my-wallet/m-wallet`. Both settings point to
   `/home/user/Monero/wallets/my-wallet` so the backup includes the entire directory.
   Set `destination.root` to an existing, writable backup directory. Review
   retention and [password protection](../../configuration.md#password-protected-backups).
   The example creates unencrypted archives.
4. Set the two `script` entrypoints to the actual absolute launch paths ending in
   `monero-wallet-gui` and `monero-wallet-rpc`. Add entries for any other paths or
   versions in use, and update the job when installation paths change. Launch each
   frontend with its absolute path so the same-user monitor can identify it if
   the kernel hides its executable path. Both roles are required. The matcher
   accepts only the entrypoints for Monero.

## Wallet selection

- **GUI** reads `wallet_path` from the Qt settings. A readable
  `monero-storage/.portable` marker under the GUI working directory selects
  `monero-storage/settings.ini` unless it contains `disabled`. With no marker,
  an existing `settings.ini` still selects legacy portable mode. For normal mode,
  Vaultkeep reads
  `$XDG_CONFIG_HOME/monero-project/monero-core.conf` from the GUI's environment,
  using `$HOME/.config` when the XDG variable is unset or empty. The saved path
  can refer to a wallet at the startup or password screen.
  If the marker cannot be read or an active marker has no settings file,
  wallet selection remains uncertain.
- **Wallet RPC** uses an explicit `--wallet-file PATH` or `--config-file PATH`
  with a `wallet-file=PATH` setting. The `--option=PATH` form also works. An
  explicit `--wallet-file` takes precedence over the config file's `wallet-file`.
  Vaultkeep still checks the specified config file for unsupported selection modes.

Relative paths use the frontend's working directory. The selected wallet's
canonical parent must equal `application.data_dir` exactly. Vaultkeep rereads
settings each poll. Settings are limited to regular UTF-8 files up to 1 MiB.
Missing, ambiguous, or unreadable selection evidence keeps a recognized frontend
guarding shutdown. Diagnostics omit settings contents and process arguments.

Use native Linux executables for the GUI and wallet RPC. Local `monerod`,
`monero-wallet-cli`, renamed binaries, confined distributions, and RPC
`wallet-dir` or `generate-from-json` selection are outside this detection model.
Check all other writers of the selected files before enabling automation.

## Root-mode offline backups

For a manual or scheduled backup in root mode, set `application.user` to the
wallet owner's account and use `consistency.mode: application_offline`. The backup
can proceed once all configured Wallet GUI and wallet RPC processes owned by
that user have exited. Vaultkeep reads Monero settings files only as the process
owner, so a running frontend that requires a settings read may report
`inspection_unavailable` and block the backup. Trigger runs in user mode.

## Validate and enable

1. Stop both frontends and any other writers. Keep the schedule and trigger
   disabled while you validate and test a manual backup.

   ```bash
   vaultkeep --user --config "$job_config" validate
   vaultkeep --user --config "$job_config" run
   vaultkeep --user --config "$job_config" verify
   ```

   Confirm that the expected archive exists and verification reports
   `Result: VERIFIED`.
2. Set `trigger.enabled: true` and keep `schedule.enabled: false`. With an active
   systemd user manager, activate the monitor.

   ```bash
   vaultkeep --user --config "$job_config" trigger install
   vaultkeep --user --config "$job_config" status
   ```
3. Open the watched wallet in either frontend and use `status` to confirm fresh
   `Application: RUNNING` evidence. Close both frontends normally and expect
   shutdown confirmation followed by a backup result. Run `verify` again.

See [process detection](../process-detection.md) and
[consistency and safety boundaries](../consistency.md).
