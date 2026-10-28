"""Application identity, UID-restricted process identification, and offline safety."""

from __future__ import annotations

import os
import pwd
import re
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Any, Protocol

import psutil  # type: ignore[import-untyped]

from vaultkeep.config.models import ApplicationConfig, JobConfig, TriggerMatcherConfig
from vaultkeep.errors import ApplicationIdentityError, SourceDiscoveryError, ValidationIssue


@dataclass(frozen=True, slots=True)
class ResolvedApplication:
    """Runtime ownership and paths, independent of raw YAML configuration."""

    name: str
    user: str
    owner_uid: int
    data_dir: str
    matcher: TriggerMatcherConfig

    def canonical(self) -> dict[str, Any]:
        """Keep the established application snapshot; UID is carried separately."""
        return {
            "name": self.name,
            "user": self.user,
            "data_dir": self.data_dir,
            "matcher": self.matcher.model_dump(),
        }


def runtime_owner(user_mode: bool, *, config_path: Path | None = None) -> tuple[int, int | None]:
    """Validate execution scope before operations using its ownership policy."""
    uid = os.geteuid()
    if not user_mode and uid != 0:
        raise ApplicationIdentityError(
            ValidationIssue(
                (), "System execution requires root; use sudo or select --user.", "execution_scope"
            ),
            config_path=config_path,
        )
    return (uid, None) if user_mode else (0, 0)


def validate_application_directory(
    application: ResolvedApplication, *, config_path: Path | None = None
) -> None:
    """Validate a prepared directory without resolving ownership again."""
    directory = Path(application.data_dir)
    try:
        if not directory.is_dir() or directory.stat().st_uid != application.owner_uid:
            raise ValueError("application directory ownership")
    except (OSError, ValueError) as error:
        raise ApplicationIdentityError(
            ValidationIssue(
                ("application", "data_dir"),
                f"Data directory must exist and be owned by user {application.user!r}.",
                "application_directory",
            ),
            config_path=config_path,
        ) from error


def execution_policy(
    config: JobConfig, application: ResolvedApplication | None
) -> dict[str, object]:
    """Canonical policy binding Trigger dispatch to the child's prepared identity."""
    return {
        "consistency": config.consistency.mode,
        "application": application.canonical() if application else None,
        "owner_uid": application.owner_uid if application else None,
    }


def resolve_application(
    application: ApplicationConfig,
    *,
    user_mode: bool,
    config_path: Path | None = None,
) -> ResolvedApplication:
    """Use the execution account unless root explicitly selects another owner."""
    executor_uid, _gid = runtime_owner(user_mode, config_path=config_path)
    if application.user is not None and user_mode:
        raise ApplicationIdentityError(
            ValidationIssue(
                ("application", "user"),
                "Unsupported property for user execution; remove application.user. "
                "The application owner is the job's execution account.",
                "user_unsupported",
            ),
            config_path=config_path,
        )
    account_label = (
        repr(application.user) if application.user is not None else f"UID {executor_uid}"
    )
    try:
        account = (
            pwd.getpwnam(application.user)
            if application.user is not None
            else pwd.getpwuid(executor_uid)
        )
    except KeyError as error:
        raise ApplicationIdentityError(
            ValidationIssue(
                ("application", "user"),
                f"User {account_label} does not exist.",
                "user_missing",
            ),
            config_path=config_path,
        ) from error
    except ValueError as error:
        raise ApplicationIdentityError(
            ValidationIssue(
                ("application", "user"),
                f"Invalid user {account_label}.",
                "user_invalid",
            ),
            config_path=config_path,
        ) from error
    except OSError as error:
        raise ApplicationIdentityError(
            ValidationIssue(
                ("application", "user"),
                f"Cannot look up user {account_label}; check the system account service.",
                "user_lookup",
            ),
            config_path=config_path,
        ) from error

    if application.user is None and account.pw_uid != executor_uid:
        raise ApplicationIdentityError(
            ValidationIssue(
                ("application", "user"),
                "The resolved application owner differs from the job's execution account.",
                "user_mismatch",
            ),
            config_path=config_path,
        )

    def resolve(value: str, field: tuple[str | int, ...]) -> str:
        expanded = account.pw_dir + value[1:] if value == "~" or value.startswith("~/") else value
        try:
            return str(Path(expanded).resolve(strict=False))
        except (OSError, ValueError) as error:
            raise ApplicationIdentityError(
                ValidationIssue(
                    field,
                    "Cannot resolve path; check permissions and symlinks.",
                    "application_path",
                ),
                config_path=config_path,
            ) from error

    directory = Path(resolve(application.data_dir, ("application", "data_dir")))
    matcher = application.matcher.model_copy(
        update={
            "exe_realpath": resolve(
                application.matcher.exe_realpath, ("application", "matcher", "exe_realpath")
            )
            if application.matcher.exe_realpath
            else None,
            "entrypoints": [
                entry.model_copy(
                    update={
                        "value": resolve(
                            entry.value, ("application", "matcher", "entrypoints", index, "value")
                        )
                    }
                )
                if entry.kind != "java_main"
                else entry
                for index, entry in enumerate(application.matcher.entrypoints)
            ],
        },
    )
    # model_copy does not copy update values; detach the entire finished snapshot.
    matcher = matcher.model_copy(deep=True)
    resolved = ResolvedApplication(
        name=application.name,
        user=account.pw_name,
        owner_uid=account.pw_uid,
        data_dir=str(directory),
        matcher=matcher,
    )
    return resolved


