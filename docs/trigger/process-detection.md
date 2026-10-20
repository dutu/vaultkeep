# Process detection and matchers

Trigger identifies each job's running application through a **strong identity**
plus a required data-directory predicate. Detection fails closed: if identity matches but the data
directory cannot be resolved, the observation is `ambiguous`, never `stopped`, so it
cannot arm a backup.

## Matcher form

```yaml
trigger:
  app: electrum
  data_dir: /home/user/.electrum
  matcher:
    exe_realpath: /usr/bin/python3                # optional additional check
    entrypoint:
      kind: script
      value: /opt/electrum/run_electrum
    argv_tokens: ["--dir", "/home/user/.electrum"] # exact, order-insensitive
    cmdline_regex: "--dir|/opt/.*"                # full-anchored per argv token
    env_eq:
      ELECTRUMDIR: /home/user/.electrum           # allow-listed keys only
```

- An application `entrypoint` is required. `exe_realpath` is an optional additional
  predicate; executable-only matchers are rejected even for native application binaries.
  This allows an unrelated hardened process to be excluded through its command line
  when its executable metadata is unreadable. A plausible matching process with unreadable
  metadata still blocks detection.
- `script`/`jar` entrypoints use canonical absolute paths; `java_main` uses the exact
  main-class token. A Python or Java interpreter path alone is not a safe application
  identity; the parser does not currently reject such entrypoint values, so this is an
  operator validation requirement.
  The `script` form also checks argv[0] and can identify a verified native binary whose
  absolute launch path appears there.
- `cmdline_regex` is capped at 200 characters and must accompany strong identity.
  It uses Python `re.fullmatch` against each token. Configured `argv_tokens` have a
  4096-character cap, but actual process tokens and regex execution time are not
  bounded. Prefer exact predicates; use only reviewed, simple patterns. A short
  pattern alone does not prevent excessive backtracking or a stalled monitor.
- `env_eq` allows only `ELECTRUMDIR` (Electrum) and `WASABI_DATADIR` (Wasabi). No Bisq
  environment keys are allowed in v1.
- Trigger inspects same-user processes only and never logs full environments or
  arguments.
- Known unrelated identities are excluded before reading environment predicates.
  Unreadable identity or command-line metadata is reported as `inspection_unavailable`,
  rather than treated as evidence that the app stopped. Snapshot failures also block
  detection. PID/create-time checks protect environment reads against process replacement.
  A matching process that disappears or changes during revalidation also requires a
  fresh snapshot; that race alone is never reported as a confirmed stop.

## Data-directory predicate

The effective directory is resolved and compared with `trigger.data_dir` (both canonicalized):

| App | Sources consulted |
| --- | --- |
| Electrum | `ELECTRUMDIR`, then `-D`/`--dir` |
| Wasabi | `WASABI_DATADIR`, then `--datadir` |
| Bisq | `--app-data-dir`/`--data-dir`/`--user-data-dir` |

A directory token alone is insufficient without application identity. Directory values
can be separate option/value tokens or `--option=value`; the first recognized option
wins. Trigger does not infer a wallet's default directory or read its application
configuration files. For Electrum/Wasabi, the allowed directory environment key is
read before argv fallback; unreadable environment evidence blocks detection even when
a directory option is present.

Unresolved directory evidence or inaccessible relevant metadata blocks dispatch.
Cross-job matcher overlap is **not** detected automatically: check that each
trigger-enabled job identifies a distinct application and data directory. Multiple
matching processes for one job are treated as running if at least one resolves to
its data directory. This does not discover additional writers with different identities.

## Observation vocabulary

`running`, `stopped`, `ambiguous`, or `inspection_unavailable`. Only verified `running`
evidence arms automatic detection; starting the monitor while an app is already stopped
creates no request.

## Validation before enabling

Examples must be disabled until locally verified. Do not present upstream arguments as observed production command lines.

- **Electrum:** distinguish long-lived daemon/GUI ownership from CLI commands and GUI requests forwarded to an existing daemon. Resolve `-D/--dir`, `ELECTRUMDIR`, portable mode, and network-specific data-directory behavior. Multiple loaded wallets do not necessarily represent separate processes or independently stoppable instances.
- **Electrum hardening:** the reviewed `run_electrum` enables Linux memory hardening using `PR_SET_DUMPABLE=0`. Verify which metadata remains readable by the normal user; executable-path access may fail. Do not disable hardening, modify startup, or add elevated privileges to satisfy the detector. Use only verified accessible identity evidence and report unsupported identification when it is insufficient.
- **Bisq:** validate desktop and daemon entry points, packaged launchers, JVM identity, and effective directories. Upstream Bisq 1 configuration supports `appDataDir`, `userDataDir`, `appName`, and `configFile`; command-line arguments are not the only possible source of directory identity. Bisq 2 has a different process/configuration model and is outside the initial scope.
- **Wasabi:** validate GUI and daemon executable identities and auxiliary processes. Upstream documents `--datadir` and `WASABI_DATADIR`; only read explicitly allowed nonsecret environment keys if this is necessary and permitted.
- **Related processes:** establish which processes can continue writing the selected sources. Parentage alone is insufficient once a child is reparented. Ignore unrelated helpers only when their role is verified.

Source references: local Electrum `run_electrum`, `electrum/commands.py`, `electrum/daemon.py`, `electrum/simple_config.py`, `electrum/util.py`, and `electrum/harden_memory_linux.py`; upstream [Bisq configuration source](https://raw.githubusercontent.com/bisq-network/bisq/master/common/src/main/java/bisq/common/config/Config.java), [Wasabi startup parameters](https://docs.wasabiwallet.io/using-wasabi/StartupParameters.html), and [Linux executable-path access rules](https://man7.org/linux/man-pages/man5/proc_pid_exe.5.html).

The expected Electrum installation at `~/.local/opt/electrum/current` was not available
at the source-review baseline. Installed Bisq and Wasabi behavior was not verified.
These are deployment gates, not validated presets. In particular, the disabled Bisq
and Wasabi examples contain no explicit directory evidence; an identity match alone
will be ambiguous. Revalidate against the intended installed versions before enabling
automatic execution.

No automatic process-model validation command is provided. Keep the shipped
[application job examples](../../README.md#configure-and-validate-trigger) disabled until these checks pass.

[User guide](../../README.md)
