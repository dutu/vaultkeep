# Vaultkeep Trigger

Optional wallet shutdown monitoring for Vaultkeep 0.2.0, on Debian 13 with
Python 3.13 or newer. Supports Electrum, Bisq 1, and Wasabi after deployment-specific
process and writer checks. All example instances are disabled.

This distribution provides `vaultkeep_trigger`, its `psutil` dependency, and the
`vaultkeep-trigger` command. Vaultkeep is invoked as a separate absolute executable;
it is not a Python dependency. Installing this package does not install systemd
units or activate monitoring.

Use the [Vaultkeep user guide](https://github.com/dutu/vaultkeep#readme) for the
shared installer and the repository's `docs/trigger/` operational references.