@dataclass(frozen=True, slots=True)
class Entrypoint:
    kind: str
    value: str


@dataclass(frozen=True, slots=True)
class MatcherSpec:
    """Strong process identity plus optional supporting predicates."""

    exe_realpath: str | None = None
    entrypoints: tuple[Entrypoint, ...] = ()
    argv_tokens: tuple[str, ...] = ()
    cmdline_regex: str | None = None
    env_eq: dict[str, str] = field(default_factory=dict)

    def canonical(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "exe_realpath": self.exe_realpath,
            "entrypoints": [
                {"kind": entrypoint.kind, "value": entrypoint.value}
                for entrypoint in self.entrypoints
            ],
            "argv_tokens": list(self.argv_tokens),
            "cmdline_regex": self.cmdline_regex,
            "env_eq": dict(sorted(self.env_eq.items())),
        }
        return payload


def matcher_from_canonical(data: dict[str, Any]) -> MatcherSpec:
    """Validate and rebuild a matcher from a frozen request snapshot."""
    raw = TriggerMatcherConfig.model_validate(data)
    entrypoints = tuple(Entrypoint(kind=item.kind, value=item.value) for item in raw.entrypoints)
    return MatcherSpec(
        exe_realpath=raw.exe_realpath,
        entrypoints=entrypoints,
        argv_tokens=tuple(raw.argv_tokens),
        cmdline_regex=raw.cmdline_regex,
        env_eq=dict(raw.env_eq),
    )


class Observation(str, Enum):  # noqa: UP042 - preserve persisted observation semantics
    """One sanitized poll result."""

    RUNNING = "running"
    STOPPED = "stopped"
    AMBIGUOUS = "ambiguous"
    INSPECTION_UNAVAILABLE = "inspection_unavailable"


@dataclass(frozen=True, slots=True)
class ProcessInfo:
    """A sanitized snapshot of one process."""

    pid: int
    create_time: float
    exe_realpath: str | None
    cmdline: tuple[str, ...]
    env: dict[str, str] | None = None
    cmdline_available: bool = True


@dataclass(frozen=True, slots=True)
class MatchResult:
    matched: bool
    reason: str | None = None
    evidence: dict[str, object] = field(default_factory=dict)
    unavailable: bool = False


def _canonical(value: str) -> str:
    return os.path.realpath(value)


def _match_entrypoint(spec_value: str, kind: str, cmdline: tuple[str, ...]) -> bool:
    if kind == "script":
        wanted = _canonical(spec_value)
        return any(_canonical(token) == wanted for token in cmdline[:2])
    if kind == "jar":
        wanted = _canonical(spec_value)
        for index, token in enumerate(cmdline):
            if token == "-jar" and index + 1 < len(cmdline):
                if _canonical(cmdline[index + 1]) == wanted:
                    return True
            elif _canonical(token) == wanted:
                return True
        return False
    # java_main: exact entry token.
    return spec_value in cmdline


