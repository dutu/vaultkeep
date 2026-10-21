#!/usr/bin/env bash
set -Eeuo pipefail

readonly SCRIPT_NAME=${0##*/}
readonly MODE=${1:-}

DRY_RUN=0
PURGE=0
COMPONENTS=core,trigger
PREVIOUS_COMPONENTS=none
PREVIOUS_SCHEMA=0
CHECK_USERS=1
RELEASE_ID=""
STAGED_RELEASE=""
RESTORE_FAILED=0
UNIT_VALIDATION_DIR=""

VK_PREFIX=${VAULTKEEP_INSTALL_PREFIX:-/opt/vaultkeep}
VK_CONFIG_ROOT=${VAULTKEEP_CONFIG_ROOT:-/etc/vaultkeep}
VK_VAR_ROOT=${VAULTKEEP_VAR_ROOT:-/var/lib/vaultkeep}
VK_BIN_LINK=${VAULTKEEP_BIN_LINK:-/usr/local/bin/vaultkeep}
VK_SYSTEMD_ROOT=${VAULTKEEP_SYSTEMD_ROOT:-/etc/systemd/system}
VK_TRIGGER_BIN_LINK=${VAULTKEEP_TRIGGER_BIN_LINK:-/usr/local/bin/vaultkeep-trigger}
VK_USER_SYSTEMD_ROOT=${VAULTKEEP_USER_SYSTEMD_ROOT:-/etc/systemd/user}
VK_MONITOR_UNIT="$VK_USER_SYSTEMD_ROOT/vaultkeep-trigger.service"
VK_WORKER_UNIT="$VK_USER_SYSTEMD_ROOT/vaultkeep-trigger-exec@.service"
VK_TESTING=${VAULTKEEP_INSTALL_TESTING:-0}

VK_RELEASES="$VK_PREFIX/releases"
VK_STAGING="$VK_PREFIX/.staging"
VK_CURRENT="$VK_PREFIX/current"
VK_MANIFEST="$VK_PREFIX/install-manifest.json"
VK_LOCK="$VK_PREFIX/install.lock"
VK_JOBS="$VK_CONFIG_ROOT/jobs"
VK_SECRETS="$VK_CONFIG_ROOT/secrets"
VK_STATE_JOBS="$VK_VAR_ROOT/jobs"
VK_TMP="$VK_VAR_ROOT/tmp"
VK_TIMER_REGISTRY="$VK_VAR_ROOT/systemd-instances.json"
VK_SERVICE_UNIT="$VK_SYSTEMD_ROOT/vaultkeep@.service"
VK_TIMER_UNIT="$VK_SYSTEMD_ROOT/vaultkeep@.timer"

ROLLBACK_ACTIVE=0
ROLLBACK_TMP=""
CANDIDATE_RELEASE_PATH=""
CANDIDATE_RELEASE_CREATED=0
REPLACED_RELEASE_PATH=""
REPLACED_RELEASE_BACKUP=""

usage() {
    cat <<'EOF'
Usage:
  sudo ./install.sh install [--dry-run]
  sudo ./install.sh update [--dry-run]
  sudo /opt/vaultkeep/current/src/install.sh uninstall [--dry-run] [--purge]
EOF
}

log() {
    printf '%s\n' "$*"
}

error() {
    local prefix='ERROR:'
    if [[ -t 2 && "${TERM:-dumb}" != dumb && -z "${NO_COLOR:-}" ]]; then
        prefix=$'\033[1;31mERROR:\033[0m'
    fi
    printf '%s %s: %s\n' "$prefix" "$SCRIPT_NAME" "$*" >&2
}

die() {
    error "$*"
    exit 1
}

parse_args() {
    case "$MODE" in
        install|update|uninstall) ;;
        ""|-h|--help)
            usage
            exit 0
            ;;
        *)
            error "unknown mode: $MODE"
            usage >&2
            exit 2
            ;;
    esac
    shift || true
    while (($#)); do
        case "$1" in
            --dry-run) DRY_RUN=1 ;;
            --purge) PURGE=1 ;;
            *)
                error "unknown option: $1"
                usage >&2
                exit 2
                ;;
        esac
        shift
    done
    if [[ "$MODE" != "uninstall" && "$PURGE" == 1 ]]; then
        die "--purge is valid only with uninstall"
    fi
}

