# Implementation and deployment validation plan

The core implementation is present. This plan records acceptance requirements and
remaining validation; it is not an installation guide or a claim that every test-matrix
scenario has been verified. The settled technical contract is in
[architecture](../design/trigger.md), and operator procedures are in the
[user guide](../../README.md).

## Current implementation

The repository contains configuration/path/environment handling, psutil detection,
shutdown confirmation, durable instance/request/launch/receipt state, monitor recovery,
static systemd worker dispatch, safety annotations, contention retries, bounded child
output, history cleanup, optional notifications, public CLI commands, and disabled
examples. The test suite uses fake jobs, processes, clocks, and systemd controllers.

## Validation status tracker

| Gate | Status | Evidence needed |
| --- | --- | --- |
| Electrum daemon vs CLI vs GUI-forwarding | Open | Verified process model on installed version |
| Electrum memory-hardening metadata access | Open | Same-user metadata sufficient for safe identification |
| Bisq desktop/daemon identity and directories | Open | Verified launchers, JVM identity, config/directory resolution |
| Wasabi GUI/daemon identity and directories | Open | Verified executable identity and directory resolution |
| Related writer processes | Open | Establish all processes that can keep writing each instance's job sources |
| Job scope and destination ownership | Open | One reviewed instance scope per job; distinct effective destinations and external-run review |
| Installed VaultKeep contract/version | Open | Recheck installed version, exact CLI behavior, environment, and preflight limits |
| Static worker template semantics | Open | Queued starts, oneshot activating state, receipts, cleanup, proven-rejection exit-code classes, and independent lifetime |
| Trigger Python environment | Verified | `.venv/bin/python` is Python 3.13.5 and runs the test suite; the historical IDE SDK selection is recorded in development/testing |

## Remaining implementation and deployment checks

1. Revalidate the deployed VaultKeep executable, version, public CLI, selected
   environment, job source scope, destination ownership, and external scheduling.
2. Verify each installed wallet's identities, data-directory resolution, metadata
   accessibility, and every related writer before enabling that instance.
3. Exercise the static template with an explicitly authorized disposable fake job:
   queued starts, oneshot activating state, receipts, absence probes, rejection
   diagnostics/codes, failed-unit cleanup, monitor-independent lifetime, logout, and
   shutdown limits. Do not use production backup jobs for this exercise.
4. Validate and address the known implementation gaps: cross-instance matcher overlap
   has no check; interpreter-only entrypoint values are accepted; Python regex matching
   is not bounded; systemctl rejection codes are classified without diagnostic proof;
   synchronous systemctl probes delay the monitor loop; request-file size caps and a
   dedicated supersession desktop alert are unimplemented. Keep these existing design
   requirements distinct from implemented guarantees.
5. Complete current IDE inspections. Earlier clean inspections cover an older revision
   only; the latest inspection result remains unverified.

## Implementation milestones and acceptance gates

| # | Milestone | Exit criteria | Artifacts |
| ---: | --- | --- | --- |
| 1 | Integration preflight | Installed VaultKeep version/contract, public validation behavior, selected environment, job scope, and destination ownership reviewed without running production jobs | Recorded contract and deployment assumptions |
| 2 | Process validation | Safe read-only identity inspection and version-specific matcher/writer evidence established; unverified instances stay disabled | Disabled example configurations and validation notes |
| 3 | Core monitoring | Configuration, observations, confirmation, pending-request IDs, persistence, and restart/config/boot recovery implemented with mocked processes | Monitoring/state modules and focused tests |
| 4 | Dispatch and worker | Durable request/attempt transitions, parallel instance workers, static-unit reconciliation, safety annotation, contention retry, and bounded receipts validated using fake commands/mocked systemd | Monitor/worker modules, recovery matrix tests |
| 5 | CLI and packaging | check-config, read-only status, monitor/template units, bounded optional notifications, and installation guidance complete; automated tests install/enable nothing | CLI, units, and app documentation |
| 6 | Automated validation | Focused tests pass in the configured Python 3.13 environment; required PyCharm inspections pass after source edits | Interpreter/check results and documentation-link validation |
| 7 | Deployment validation | Explicitly authorized template exercise uses a fake job; actual process matchers, session notifications, and lifetime limits verified read-only where possible | Deployment record and remaining limitations; production enablement separately authorized |

## Test plan

Use temporary state/runtime directories through test-only injection, fake clocks/process snapshots, fake VaultKeep executables, and mocked systemd/notification interfaces. Never terminate wallets, inspect real wallet contents, execute production jobs, touch production destinations, or alter user services during automated tests.