def match_identity(matcher: MatcherSpec, proc: ProcessInfo) -> MatchResult:
    """Evaluate a matcher's strong identity and supporting predicates."""
    unavailable = False
    if matcher.exe_realpath is not None:
        if proc.exe_realpath is None:
            unavailable = True
        elif _canonical(proc.exe_realpath) != _canonical(matcher.exe_realpath):
            return MatchResult(False, "executable path does not match")

    if matcher.entrypoints:
        if not proc.cmdline_available:
            unavailable = True
        elif not any(
            _match_entrypoint(entrypoint.value, entrypoint.kind, proc.cmdline)
            for entrypoint in matcher.entrypoints
        ):
            return MatchResult(False, "no configured entrypoint matches")

    if matcher.exe_realpath is None and not matcher.entrypoints:
        return MatchResult(False, "matcher declares no strong identity")

    if not proc.cmdline_available and (matcher.argv_tokens or matcher.cmdline_regex):
        unavailable = True
    missing = (
        [token for token in matcher.argv_tokens if token not in proc.cmdline]
        if proc.cmdline_available
        else []
    )
    if missing:
        return MatchResult(False, "argv tokens not present")

    if matcher.cmdline_regex is not None and proc.cmdline_available:
        pattern = re.compile(matcher.cmdline_regex)
        if not any(pattern.fullmatch(token) for token in proc.cmdline):
            return MatchResult(False, "cmdline regex did not match any token")

    if unavailable:
        return MatchResult(False, "process identity metadata is not readable", unavailable=True)

    if matcher.env_eq:
        if proc.env is None:
            return MatchResult(False, "environment not read", unavailable=True)
        for key, value in matcher.env_eq.items():
            if proc.env.get(key) != value:
                return MatchResult(False, f"environment key {key} does not match")

    return MatchResult(
        True,
        evidence={"pid": proc.pid, "create_time": proc.create_time},
    )


def required_env_keys(matcher: MatcherSpec) -> tuple[str, ...]:
    """Return environment keys the matcher needs to read for a process."""
    return tuple(sorted(matcher.env_eq))


_BOOT_ID_PATH = Path("/proc/sys/kernel/random/boot_id")


def boot_id() -> str | None:
    """Return the kernel boot ID, or ``None`` when it cannot be read."""
    try:
        return _BOOT_ID_PATH.read_text(encoding="ascii").strip()
    except OSError:
        return None


class ProcessProvider(Protocol):
    """Interface for process inspection (replaceable in tests)."""

    def snapshot(self) -> list[ProcessInfo]:
        """Return a frozen snapshot of the current processes."""

    def read_env(self, pid: int, keys: Iterable[str]) -> dict[str, str] | None:
        """Return the requested environment keys, or ``None`` when unreadable."""

    def resolve_directory(self, pid: int, value: str) -> str | None:
        """Resolve a process-reported directory using verified owner/home/cwd metadata."""

    def alive(self, pid: int, create_time: float) -> bool | None:
        """Return whether ``pid`` is still the same process (pid + create time)."""


