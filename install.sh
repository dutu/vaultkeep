#!/usr/bin/env bash
set -Eeuo pipefail

readonly SCRIPT_NAME=${0##*/}
readonly MODE=${1:-}

DRY_RUN=0
PURGE=0

VK_PREFIX=${VAULTKEEP_INSTALL_PREFIX:-/opt/vaultkeep}
VK_CONFIG_ROOT=${VAULTKEEP_CONFIG_ROOT:-/etc/vaultkeep}
VK_VAR_ROOT=${VAULTKEEP_VAR_ROOT:-/var/lib/vaultkeep}
VK_BIN_LINK=${VAULTKEEP_BIN_LINK:-/usr/local/bin/vaultkeep}
VK_SYSTEMD_ROOT=${VAULTKEEP_SYSTEMD_ROOT:-/etc/systemd/system}
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
PREVIOUS_CURRENT_TARGET=""
PREVIOUS_BIN_TARGET=""
CANDIDATE_RELEASE_PATH=""
CANDIDATE_RELEASE_CREATED=0

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

die() {
    printf '%s: %s\n' "$SCRIPT_NAME" "$*" >&2
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

preflight_host() {
    if [[ "$VK_TESTING" == 1 ]]; then
        return
    fi
    [[ "$(id -u)" == 0 ]] || die "installer requires root"
    [[ -r /etc/os-release ]] || die "/etc/os-release is required"
    # shellcheck disable=SC1091
    . /etc/os-release
    [[ "${ID:-}" == "debian" ]] || die "only Debian is supported"
    case " ${VERSION_CODENAME:-} ${VERSION_ID:-} " in
        *" bookworm "*|*" trixie "*|*" 12 "*|*" 13 "*) ;;
        *) die "supported Debian releases are bookworm/trixie only" ;;
    esac
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
    printf '%s\n%s\n' 'vaultkeep-installer-test' 'vaultkeep-installer-test' |
        7z a -t7z -mhe=on -sccUTF-8 -bd -p "$tmp/payload.tar.7z" "$tmp/payload.tar" >/dev/null
    printf '%s\n' 'vaultkeep-installer-test' |
        7z t -bd -p "$tmp/payload.tar.7z" >/dev/null
}

candidate_version() {
    sed -nE 's/^version = "([^"]+)"/\1/p' "$SOURCE_DIR/pyproject.toml" | head -n 1
}

source_digest() {
    (cd "$SOURCE_DIR" && find . -type f \
        ! -path './.git/*' \
        ! -path './.idea/*' \
        ! -path './.venv/*' \
        ! -path './.mypy_cache/*' \
        ! -path './.pip-tools-cache/*' \
        ! -path './.pytest_cache/*' \
        ! -path './.ruff_cache/*' \
        ! -path './build/*' \
        ! -path './dist/*' \
        ! -path '*/__pycache__/*' \
        -print0 |
        sort -z |
        xargs -0 sha256sum |
        sha256sum |
        awk '{print $1}')
}

manifest_value() {
    local key=$1
    [[ -f "$VK_MANIFEST" ]] || return 1
    python3 - "$VK_MANIFEST" "$key" <<'PY'
import json
import sys
from pathlib import Path

data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
value = data
for part in sys.argv[2].split("."):
    value = value[part]
print(value)
PY
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
    [[ -f "$SOURCE_DIR/examples/vaultkeep-job.yaml.disabled" ]] ||
        die "disabled example configuration not found"
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
    log "  release: $VK_RELEASES/$version"
    log "  current: $VK_CURRENT -> releases/$version"
    log "  executable: $VK_BIN_LINK -> $VK_CURRENT/venv/bin/vaultkeep"
    log "  service unit: $VK_SERVICE_UNIT"
    log "  timer unit: $VK_TIMER_UNIT"
    log "  config root: $VK_CONFIG_ROOT"
    log "  state root: $VK_VAR_ROOT"
}