require_command() {
    command -v "$1" >/dev/null 2>&1 || die "required command not found: $1"
}

run() {
    if [[ "$DRY_RUN" == 1 ]]; then
        printf 'PLAN run:'
        printf ' %q' "$@"
        printf '\n'
    else
        "$@"
    fi
}

script_dir() {
    local source=${BASH_SOURCE[0]}
    while [[ -L "$source" ]]; do
        local dir
        dir=$(cd -P "$(dirname "$source")" && pwd)
        source=$(readlink "$source")
        [[ "$source" != /* ]] && source="$dir/$source"
    done
    cd -P "$(dirname "$source")" && pwd
}

SOURCE_DIR=$(script_dir)
SNAPSHOT_PATHS=("$VK_CURRENT" "$VK_BIN_LINK" "$VK_SERVICE_UNIT" "$VK_TIMER_UNIT" "$VK_MANIFEST" "$VK_TIMER_REGISTRY")

preflight_host() {
    if [[ "$VK_TESTING" != 1 ]]; then
        [[ "$(id -u)" == 0 ]] || die "installer requires root"
    fi
    local os_release=/etc/os-release
    if [[ "$VK_TESTING" == 1 ]]; then
        os_release=${VAULTKEEP_TEST_OS_RELEASE:-/etc/os-release}
    fi
    [[ -r "$os_release" ]] || die "/etc/os-release is required"
    # shellcheck disable=SC1090
    . "$os_release"
    [[ "${ID:-}" == debian && "${VERSION_ID:-}" == 13 && "${VERSION_CODENAME:-trixie}" == trixie ]] ||
        die "Vaultkeep requires Debian 13 trixie; upgrade Debian 12 before updating Vaultkeep"
    if command -v python3 >/dev/null 2>&1; then
        verify_python
    elif [[ -e "$VK_MANIFEST" || "$MODE" == uninstall ]]; then
        die "python3 >=3.13 is required to inspect an existing deployment"
    fi
    if [[ "$VK_TESTING" == 1 ]]; then
        return
    fi
    [[ -d /run/systemd/system ]] || die "systemd must be the active system manager"
    require_command systemctl
    local version
    version=$(systemctl --version | awk '/^systemd / {print $2; exit}')
    [[ -n "$version" && "$version" -ge 247 ]] || die "systemd 247 or newer is required"
}

install_dependencies() {
    if [[ "$VK_TESTING" == 1 ]]; then
        log "PLAN testing: skip apt dependency installation"
        return
    fi
    run apt-get update
    run apt-get install -y python3 python3-venv tar zstd rsync util-linux mount 7zip
}

verify_executables() {
    local commands=(python3 tar zstd rsync findmnt systemctl systemd-analyze)
    if [[ "$VK_TESTING" != 1 ]]; then
        commands+=(7z)
    fi
    for item in "${commands[@]}"; do
        if command -v "$item" >/dev/null 2>&1; then
            continue
        fi
        if [[ "$DRY_RUN" == 1 ]]; then
            log "PLAN dependency will provide missing command: $item"
            continue
        fi
        die "required command not found: $item"
    done
}

acquire_lock() {
    if [[ "$DRY_RUN" == 1 ]]; then
        log "PLAN acquire installer lock: $VK_LOCK"
        return
    fi
    require_command flock
    install -d -o root -g root -m 0755 "$VK_PREFIX"
    exec 9>"$VK_LOCK"
    flock -n 9 || die "another Vaultkeep installer transaction is active"
}

check_7z_compatibility() {
    if [[ "$DRY_RUN" == 1 || "$VK_TESTING" == 1 ]]; then
        log "PLAN validate Debian 7zip encrypted-header behavior"
        return
    fi
    local tmp
    tmp=$(mktemp -d)
    trap 'rm -rf "$tmp"; trap - RETURN' RETURN
    printf 'vaultkeep installer check\n' >"$tmp/payload.txt"
    tar -C "$tmp" -cf "$tmp/payload.tar" payload.txt
    7z a -t7z -mhe=on -sccUTF-8 -bd -pvaultkeep-installer-test \
        -- "$tmp/payload.tar.7z" "$tmp/payload.tar" >/dev/null
    7z t -bd -sccUTF-8 -pvaultkeep-installer-test \
        -- "$tmp/payload.tar.7z" >/dev/null
}

candidate_version() {
    sed -nE 's/^version = "([^"]+)"/\1/p' "$SOURCE_DIR/pyproject.toml" | head -n 1
}

source_digest() {
    (cd "$SOURCE_DIR" && find . -type f \
        ! -path './.git/*' ! -path './.idea/*' ! -path '*/.venv/*' \
        ! -path '*/.mypy_cache/*' ! -path '*/.pip-tools-cache/*' \
        ! -path '*/.pytest_cache/*' ! -path '*/.ruff_cache/*' \
        ! -path '*/build/*' ! -path '*/dist/*' ! -path '*/__pycache__/*' \
        ! -path './.aws/*' ! -path './.codex/*' ! -path './.agents/*' \
        ! -name '.coverage*' ! -name '*.pyc' ! -name '.env' ! -name '.env.*' \
        -print0 | sort -z | xargs -0 sha256sum | sha256sum | awk '{print $1}')
}

manifest_value() {
    support manifest "$1"
}

version_is_newer() {
    python3 - "$1" "$2" <<'PY'
import re
import sys

def normalize(value: str) -> tuple[tuple[int, ...], int, int]:
    match = re.fullmatch(r"(\d+(?:\.\d+)*)(?:\.dev(\d+))?", value)
    if match is None:
        raise SystemExit(2)
    release = tuple(int(part) for part in match.group(1).split("."))
    dev = int(match.group(2)) if match.group(2) is not None else 10**9
    return release, dev, len(release)

candidate = normalize(sys.argv[1])
active = normalize(sys.argv[2])
raise SystemExit(0 if candidate > active else 1)
PY
}

ensure_checkout() {
    [[ -f "$SOURCE_DIR/pyproject.toml" ]] || die "pyproject.toml not found in checkout: $SOURCE_DIR"
    [[ -f "$SOURCE_DIR/requirements.lock" ]] || die "requirements.lock not found in checkout"
    [[ -f "$SOURCE_DIR/src/vaultkeep/trigger/cli.py" &&
       -f "$SOURCE_DIR/systemd/vaultkeep-trigger-exec@.service" ]] ||
        die "built-in Trigger source or service templates are missing"
    local found_example=0
    local example
    for example in "$SOURCE_DIR"/examples/*.example; do
        [[ -f "$example" ]] || continue
        found_example=1
        break
    done
    [[ "$found_example" == 1 ]] || die "example configuration files not found"
    [[ -f "$SOURCE_DIR/systemd/vaultkeep@.service" ]] ||
        die "service template not found"
    [[ -f "$SOURCE_DIR/systemd/vaultkeep@.timer" ]] ||
        die "timer template not found"
}

plan_install_or_update() {
    local version=$1 digest=$2
    log "Vaultkeep $MODE plan"
    log "  source: $SOURCE_DIR"
    log "  version: $version"
    log "  source_digest: $digest"
    log "  Trigger is included; monitoring remains opt-in"
    log "  interpreter: Debian python3 >=3.13; hash-locked binary dependencies: requirements.lock"
    log "  release: $VK_RELEASES/$RELEASE_ID"
    log "  current: $VK_CURRENT -> releases/$RELEASE_ID"
    log "  executable: $VK_BIN_LINK -> $VK_CURRENT/venv/bin/vaultkeep"
    log "  service unit: $VK_SERVICE_UNIT"
    log "  timer unit: $VK_TIMER_UNIT"
    if [[ "$COMPONENTS" == core,trigger ]]; then
        log "  shared user worker template: $VK_WORKER_UNIT; monitor services are configured per job"
    fi
    log "  preserve config/state: $VK_CONFIG_ROOT $VK_VAR_ROOT and all user homes, wallet data and archives"
    log "  activity checks: system schedules/jobs, running user managers, runtime locks and foreground processes"
    log "  schedules must be paused/drained manually; enablement and timer timestamps are preserved"
    if [[ "$DRY_RUN" == 1 ]]; then
        log "PLAN activity checks are not a live quiescence guarantee; apply repeats checks before staging and activation"
    fi
}

prepare_directories() {
    run install -d -o root -g root -m 0755 "$VK_PREFIX" "$VK_RELEASES" "$VK_STAGING"
    run install -d -o root -g root -m 0755 "$VK_CONFIG_ROOT"
    run install -d -o root -g root -m 0750 "$VK_JOBS" "$VK_VAR_ROOT" "$VK_STATE_JOBS"
    run install -d -o root -g root -m 0700 "$VK_SECRETS" "$VK_TMP"
}

prepare_registry() {
    if [[ "$DRY_RUN" == 0 && ! -f "$VK_TIMER_REGISTRY" ]]; then
        printf '{}\n' >"$VK_TIMER_REGISTRY"
        chown root:root "$VK_TIMER_REGISTRY"
        chmod 0600 "$VK_TIMER_REGISTRY"
    fi
}

install_examples() {
    local installed=0
    local example destination
    for example in "$SOURCE_DIR"/examples/*.example; do
        [[ -f "$example" ]] || continue
        destination="$VK_JOBS/${example##*/}"
        run install -o root -g root -m 0640 "$example" "$destination"
        installed=1
    done
    [[ "$installed" == 1 ]] || die "example configuration files not found"
}

stage_release() {
    local version=$1 digest=$2
    local stage="$VK_STAGING/$RELEASE_ID-$$"
    if [[ "$DRY_RUN" == 1 ]]; then
        log "PLAN stage release below $stage"
        log "PLAN finalize selected Python entry points in $VK_RELEASES/$RELEASE_ID/venv"
        return
    fi
    [[ ! -e "$stage" ]] || die "staging path already exists: $stage"
    STAGED_RELEASE="$stage"
    install -d -o root -g root -m 0755 "$stage/src"
    rsync --archive --delete \
        --exclude='.git/' --exclude='.idea/' --exclude='.venv/' \
        --exclude='.mypy_cache/' --exclude='.pip-tools-cache/' \
        --exclude='.pytest_cache/' --exclude='.ruff_cache/' \
        --exclude='build/' --exclude='dist/' --exclude='__pycache__/' \
        --exclude='.aws/' --exclude='.codex/' --exclude='.agents/' --exclude='.coverage*' \
        --exclude='.env' --exclude='.env.*' \
        "$SOURCE_DIR/" "$stage/src/"
    python3 -m venv "$stage/venv"
    "$stage/venv/bin/python" -m pip install --require-hashes --only-binary=:all: -r "$stage/src/requirements.lock"
    log "Preparing Vaultkeep $version in staging for validation..."
    install_packages "$stage"
    verify_release "$stage" "$version"
    python3 - "$stage/deployment.json" "$version" "$digest" "$SOURCE_DIR" <<'META'
import json
import sys
from pathlib import Path

Path(sys.argv[1]).write_text(json.dumps({
    "schema_version": 3,
    "version": sys.argv[2],
    "source_digest": sys.argv[3],
    "source_checkout": sys.argv[4],
}, indent=2, sort_keys=True) + "\n")
META
    chown -R root:root "$stage"
}

finalize_release_venv() {
    local release=$1 version=$2
    log "Finalizing Vaultkeep $version at $release (updating command paths)..."
    "$release/venv/bin/python" -m pip install --quiet \
        --force-reinstall --no-build-isolation --no-deps "$release/src"
    verify_release "$release" "$version"
}

replace_release_from_staging() {
    local version=$1
    local release="$VK_RELEASES/$RELEASE_ID"
    [[ -n "$STAGED_RELEASE" ]] || return 0
    CANDIDATE_RELEASE_PATH="$release"
    if [[ -e "$release" ]]; then
        [[ -f "$VK_MANIFEST" && "$(manifest_value active_release)" == "$release" ]] ||
            die "pre-existing inactive release requires operator inspection: $release"
        REPLACED_RELEASE_PATH="$release"
        REPLACED_RELEASE_BACKUP="$ROLLBACK_TMP/replaced-release"
        mv "$release" "$REPLACED_RELEASE_BACKUP"
    fi
    CANDIDATE_RELEASE_CREATED=1
    mv "$STAGED_RELEASE" "$release"
    STAGED_RELEASE=""
    finalize_release_venv "$release" "$version"
}

verify_systemd_templates() {
    if [[ "$DRY_RUN" == 1 ]]; then
        log "PLAN verify systemd templates"
        return
    fi
    local tmp
    tmp=$(mktemp -d "$VK_STAGING/units.XXXXXX")
    UNIT_VALIDATION_DIR="$tmp"
    sed -E 's#^ExecStart=/usr/local/bin/vaultkeep .*$#ExecStart=/bin/true#' \
        "$SOURCE_DIR/systemd/vaultkeep@.service" >"$tmp/vaultkeep@.service"
    awk '
        { print }
        $0 == "Unit=vaultkeep@%i.service" { print "OnCalendar=*-*-* 00:00:00" }
    ' "$SOURCE_DIR/systemd/vaultkeep@.timer" >"$tmp/vaultkeep@.timer"
    systemd-analyze verify "$tmp/vaultkeep@.service" "$tmp/vaultkeep@.timer"
    if [[ "$COMPONENTS" == core,trigger ]]; then
        local name
        for name in vaultkeep-trigger-exec@.service; do
            sed -E 's#^ExecStart=.*#ExecStart=/bin/true#' \
                "$SOURCE_DIR/systemd/$name" >"$tmp/$name"
        done
        install -d -m 0700 "$tmp/runtime"
        XDG_RUNTIME_DIR="$tmp/runtime" systemd-analyze --user verify \
            "$tmp/vaultkeep-trigger-exec@.service"
    fi
    rm -rf "$tmp"
    UNIT_VALIDATION_DIR=""
}

refuse_active_services() {
    if [[ "$DRY_RUN" == 1 ]]; then
        log "PLAN confirm paused core timers/services and Trigger monitors/workers; no relevant queued jobs or held locks"
        return
    fi
    support activity "$CHECK_USERS" "$([[ "$MODE" == uninstall ]] && echo 1 || echo 0)" "$COMPONENTS"
}

install_units_and_links() {
    run install -d -o root -g root -m 0755 "$VK_SYSTEMD_ROOT" "$(dirname "$VK_BIN_LINK")"
    run install -o root -g root -m 0644 "$SOURCE_DIR/systemd/vaultkeep@.service" "$VK_SERVICE_UNIT"
    run install -o root -g root -m 0644 "$SOURCE_DIR/systemd/vaultkeep@.timer" "$VK_TIMER_UNIT"
    if [[ "$COMPONENTS" == core,trigger ]]; then
        run install -d -o root -g root -m 0755 "$VK_USER_SYSTEMD_ROOT"
        run install -o root -g root -m 0644 "$SOURCE_DIR/systemd/vaultkeep-trigger-exec@.service" "$VK_WORKER_UNIT"
    fi
    if [[ "$DRY_RUN" == 0 ]]; then
        ln -s "releases/$RELEASE_ID" "$VK_PREFIX/.current.$$"
        mv -Tf "$VK_PREFIX/.current.$$" "$VK_CURRENT"
        ln -sfn "$VK_CURRENT/venv/bin/vaultkeep" "$VK_BIN_LINK"
        if [[ "$PREVIOUS_SCHEMA" != 3 && "$PREVIOUS_COMPONENTS" == core,trigger ]]; then
            rm -f "$VK_TRIGGER_BIN_LINK" "$VK_MONITOR_UNIT"
        fi
    else
        log "PLAN switch $VK_CURRENT to releases/$RELEASE_ID and reconcile selected command links"
    fi
}

begin_commit_transaction() {
    if [[ "$DRY_RUN" == 1 ]]; then
        return
    fi
    if [[ "$COMPONENTS" == core,trigger ]]; then
        SNAPSHOT_PATHS+=("$VK_TRIGGER_BIN_LINK" "$VK_MONITOR_UNIT" "$VK_WORKER_UNIT")
    fi
    local index path
    ROLLBACK_TMP=$(mktemp -d "$VK_PREFIX/.transaction.XXXXXX")
    for index in "${!SNAPSHOT_PATHS[@]}"; do
        path=${SNAPSHOT_PATHS[$index]}
        if [[ -e "$path" || -L "$path" ]]; then
            cp -a --no-dereference "$path" "$ROLLBACK_TMP/$index"
        fi
    done
    ROLLBACK_ACTIVE=1
}

finish_commit_transaction() {
    [[ "$DRY_RUN" == 0 ]] || return 0
    ROLLBACK_ACTIVE=0
    trap - EXIT INT TERM HUP
    rm -rf "$ROLLBACK_TMP"
}

restore_or_remove_file() {
    local backup=$1 destination=$2
    if [[ -e "$backup" || -L "$backup" ]]; then
        rm -f "$destination" && cp -a --no-dereference "$backup" "$destination"
    else
        rm -f "$destination"
    fi
}

rollback_commit() {
    local status=${1:-1}
    trap - EXIT INT TERM HUP
    set +e
    [[ "$status" != 0 ]] || status=1
    error "installer transaction failed (exit $status); cleaning up; schedules remain paused"
    if [[ "$ROLLBACK_ACTIVE" == 1 ]]; then
        local index path
        for index in "${!SNAPSHOT_PATHS[@]}"; do
            path=${SNAPSHOT_PATHS[$index]}
            restore_or_remove_file "$ROLLBACK_TMP/$index" "$path" || rollback_failure "$path"
        done
    fi
    if [[ "$CANDIDATE_RELEASE_CREATED" == 1 ]]; then
        rm -rf "$CANDIDATE_RELEASE_PATH" || rollback_failure "$CANDIDATE_RELEASE_PATH"
    fi
    if [[ -n "$REPLACED_RELEASE_BACKUP" && -d "$REPLACED_RELEASE_BACKUP" ]]; then
        mv "$REPLACED_RELEASE_BACKUP" "$REPLACED_RELEASE_PATH" || rollback_failure "$REPLACED_RELEASE_PATH"
    fi
    if [[ -n "$STAGED_RELEASE" ]]; then
        rm -rf "$STAGED_RELEASE" || rollback_failure "$STAGED_RELEASE"
    fi
    if [[ -n "$UNIT_VALIDATION_DIR" ]]; then
        rm -rf "$UNIT_VALIDATION_DIR" || rollback_failure "$UNIT_VALIDATION_DIR"
    fi
    rm -f "$VK_PREFIX/.current.$$" "${VK_MANIFEST%.json}.tmp"
    if [[ "$ROLLBACK_ACTIVE" == 1 ]]; then
        systemctl daemon-reload || rollback_failure "system manager daemon-reload"
        if [[ "$CHECK_USERS" == 1 && "$COMPONENTS" == core,trigger ]]; then
            support reload-users || rollback_failure "running user managers daemon-reload"
        fi
    fi
    if [[ "$RESTORE_FAILED" == 0 ]]; then
        [[ -z "$ROLLBACK_TMP" ]] || rm -rf "$ROLLBACK_TMP"
    else
        error "rollback incomplete; recovery snapshots retained at $ROLLBACK_TMP"
    fi
    exit "$status"
}

write_manifest() {
    local version=$1 digest=$2 previous=${3:-}
    if [[ "$DRY_RUN" == 1 ]]; then
        log "PLAN write $VK_MANIFEST (schema 3; retained: $previous)"
        return
    fi
    support write-manifest "$version" "$digest" "$previous" "$COMPONENTS"
}

daemon_reload() {
    if [[ "$DRY_RUN" == 1 ]]; then
        log "PLAN systemctl daemon-reload; reload running user managers without starting or enabling monitors"
        return
    fi
    systemctl daemon-reload
    if [[ "$COMPONENTS" == core,trigger ]]; then
        support reload-users
    fi
}

validate_existing_mode() {
    local version=$1 digest=$2
    if [[ -f "$VK_MANIFEST" ]]; then
        local active_version active_digest
        active_version=$(manifest_value version)
        active_digest=$(manifest_value source_digest)
        if [[ "$MODE" == "install" ]]; then
            if [[ "$active_version" == "$version" && "$active_digest" == "$digest" ]]; then
                log "existing installation matches candidate; reconciling installed files"
                return 10
            fi
            if [[ "$active_version" == "$version" ]]; then
                log "existing version differs from candidate; reinstalling from current checkout"
                return 11
            fi
            die "existing deployment version $active_version found; use update for a new release"
        fi
        if [[ "$active_version" == "$version" && "$active_digest" == "$digest" ]]; then
            log "installed version and source digest already match; reconciling installed files"
            return 10
        fi
        if [[ "$active_version" == "$version" ]]; then
            die "same version with different source digest is not an update; use install to refresh the installed code"
        fi
        version_is_newer "$version" "$active_version" ||
            die "candidate version $version is not newer than installed version $active_version"
    elif [[ "$MODE" == "update" ]]; then
        die "update requires an existing installation manifest"
    fi
    return 0
}

install_or_update() {
    preflight_host
    ensure_checkout
    local version digest previous_release="" active_release="" mode_result=0 rebuild=1
    local manifest_before=missing manifest_after=missing
    if [[ -f "$VK_MANIFEST" ]]; then
        manifest_before=$(sha256sum "$VK_MANIFEST")
    fi
    version=$(candidate_version)
    [[ "$version" =~ ^[0-9]+(\.[0-9]+)*(\.dev[0-9]+)?$ ]] || die "invalid project version"
    if [[ -f "$VK_MANIFEST" ]]; then
        PREVIOUS_COMPONENTS=$(manifest_value components)
        PREVIOUS_SCHEMA=$(manifest_value schema_version)
        active_release=$(manifest_value active_release)
        previous_release=$(manifest_value retained_release)
        CHECK_USERS=1
    elif [[ "$MODE" == update ]]; then
        die "update requires an existing installation manifest"
    fi
    if [[ "$COMPONENTS" == core,trigger ]]; then
        CHECK_USERS=1
    fi
    RELEASE_ID=$version
    digest=$(source_digest)
    plan_install_or_update "$version" "$digest"
    if validate_existing_mode "$version" "$digest"; then
        mode_result=0
    else
        mode_result=$?
        [[ "$mode_result" == 10 || "$mode_result" == 11 ]] || return "$mode_result"
    fi
    if command -v python3 >/dev/null 2>&1; then
        support ownership "$SOURCE_DIR" "$COMPONENTS"
    fi
    # A clean supported host may bootstrap Debian's missing interpreter. An
    # existing incompatible interpreter was already rejected before any mutation.
    if ! command -v python3 >/dev/null 2>&1; then
        if [[ "$DRY_RUN" == 1 ]]; then
            log "PLAN apt installs Debian python3; verify >=3.13 before venv creation"
        else
            install_dependencies
            verify_python
            support ownership "$SOURCE_DIR" "$COMPONENTS"
        fi
    fi
    acquire_lock
    if [[ -f "$VK_MANIFEST" ]]; then
        manifest_after=$(sha256sum "$VK_MANIFEST")
    fi
    [[ "$manifest_before" == "$manifest_after" ]] ||
        die "deployment changed during preflight; retry maintenance"
    # Recheck metadata/ownership under the installer lock.
    if command -v python3 >/dev/null 2>&1; then
        support ownership "$SOURCE_DIR" "$COMPONENTS"
    fi
    refuse_active_services
    if [[ "$mode_result" == 10 && "$PREVIOUS_SCHEMA" == 3 ]]; then
        if [[ "$DRY_RUN" == 1 ]]; then
            log "PLAN verify installed environment; rebuild through protected transaction if damaged"
            rebuild=0
        elif verify_release "$active_release" "$version"; then
            rebuild=0
        else
            log "installed environment is inconsistent; staging a complete repair"
        fi
    fi
    if [[ -n "$active_release" && "$active_release" != "$VK_RELEASES/$RELEASE_ID" ]]; then
        previous_release=$active_release
    fi
    log "  retained release after success: $previous_release"
    if [[ "$rebuild" == 0 && "$DRY_RUN" == 0 && "$(support needs-reconciliation "$COMPONENTS")" == 0 ]]; then
        log "Vaultkeep $version verified; no deployment changes required"
        return
    fi
    start_transaction
    if [[ "$rebuild" == 1 ]]; then
        install_dependencies
        if [[ "$DRY_RUN" == 0 ]]; then
            verify_python
            support ownership "$SOURCE_DIR" "$COMPONENTS"
        fi
        verify_executables
        check_7z_compatibility
        prepare_directories
        verify_systemd_templates
        stage_release "$version" "$digest"
    fi
    prepare_directories
    refuse_active_services
    support_or_plan_ownership
    begin_commit_transaction
    prepare_registry
    if [[ "$DRY_RUN" == 0 && "$rebuild" == 1 ]]; then
        replace_release_from_staging "$version"
    fi
    install_units_and_links
    install_examples
    write_manifest "$version" "$digest" "$previous_release"
    daemon_reload
    if [[ "$DRY_RUN" == 0 ]]; then
        verify_release "$VK_RELEASES/$RELEASE_ID" "$version"
        support ownership "$SOURCE_DIR" "$COMPONENTS" staging
        refuse_active_services
    fi
    finish_commit_transaction
    log "Vaultkeep $version installed. Validate jobs/configuration and resume previously active schedules explicitly."
}

validate_manifest_for_uninstall() {
    [[ -f "$VK_MANIFEST" ]] || die "installation manifest not found: $VK_MANIFEST"
    COMPONENTS=$(manifest_value components)
    PREVIOUS_COMPONENTS=$COMPONENTS
    PREVIOUS_SCHEMA=$(manifest_value schema_version)
    CHECK_USERS=1
    support ownership "$SOURCE_DIR" "$COMPONENTS"
}

uninstall_plan() {
    log "Vaultkeep uninstall plan"
    log "  require stopped/disabled schedules and drained services/workers/jobs in all running managers"
    log "  remove $VK_SERVICE_UNIT $VK_TIMER_UNIT $VK_BIN_LINK"
    if [[ "$COMPONENTS" == core,trigger ]]; then
        log "  remove $VK_WORKER_UNIT"
        if [[ "$PREVIOUS_SCHEMA" != 3 ]]; then
            log "  remove legacy $VK_MONITOR_UNIT $VK_TRIGGER_BIN_LINK"
        fi
    fi
    log "  remove $VK_PREFIX $VK_TMP $VK_TIMER_REGISTRY"
    if [[ "$PURGE" == 1 ]]; then
        log "  purge $VK_CONFIG_ROOT $VK_VAR_ROOT"
    else
        log "  preserve $VK_CONFIG_ROOT $VK_STATE_JOBS"
    fi
    log "  preserve every user's Trigger config/state/recovery evidence and core data, wallets and archives (also with --purge)"
}

uninstall_vaultkeep() {
    preflight_host
    validate_manifest_for_uninstall
    acquire_lock
    validate_manifest_for_uninstall
    uninstall_plan
    refuse_active_services
    if [[ "$DRY_RUN" == 1 ]]; then
        return
    fi
    # Repeat the live manager inventory immediately before removal.
    refuse_active_services
    rm -f "$VK_SERVICE_UNIT" "$VK_TIMER_UNIT" "$VK_BIN_LINK"
    if [[ "$COMPONENTS" == core,trigger ]]; then
        rm -f "$VK_WORKER_UNIT"
        if [[ "$PREVIOUS_SCHEMA" != 3 ]]; then
            rm -f "$VK_TRIGGER_BIN_LINK" "$VK_MONITOR_UNIT"
        fi
    fi
    # Reload while the support script is still present (installed uninstall source).
    daemon_reload
    rm -rf "$VK_TMP"
    rm -f "$VK_TIMER_REGISTRY"
    if [[ "$PURGE" == 1 ]]; then
        rm -rf "$VK_CONFIG_ROOT" "$VK_VAR_ROOT"
    fi
    rm -rf "$VK_PREFIX"
    log "Vaultkeep uninstalled; user homes and backup destinations preserved"
}


support() {
    python3 "$SOURCE_DIR/scripts/install_support.py" "$@"
}

verify_python() {
    python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 13) else 1)' ||
        die "Debian python3 >=3.13 is required; refusing before apt or deployment changes"
}

install_packages() {
    local release=$1
    "$release/venv/bin/python" -m pip install --quiet --no-build-isolation --no-deps "$release/src"
}

verify_release() {
    local release=$1 version=$2
    [[ -x "$release/venv/bin/python" ]] || return 1
    "$release/venv/bin/python" "$SOURCE_DIR/scripts/install_support.py" environment \
        "$release" "$version" "$COMPONENTS" "$SOURCE_DIR"
}

support_or_plan_ownership() {
    if [[ "$DRY_RUN" == 0 ]]; then
        support ownership "$SOURCE_DIR" "$COMPONENTS" staging
    fi
}

start_transaction() {
    [[ "$DRY_RUN" == 0 ]] || return 0
    trap 'rollback_commit $?' EXIT
    trap 'rollback_commit 130' INT
    trap 'rollback_commit 143' TERM
    trap 'rollback_commit 129' HUP
}

rollback_failure() {
    error "rollback failed: $1"
    RESTORE_FAILED=1
}

main() {
    parse_args "$@"
    case "$MODE" in
        install|update) install_or_update ;;
        uninstall) uninstall_vaultkeep ;;
    esac
}

main "$@"