class PsutilProcessProvider:
    """Real process provider using psutil."""

    def __init__(self, *, owner_uid: int | None = None) -> None:
        self.owner_uid = os.geteuid() if owner_uid is None else owner_uid
        self._last: dict[int, float] = {}

    def snapshot(self) -> list[ProcessInfo]:
        infos: list[ProcessInfo] = []
        seen: dict[int, float] = {}
        for proc in psutil.process_iter(["pid", "uids"]):
            info = proc.info
            pid = proc.pid
            uids = info.get("uids")
            if uids is None:
                raise psutil.AccessDenied(pid)
            if uids.real != self.owner_uid:
                continue
            try:
                create_time = proc.create_time()
                cmdline = tuple(proc.cmdline())
                cmdline_available = True
            except psutil.NoSuchProcess:
                continue
            except (psutil.AccessDenied, OSError):
                create_time = proc.create_time()
                cmdline = ()
                cmdline_available = False
            try:
                exe = proc.exe()
            except (psutil.AccessDenied, psutil.NoSuchProcess, OSError):
                exe = None
            if not self.alive(pid, float(create_time)):
                raise psutil.AccessDenied(pid)
            infos.append(
                ProcessInfo(
                    pid=pid,
                    create_time=float(create_time),
                    exe_realpath=exe,
                    cmdline=cmdline,
                    cmdline_available=cmdline_available,
                )
            )
            seen[pid] = float(create_time)
        self._last = seen
        return infos

    def read_env(self, pid: int, keys: Iterable[str]) -> dict[str, str] | None:
        wanted = set(keys)
        if not wanted:
            return {}
        try:
            expected = self._last.get(pid)
            if expected is None or not self.alive(pid, expected):
                return None
            environ = psutil.Process(pid).environ()
            if not self.alive(pid, expected):
                return None
        except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess, OSError):
            return None
        return {key: environ[key] for key in wanted if key in environ}

    def resolve_directory(self, pid: int, value: str) -> str | None:
        """Never resolve process paths against the backup executor's home or cwd."""
        expected = self._last.get(pid)
        try:
            if expected is None or not self.alive(pid, expected):
                return None
            if value == "~" or value.startswith("~/"):
                home = pwd.getpwuid(self.owner_uid).pw_dir
                if not os.path.isabs(home):
                    return None
                expanded = home + value[1:]
            elif value.startswith("~"):
                return None
            elif os.path.isabs(value):
                expanded = value
            else:
                cwd = psutil.Process(pid).cwd()
                if not os.path.isabs(cwd):
                    return None
                expanded = os.path.join(cwd, value)
            resolved = os.path.realpath(expanded)
            return resolved if self.alive(pid, expected) else None
        except (KeyError, psutil.Error, OSError):
            return None

    def alive(self, pid: int, create_time: float) -> bool | None:
        try:
            proc = psutil.Process(pid)
            if not proc.is_running():
                return False
            return bool(
                abs(proc.create_time() - create_time) < 0.01 and proc.uids().real == self.owner_uid
            )
        except (psutil.NoSuchProcess, psutil.ZombieProcess, OSError):
            return False
        except psutil.AccessDenied:
            return None


_DIRECTORY_ENV_KEY: dict[str, str] = {
    "electrum": "ELECTRUMDIR",
    "wasabi": "WASABI_DATADIR",
}
_DIRECTORY_OPTIONS: dict[str, tuple[str, ...]] = {
    "electrum": ("-D", "--dir"),
    "wasabi": ("--datadir",),
    "bisq": ("--appDataDir",),
}


@dataclass(frozen=True, slots=True)
class DetectionResult:
    observation: Observation
    boot_id: str | None = None
    pid: int | None = None
    create_time: float | None = None
    evidence: dict[str, object] = field(default_factory=dict)
    reason: str | None = None


def _option_value(cmdline: tuple[str, ...], options: tuple[str, ...]) -> str | None:
    for index, token in enumerate(cmdline):
        for option in options:
            if token == option and index + 1 < len(cmdline):
                return cmdline[index + 1]
            if token.startswith(f"{option}="):
                return token[len(option) + 1 :]
    return None


def _resolve_directory(
    app: str, proc: ProcessInfo, provider: ProcessProvider
) -> tuple[str | None, str]:
    """Return (canonical directory, status) where status is resolved/unresolved/inaccessible."""

    def resolve(value: str) -> tuple[str | None, str]:
        if os.path.isabs(value):
            return _canonical(value), "resolved"
        # Unsupported/unavailable process context is uncertainty, never evidence
        # that this process belongs to another application instance.
        directory = provider.resolve_directory(proc.pid, value)
        return (directory, "resolved") if directory is not None else (None, "inaccessible")

    env_key = _DIRECTORY_ENV_KEY.get(app)
    if env_key is not None:
        env = provider.read_env(proc.pid, [env_key])
        if env is None:
            return None, "inaccessible"
        if env.get(env_key):
            return resolve(env[env_key])

    value = _option_value(proc.cmdline, _DIRECTORY_OPTIONS.get(app, ()))
    if value:
        return resolve(value)
    return None, "unresolved"