prepare_directories() {
    run install -d -o root -g root -m 0755 "$VK_PREFIX" "$VK_RELEASES" "$VK_STAGING"
    run install -d -o root -g root -m 0755 "$VK_CONFIG_ROOT"
    run install -d -o root -g root -m 0750 "$VK_JOBS" "$VK_VAR_ROOT" "$VK_STATE_JOBS"
    run install -d -o root -g root -m 0700 "$VK_SECRETS" "$VK_TMP"
    if [[ "$DRY_RUN" == 0 && ! -f "$VK_TIMER_REGISTRY" ]]; then
        printf '{}\n' >"$VK_TIMER_REGISTRY"
        chown root:root "$VK_TIMER_REGISTRY"
        chmod 0600 "$VK_TIMER_REGISTRY"
    fi
}

install_example() {
    local destination="$VK_JOBS/example.yaml.disabled"
    if [[ -e "$destination" ]]; then
        log "preserve existing example: $destination"
        return
    fi
    run install -o root -g root -m 0640 \
        "$SOURCE_DIR/examples/vaultkeep-job.yaml.disabled" "$destination"
}

stage_release() {
    local version=$1 digest=$2
    local stage="$VK_STAGING/$version-$$"
    local release="$VK_RELEASES/$version"
    if [[ "$DRY_RUN" == 1 ]]; then
        log "PLAN stage release below $stage"
        return
    fi
    [[ ! -e "$stage" ]] || die "staging path already exists: $stage"
    rm -rf "$stage"
    install -d -o root -g root -m 0755 "$stage/src"
    rsync --archive --delete \
        --exclude='.git/' \
        --exclude='.idea/' \
        --exclude='.venv/' \
        --exclude='.mypy_cache/' \
        --exclude='.pip-tools-cache/' \
        --exclude='.pytest_cache/' \
        --exclude='.ruff_cache/' \
        --exclude='build/' \
        --exclude='dist/' \
        --exclude='__pycache__/' \
        "$SOURCE_DIR/" "$stage/src/"
    python3 -m venv "$stage/venv"
    "$stage/venv/bin/python" -m pip install --require-hashes -r "$stage/src/requirements.lock"
    "$stage/venv/bin/python" -m pip install --no-build-isolation --no-deps "$stage/src"
    "$stage/venv/bin/vaultkeep" --version | grep -Fx "$version" >/dev/null
    "$stage/venv/bin/vaultkeep" --config "$stage/src/examples/vaultkeep-job.yaml.disabled" \
        validate --schema-only >/dev/null
    python3 - "$stage/deployment.json" "$version" "$digest" "$SOURCE_DIR" <<'PY'
import json
import sys
from pathlib import Path

Path(sys.argv[1]).write_text(
    json.dumps(
        {
            "schema_version": 1,
            "version": sys.argv[2],
            "source_digest": sys.argv[3],
            "source_checkout": sys.argv[4],
        },
        indent=2,
        sort_keys=True,
    )
    + "\n",
    encoding="utf-8",
)
PY
    chown -R root:root "$stage"
    if [[ -e "$release" ]]; then
        rm -rf "$stage"
        die "release already exists: $release"
    fi
    mv "$stage" "$release"
    CANDIDATE_RELEASE_PATH="$release"
    CANDIDATE_RELEASE_CREATED=1
}

verify_systemd_templates() {
    if [[ "$DRY_RUN" == 1 || "$VK_TESTING" == 1 ]]; then
        log "PLAN verify systemd templates"
        return
    fi
    systemd-analyze verify "$SOURCE_DIR/systemd/vaultkeep@.service" "$SOURCE_DIR/systemd/vaultkeep@.timer"
}

refuse_active_services() {
    if [[ "$DRY_RUN" == 1 || "$VK_TESTING" == 1 ]]; then
        log "PLAN confirm no active vaultkeep@*.service instances"
        return
    fi
    if systemctl list-units --state=active 'vaultkeep@*.service' --no-legend | grep -q .; then
        die "one or more Vaultkeep backup services are active"
    fi
}