| Area | Required scenarios |
| --- | --- |
| Identity | Strong vs interpreter-only identity, exact argv/entrypoint forms, allowed/disallowed environment keys, directory/network variants, related writers, cross-instance overlap, Electrum hardening and GUI forwarding, multiple Bisq directories, symlink changes, pathological regex limits |
| Detection/lifecycle | Start/stop/crash, initially stopped, restart during confirmation, ambiguous/unavailable evidence resets full delay, PID reuse/races, boot change, missed shutdown across monitor restart, whole lifecycle between polls |
| Requests/concurrency | Stable pending ID, duplicate observations, one active plus one pending per instance, latest pending replacement/drop count, restart invalidation, parallel independent destinations, new pending request preserved across an older blocked/deferred receipt |
| Persistence/recovery | Crash between every adjacent request/association/launch/start/receipt/reconciliation step, unassociated request adoption, corrupt/incompatible/contradictory state, failed durable write, verified rejection persisted before requeue, crash before rejection persistence remains uncertain, per-request rejection count/processed-attempt evidence and alert decisions survive restart without recounting, compatible pending restart, matcher/job/parameter/enablement changes, removed instance with active worker |
| Systemd recovery | Queued start, oneshot activating worker, loaded-but-inactive template instance, receipt with unloaded/completed unit, failed unit without receipt, proven rejection before queueing retries through the delayed pending path, deployment-verified start-command code/diagnostic mapping distinct from status codes, pre-queue refusal classes 4/5/6 qualify while generic failure/timeout/lost bus response do not, invalid argument 2 is an integration error, numeric code alone never proves rejection, nonzero submission plus absent unit/jobs without rejection proof yields outcome_unknown only once worker absence is established, inconclusive probe retains active association then resolves to receipt or confirmed absence, unavailable manager retains association, no replay after outcome_unknown, later new lifecycle allowed, second-monitor rejection, lock descriptor not inherited |
| Worker/results | Full exit mapping, exit 10 delayed fresh-attempt retry, exit 2/missing/exec-failing binary and malformed request integration errors, signal failure, failed final recheck, actual version mismatch, absolute executable/request environment, child XDG_RUNTIME_DIR equals the user runtime directory, same-namespace external run contends on the same lock file, created/unchanged/result_unknown |
| Safety/deadlines | App restarts during backup, ambiguous metadata and monitor gap while worker runs/completes, immutable receipt plus review_required, warning retained on later unchanged/result_unknown, overdue worker continues with active slot held, no automatic kill/delete |
| Diagnostics/retention | Output exceeds cap but child drains and final marker parses, misleading earlier/stderr markers, bounded/absent final marker, private permissions, cleanup excludes active/pending/unresolved/recovery-referenced attempts, completed reconciled summarized outcome_unknown/review_required becomes cleanup-eligible, per-instance 20-record summarized-history cap removes oldest eligible records, protected records are retained and affected-instance dispatch blocked when a bound cannot be enforced, safety summaries survive log expiry |
| Config/preflight | Unknown keys/types, unsafe job IDs, invalid app/matcher/path/timing, schedule/trigger exclusion, per-job lock/state/worker isolation, optional deadlines, disabled structural validation without runtime job probing, static path without manager/runtime, configured params passed, schema-only limits, full validate failure mapping, missing/masked unit, unwritable/absent state parent, unreachable bus, version warning, no validation writes/probe units |
| CLI/notifications | Status up/down/without runtime/without state, held-but-stalled monitor age, stale/corrupt state without mutation, blocked status with blocking attempt ID/wait age, one wait notification using the blocking attempt's frozen deadline despite config changes, wait timer and alert decision survive restart/pending replacement, later waits behind an already-notified attempt do not repeat alert, sanitized evidence and removed-instance records, exit codes 0–6, second monitor exits 3, corrupt state exits 2, valid state with failures exits 0, globally disabled notifications, missing notify-send/session bus, timeout, recovery deduplication |
| Rejection notifications | One start alert across contention/proven-rejection attempts, escalation only at the fifth consecutive proven rejection, no repeat on sixth/later rejection or restart, disabled/failed escalation delivery remains best effort, repeated attempt reconciliation does not increment count, count visible in status, proven acceptance resets count while preserving alert/suppression decisions, inconclusive submission leaves count unchanged, new request resets count and decisions, later terminal/overdue/compromised-safety alerts remain deliverable after escalation |
| Documentation/deployment | Fixed account-home state despite HOME/XDG overrides, reviewed destination requirement, relative links and anchors, no deferred public commands/settings presented as v1, internal blocked-wait, summarized uncertain-history, and consecutive-rejection alert constants documented in the service operation topic, authorized fake-job template exercise and monitor/logout/shutdown limits |

Record exact validation commands, interpreter version, results, and remaining deployment limitations during implementation.

Automated results and the retained historical validation record are maintained in
[development/testing](../development/testing.md#validation-limits). A passing mocked
suite does not close installed-process or live-service gates.