def detect_spec(
    *,
    app: str,
    data_dir: str,
    matcher: MatcherSpec,
    provider: ProcessProvider,
) -> DetectionResult:
    """Return an observation from a frozen app/data-dir/matcher snapshot."""
    current_boot = boot_id()
    if not os.path.isabs(data_dir):
        return DetectionResult(
            Observation.AMBIGUOUS,
            boot_id=current_boot,
            reason="configured data directory is not resolved",
        )
    wanted_dir = _canonical(data_dir)
    try:
        snapshot = provider.snapshot()
    except (psutil.Error, OSError):
        return DetectionResult(
            Observation.INSPECTION_UNAVAILABLE,
            boot_id=current_boot,
            reason="process snapshot is not readable",
        )

    identity_matches: list[ProcessInfo] = []
    ambiguous_reason: str | None = None
    unavailable_reason: str | None = None

    env_keys = required_env_keys(matcher)
    for proc in snapshot:
        # Reject known unrelated identities before reading any environment.
        identity = match_identity(replace(matcher, env_eq={}), proc)
        if identity.unavailable:
            unavailable_reason = identity.reason
            continue
        if not identity.matched:
            continue
        env: dict[str, str] | None = None
        if env_keys:
            env = provider.read_env(proc.pid, env_keys)
            if env is None:
                unavailable_reason = "process environment is not readable"
                continue
        candidate = replace(proc, env=env)
        result: MatchResult = match_identity(matcher, candidate)
        if result.matched:
            identity_matches.append(candidate)

    resolved_matches: list[ProcessInfo] = []
    for candidate in identity_matches:
        if not provider.alive(candidate.pid, candidate.create_time):
            unavailable_reason = "process identity changed before metadata inspection"
            continue
        directory, status = _resolve_directory(app, candidate, provider)
        if not provider.alive(candidate.pid, candidate.create_time):
            unavailable_reason = "process identity changed during metadata inspection"
            continue
        if status == "inaccessible":
            unavailable_reason = "data directory metadata is not readable"
            continue
        if status == "unresolved":
            ambiguous_reason = "could not resolve the instance data directory"
            continue
        if directory == wanted_dir:
            resolved_matches.append(candidate)

    # Re-validate identity immediately before reporting running.
    for candidate in resolved_matches:
        alive = provider.alive(candidate.pid, candidate.create_time)
        if not alive:
            unavailable_reason = "matching process disappeared, changed, or cannot be revalidated"
        if alive:
            return DetectionResult(
                observation=Observation.RUNNING,
                boot_id=current_boot,
                pid=candidate.pid,
                create_time=candidate.create_time,
                evidence={
                    "matched_pid": candidate.pid,
                    "create_time": candidate.create_time,
                    "data_dir": wanted_dir,
                },
            )

    if ambiguous_reason is not None:
        return DetectionResult(
            observation=Observation.AMBIGUOUS,
            boot_id=current_boot,
            reason=ambiguous_reason,
        )
    if unavailable_reason is not None:
        return DetectionResult(
            observation=Observation.INSPECTION_UNAVAILABLE,
            boot_id=current_boot,
            reason=unavailable_reason,
        )
    return DetectionResult(
        observation=Observation.STOPPED,
        boot_id=current_boot,
        evidence={"data_dir": wanted_dir},
    )


def require_application_offline(
    config: JobConfig, *, application: ResolvedApplication | None
) -> None:
    """Require a reliable stopped observation; files mode does no process inspection."""
    if config.consistency.mode == "files":
        return
    app = application
    if app is None:
        raise SourceDiscoveryError("Offline validation requires a resolved application identity")
    owner_uid = app.owner_uid
    if os.geteuid() not in (0, owner_uid):
        raise SourceDiscoveryError("Cannot inspect the configured application's process owner")
    try:
        directory = Path(app.data_dir)
        if (
            pwd.getpwnam(app.user).pw_uid != owner_uid
            or str(directory.resolve(strict=True)) != app.data_dir
            or not directory.is_dir()
            or directory.stat().st_uid != owner_uid
        ):
            raise ValueError("resolved application owner or data directory changed")
    except (KeyError, OSError, ValueError) as error:
        raise SourceDiscoveryError(
            f"Application identity is unavailable or changed: {error}"
        ) from error
    result = detect_spec(
        app=app.name,
        data_dir=app.data_dir,
        matcher=matcher_from_canonical(app.matcher.model_dump()),
        provider=PsutilProcessProvider(owner_uid=owner_uid),
    )
    if result.observation is not Observation.STOPPED:
        raise SourceDiscoveryError(
            f"Application must be offline: {result.observation.value}: {result.reason or app.name}"
        )