install_units_and_links() {
    local version=$1
    run install -o root -g root -m 0644 "$SOURCE_DIR/systemd/vaultkeep@.service" "$VK_SERVICE_UNIT"
    run install -o root -g root -m 0644 "$SOURCE_DIR/systemd/vaultkeep@.timer" "$VK_TIMER_UNIT"
    if [[ "$DRY_RUN" == 0 ]]; then
        local tmp_link="$VK_PREFIX/.current.$$"
        ln -s "releases/$version" "$tmp_link"
        mv -Tf "$tmp_link" "$VK_CURRENT"
        ln -sfn "$VK_CURRENT/venv/bin/vaultkeep" "$VK_BIN_LINK"
    else
        log "PLAN switch $VK_CURRENT to releases/$version"
        log "PLAN create $VK_BIN_LINK"
    fi
}

begin_commit_transaction() {
    local version=$1
    if [[ "$DRY_RUN" == 1 ]]; then
        return
    fi
    ROLLBACK_TMP=$(mktemp -d)
    CANDIDATE_RELEASE_PATH="$VK_RELEASES/$version"
    if [[ -L "$VK_CURRENT" ]]; then
        PREVIOUS_CURRENT_TARGET=$(readlink "$VK_CURRENT")
    fi
    if [[ -L "$VK_BIN_LINK" ]]; then
        PREVIOUS_BIN_TARGET=$(readlink "$VK_BIN_LINK")
    fi
    [[ -f "$VK_SERVICE_UNIT" ]] && cp -a "$VK_SERVICE_UNIT" "$ROLLBACK_TMP/service"
    [[ -f "$VK_TIMER_UNIT" ]] && cp -a "$VK_TIMER_UNIT" "$ROLLBACK_TMP/timer"
    [[ -f "$VK_MANIFEST" ]] && cp -a "$VK_MANIFEST" "$ROLLBACK_TMP/manifest"
    [[ -f "$VK_TIMER_REGISTRY" ]] && cp -a "$VK_TIMER_REGISTRY" "$ROLLBACK_TMP/timer-registry"
    ROLLBACK_ACTIVE=1
    trap rollback_commit ERR
}

finish_commit_transaction() {
    if [[ "$DRY_RUN" == 1 ]]; then
        return
    fi
    ROLLBACK_ACTIVE=0
    trap - ERR
    rm -rf "$ROLLBACK_TMP"
}

restore_or_remove_file() {
    local backup=$1 destination=$2
    if [[ -f "$backup" ]]; then
        cp -a "$backup" "$destination"
    else
        rm -f "$destination"
    fi
}

rollback_commit() {
    local status=$?
    trap - ERR
    if [[ "$ROLLBACK_ACTIVE" == 1 ]]; then
        log "rolling back incomplete Vaultkeep installer transaction" >&2
        if [[ -n "$PREVIOUS_CURRENT_TARGET" ]]; then
            ln -sfn "$PREVIOUS_CURRENT_TARGET" "$VK_CURRENT"
        else
            rm -f "$VK_CURRENT"
        fi
        if [[ -n "$PREVIOUS_BIN_TARGET" ]]; then
            ln -sfn "$PREVIOUS_BIN_TARGET" "$VK_BIN_LINK"
        else
            rm -f "$VK_BIN_LINK"
        fi
        restore_or_remove_file "$ROLLBACK_TMP/service" "$VK_SERVICE_UNIT"
        restore_or_remove_file "$ROLLBACK_TMP/timer" "$VK_TIMER_UNIT"
        restore_or_remove_file "$ROLLBACK_TMP/manifest" "$VK_MANIFEST"
        restore_or_remove_file "$ROLLBACK_TMP/timer-registry" "$VK_TIMER_REGISTRY"
        if [[ "$CANDIDATE_RELEASE_CREATED" == 1 && "$CANDIDATE_RELEASE_PATH" == "$VK_RELEASES/"* ]]; then
            rm -rf "$CANDIDATE_RELEASE_PATH"
        fi
        if [[ "$VK_TESTING" != 1 ]]; then
            systemctl daemon-reload >/dev/null 2>&1 || true
        fi
        rm -rf "$ROLLBACK_TMP"
    fi
    exit "$status"
}

write_manifest() {
    local version=$1 digest=$2 previous=${3:-}
    if [[ "$DRY_RUN" == 1 ]]; then
        log "PLAN write $VK_MANIFEST"
        return
    fi
    python3 - "$VK_MANIFEST" "$version" "$digest" "$VK_PREFIX" "$VK_RELEASES/$version" \
        "$previous" "$VK_CURRENT" "$VK_BIN_LINK" "$VK_SERVICE_UNIT" "$VK_TIMER_UNIT" \
        "$VK_CONFIG_ROOT" "$VK_VAR_ROOT" "$VK_TIMER_REGISTRY" <<'PY'
import hashlib
import json
import os
import sys
from pathlib import Path

(
    manifest,
    version,
    digest,
    prefix,
    release,
    previous,
    current,
    bin_link,
    service_unit,
    timer_unit,
    config_root,
    var_root,
    timer_registry,
) = sys.argv[1:]

def file_digest(path: str) -> str:
    item = Path(path)
    if not item.exists() or item.is_symlink():
        return ""
    return hashlib.sha256(item.read_bytes()).hexdigest()

def record(path: str) -> dict[str, object]:
    item = Path(path)
    if item.is_symlink():
        return {
            "path": str(item),
            "type": "symlink",
            "target": os.readlink(item),
        }
    return {
        "path": str(item),
        "type": "file" if item.is_file() else "directory",
        "mode": oct(item.stat().st_mode & 0o777),
        "sha256": file_digest(path),
    }

data = {
    "schema_version": 1,
    "version": version,
    "source_digest": digest,
    "prefix": prefix,
    "active_release": release,
    "retained_release": previous,
    "current_symlink": current,
    "bin_symlink": bin_link,
    "config_root": config_root,
    "var_root": var_root,
    "timer_registry": timer_registry,
    "owned_artifacts": [
        record(prefix),
        record(release),
        record(current),
        record(bin_link),
        record(service_unit),
        record(timer_unit),
        record(timer_registry),
    ],
}
target = Path(manifest)
tmp = target.with_name(target.name + ".tmp")
tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
os.chown(tmp, 0, 0)
os.chmod(tmp, 0o600)
os.replace(tmp, target)
PY
}

daemon_reload_and_sync() {
    if [[ "$DRY_RUN" == 1 || "$VK_TESTING" == 1 ]]; then
        log "PLAN systemctl daemon-reload"
        log "PLAN vaultkeep timers sync"
        return
    fi
    systemctl daemon-reload
    "$VK_BIN_LINK" timers sync
    "$VK_BIN_LINK" timers validate
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
            die "existing deployment found; use update"
        fi
        if [[ "$active_version" == "$version" && "$active_digest" == "$digest" ]]; then
            log "installed version and source digest already match; reconciling installed files"
            return 10
        fi
        if [[ "$active_version" == "$version" ]]; then
            die "same version with different source digest is not a valid update"
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
    acquire_lock
    ensure_checkout
    local version digest previous_release=""
    version=$(candidate_version)
    [[ -n "$version" ]] || die "could not read project version from pyproject.toml"
    digest=$(source_digest)
    plan_install_or_update "$version" "$digest"
    local mode_result=0
    if validate_existing_mode "$version" "$digest"; then
        mode_result=0
    else
        mode_result=$?
        [[ "$mode_result" == 10 ]] || return "$mode_result"
    fi
    if [[ -f "$VK_MANIFEST" ]]; then
        previous_release=$(manifest_value active_release || true)
    fi
    install_dependencies
    verify_executables
    check_7z_compatibility
    prepare_directories
    install_example
    verify_systemd_templates
    if [[ "$mode_result" != 10 ]]; then
        stage_release "$version" "$digest"
    fi
    refuse_active_services
    begin_commit_transaction "$version"
    install_units_and_links "$version"
    write_manifest "$version" "$digest" "$previous_release"
    daemon_reload_and_sync
    finish_commit_transaction
    log "Vaultkeep $version installed"
}

validate_manifest_for_uninstall() {
    [[ -f "$VK_MANIFEST" ]] || die "installation manifest not found: $VK_MANIFEST"
    python3 - "$VK_MANIFEST" "$VK_PREFIX" "$VK_BIN_LINK" "$VK_SERVICE_UNIT" "$VK_TIMER_UNIT" "$VK_TIMER_REGISTRY" <<'PY'
import json
import sys
from pathlib import Path

manifest = Path(sys.argv[1])
prefix = Path(sys.argv[2])
allowed = {Path(arg) for arg in sys.argv[2:]}
data = json.loads(manifest.read_text(encoding="utf-8"))
required = {
    "schema_version",
    "version",
    "source_digest",
    "prefix",
    "active_release",
    "retained_release",
    "current_symlink",
    "bin_symlink",
    "config_root",
    "var_root",
    "timer_registry",
    "owned_artifacts",
}
if set(data) != required:
    raise SystemExit("install manifest has unexpected fields")
if data["schema_version"] != 1:
    raise SystemExit("unsupported install manifest schema")
for artifact in data["owned_artifacts"]:
    path = Path(artifact["path"])
    if not path.is_absolute():
        raise SystemExit(f"manifest path is not absolute: {path}")
    if path == prefix or prefix in path.parents or path in allowed:
        continue
    raise SystemExit(f"manifest path outside installer allowlist: {path}")
PY
}

uninstall_plan() {
    log "Vaultkeep uninstall plan"
    log "  disable vaultkeep timers"
    log "  remove $VK_SERVICE_UNIT"
    log "  remove $VK_TIMER_UNIT"
    log "  remove $VK_BIN_LINK when it is the Vaultkeep symlink"
    log "  remove $VK_PREFIX"
    log "  remove $VK_TMP"
    log "  remove $VK_TIMER_REGISTRY"
    if [[ "$PURGE" == 1 ]]; then
        log "  purge $VK_CONFIG_ROOT"
        log "  purge $VK_VAR_ROOT"
    else
        log "  preserve $VK_CONFIG_ROOT"
        log "  preserve $VK_STATE_JOBS"
    fi
}

remove_if_owned_symlink() {
    local path=$1 target_prefix=$2
    if [[ -L "$path" ]]; then
        local target
        target=$(readlink "$path")
        if [[ "$target" == "$target_prefix"* ]]; then
            run rm -f "$path"
        else
            log "preserve non-Vaultkeep symlink: $path -> $target"
        fi
    fi
}

uninstall_vaultkeep() {
    preflight_host
    acquire_lock
    validate_manifest_for_uninstall
    uninstall_plan
    refuse_active_services
    if [[ "$DRY_RUN" == 1 ]]; then
        return
    fi
    if [[ "$VK_TESTING" != 1 ]]; then
        systemctl disable --now 'vaultkeep@*.timer' >/dev/null 2>&1 || true
    fi
    rm -f "$VK_SERVICE_UNIT" "$VK_TIMER_UNIT"
    remove_if_owned_symlink "$VK_BIN_LINK" "$VK_CURRENT"
    rm -rf "$VK_PREFIX"
    rm -rf "$VK_TMP"
    rm -f "$VK_TIMER_REGISTRY"
    if [[ "$PURGE" == 1 ]]; then
        rm -rf "$VK_CONFIG_ROOT" "$VK_VAR_ROOT"
    fi
    if [[ "$VK_TESTING" != 1 ]]; then
        systemctl daemon-reload
    fi
    log "Vaultkeep uninstalled"
}

main() {
    parse_args "$@"
    case "$MODE" in
        install|update) install_or_update ;;
        uninstall) uninstall_vaultkeep ;;
    esac
}

main "$@"
