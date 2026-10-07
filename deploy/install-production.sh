#!/usr/bin/env bash
set -Eeuo pipefail

SOURCE_DIR=${1:?usage: install-production.sh SOURCE_DIR}
PROJECT_DIR=/root/iCloud
NGINX_VHOST=/www/server/panel/vhost/nginx/icloud-pickup.conf
NGINX_ZONES=/www/server/panel/vhost/nginx/00-icloud-security-zones.conf
SYSTEMD_OVERRIDE=/etc/systemd/system/icloud-hme.service.d/pickup.conf
STAMP=$(date +%Y%m%d-%H%M%S)
BACKUP_DIR=/root/icloud-production-backup-$STAMP
BACKUP_LAUNCHER=/usr/local/sbin/icloud-hme-backup

if [[ $(id -u) -ne 0 ]]; then
    echo "must run as root" >&2
    exit 1
fi

exec 9>/run/lock/icloud-hme-operations.lock
flock -w 300 9

cd "$SOURCE_DIR"
PYTHON_BIN=${PYTHON_BIN:-/root/iCloud/.venv/bin/python}
if [[ ! -x "$PYTHON_BIN" ]]; then
    PYTHON_BIN=$(command -v python3 || true)
fi
[[ -n "$PYTHON_BIN" && -x "$PYTHON_BIN" ]] || {
    echo "python3 is required for compileall and tests" >&2
    exit 1
}

# Validate every Python source file and run the complete test suite before any
# service files are changed.  Running one regression script is not enough: it
# misses the standalone tests for persistence, proxy handling, and cleanup.
"$PYTHON_BIN" -m compileall -q .
if "$PYTHON_BIN" -c 'import pytest' 2>/dev/null; then
    "$PYTHON_BIN" -m pytest -q
elif [[ "${SKIP_DEPLOY_TESTS:-0}" == "1" ]]; then
    echo "warning: pytest is not installed in the production runtime; local CI tests must be green before deployment" >&2
else
    echo "pytest is required; set SKIP_DEPLOY_TESTS=1 only after external CI verification" >&2
    exit 1
fi

build_source_manifest() {
    local root=$1 output=$2
    (
        cd "$root"
        find . -type f \
            ! -path './results/*' ! -path './logs/*' \
            ! -path './.git/*' ! -path './.venv/*' \
            ! -path './.pytest_cache/*' ! -path '*/__pycache__/*' \
            ! -name 'accounts.json' ! -name '.credentials.key' \
            ! -name '*.tmp' ! -name '*.pyc' \
            -print0 | sort -z | xargs -0 sha256sum
    ) > "$output"
}

SOURCE_MANIFEST=$(mktemp)
trap 'rm -f "$SOURCE_MANIFEST"' EXIT
build_source_manifest "$SOURCE_DIR" "$SOURCE_MANIFEST"

SERVICE_WAS_ACTIVE=0
CHANGES_STARTED=0
DATA_LOCK_HELD=0
lock_runtime() {
    exec 8>"$PROJECT_DIR/results/icloud-hme.service.lock"
    flock -w 45 8 || return 1
    DATA_LOCK_HELD=1
}
unlock_runtime() {
    if [[ "$DATA_LOCK_HELD" == 1 ]]; then
        flock -u 8
        exec 8>&-
        DATA_LOCK_HELD=0
    fi
}
deployment_exit() {
    local status=$?
    trap - EXIT ERR
    if [[ "$status" != 0 && "$CHANGES_STARTED" == 1 ]]; then
        if ! rollback; then
            echo "rollback failed; service left stopped for manual recovery; backup=$BACKUP_DIR" >&2
        fi
    elif [[ "$SERVICE_WAS_ACTIVE" == 1 ]] && ! systemctl is-active --quiet icloud-hme.service; then
        unlock_runtime
        systemctl start icloud-hme.service || true
    fi
    rm -f "$SOURCE_MANIFEST"
    exit "$status"
}
trap deployment_exit EXIT
if systemctl is-active --quiet icloud-hme.service; then
    SERVICE_WAS_ACTIVE=1
    systemctl stop icloud-hme.service
fi
"$PYTHON_BIN" ops/backup.py --verified-backup
lock_runtime
mkdir -m 700 "$BACKUP_DIR"
cp -a "$PROJECT_DIR" "$BACKUP_DIR/project"
cp -a "$NGINX_VHOST" "$BACKUP_DIR/icloud-pickup.conf"
cp -a "$NGINX_ZONES" "$BACKUP_DIR/00-icloud-security-zones.conf"
cp -a "$SYSTEMD_OVERRIDE" "$BACKUP_DIR/icloud-hme-override.conf"
if [[ -f "$BACKUP_LAUNCHER" ]]; then
    cp -a "$BACKUP_LAUNCHER" "$BACKUP_DIR/backup-launcher"
fi
cp -a /www/wwwlogs/icloud-mail-access.log "$BACKUP_DIR/" 2>/dev/null || true
cp -a /www/wwwlogs/icloud-mail-error.log "$BACKUP_DIR/" 2>/dev/null || true
chmod -R go-rwx "$BACKUP_DIR"

# Deployment snapshots are only for fast rollback. Daily backups have their
# own retention policy, so keep the newest ten deployment snapshots here.
mapfile -t OLD_DEPLOY_BACKUPS < <(
    find /root -maxdepth 1 -mindepth 1 -type d \
        -name 'icloud-production-backup-*' -printf '%T@ %p\n' \
        | sort -nr | tail -n +11 | cut -d' ' -f2-
)
for old_backup in "${OLD_DEPLOY_BACKUPS[@]}"; do
    [[ "$old_backup" == /root/icloud-production-backup-* ]] || continue
    rm -rf -- "$old_backup"
done

remove_source_managed_paths() {
    while IFS= read -r -d '' item; do
        base=$(basename "$item")
        case "$base" in
            results|logs|accounts.json|.credentials.key|.venv|.git) continue ;;
        esac
        rm -rf -- "$item" || return 1
    done < <(find "$PROJECT_DIR" -mindepth 1 -maxdepth 1 -print0)
}

rollback() {
    echo "deployment failed; restoring $BACKUP_DIR" >&2
    systemctl stop icloud-hme.service || return 1
    if [[ "$DATA_LOCK_HELD" != 1 ]]; then
        lock_runtime || return 1
    fi
    # Revert code/config only. Never overwrite progress, credentials, SQLite,
    # WAL or SHM produced by the new process with the pre-deployment snapshot.
    remove_source_managed_paths || return 1
    tar -C "$BACKUP_DIR/project" \
        --exclude='./results' --exclude='./logs' \
        --exclude='./accounts.json' --exclude='./.credentials.key' \
        --exclude='./.git' --exclude='./.venv' \
        -cf - . | tar -C "$PROJECT_DIR" -xf - || return 1
    cp -a "$BACKUP_DIR/icloud-pickup.conf" "$NGINX_VHOST" || return 1
    cp -a "$BACKUP_DIR/00-icloud-security-zones.conf" "$NGINX_ZONES" || return 1
    cp -a "$BACKUP_DIR/icloud-hme-override.conf" "$SYSTEMD_OVERRIDE" || return 1
    if [[ -f "$BACKUP_DIR/backup-launcher" ]]; then
        cp -a "$BACKUP_DIR/backup-launcher" "$BACKUP_LAUNCHER" || return 1
    fi
    systemctl daemon-reload || return 1
    nginx -t || return 1
    systemctl reload nginx || return 1
    unlock_runtime || return 1
    if [[ "$SERVICE_WAS_ACTIVE" == "1" ]]; then
        systemctl restart icloud-hme.service || return 1
    fi
}

CHANGES_STARTED=1
mkdir -p "$PROJECT_DIR"
if command -v rsync >/dev/null 2>&1; then
    rsync -a --delete \
        --exclude='/results/***' --exclude='/logs/***' \
        --exclude='/accounts.json' --exclude='/.credentials.key' \
        --exclude='/.git/***' --exclude='/.venv/***' \
        --exclude='/.pytest_cache/***' --exclude='*/__pycache__/***' \
        --exclude='*.pyc' --exclude='*.tmp' \
        "$SOURCE_DIR/" "$PROJECT_DIR/"
else
    echo "warning: rsync is not installed; using tar-based source sync" >&2
    # Keep runtime state and the service virtualenv, then replace every
    # source-managed path so removed modules cannot remain on the server.
    remove_source_managed_paths
    tar -C "$SOURCE_DIR" \
        --exclude='./results' --exclude='./logs' \
        --exclude='./accounts.json' --exclude='./.credentials.key' \
        --exclude='./.git' --exclude='./.venv' \
        --exclude='./.pytest_cache' --exclude='*/__pycache__' \
        --exclude='*.pyc' --exclude='*.tmp' -cf - . \
        | tar -C "$PROJECT_DIR" -xf -
fi

# Older source checkouts did not carry the optional GitHub workflow.  Keep the
# production deploy compatible with those checkouts instead of failing while
# trying to install a nonexistent file; the source manifest still records all
# files that were actually deployed.
if [[ -f "$SOURCE_DIR/.github/workflows/tests.yml" ]]; then
    install -m 644 "$SOURCE_DIR/.github/workflows/tests.yml" "$PROJECT_DIR/.github/workflows/tests.yml"
fi
build_source_manifest "$PROJECT_DIR" "$BACKUP_DIR/source-manifest.remote.sha256"
if ! diff -u "$SOURCE_MANIFEST" "$BACKUP_DIR/source-manifest.remote.sha256"; then
    echo "deployed source manifest does not match source checkout" >&2
    exit 1
fi
install -m 600 "$SOURCE_MANIFEST" "$BACKUP_DIR/source-manifest.sha256"
install -m 600 deploy/icloud-pickup.conf "$NGINX_VHOST"
install -m 600 deploy/00-icloud-security-zones.conf "$NGINX_ZONES"
install -m 600 deploy/icloud-hme-override.conf "$SYSTEMD_OVERRIDE"
install -m 700 deploy/icloud-hme-backup "$BACKUP_LAUNCHER"

nginx -t
systemctl daemon-reload
unlock_runtime
if [[ "$SERVICE_WAS_ACTIVE" == "1" ]]; then
    systemctl restart icloud-hme.service
fi
systemctl reload nginx

if [[ "$SERVICE_WAS_ACTIVE" == "1" ]]; then
    for _ in $(seq 1 40); do
        if curl -fsS http://127.0.0.1:5050/healthz >/dev/null; then
            break
        fi
        sleep 0.25
    done
    curl -fsS http://127.0.0.1:5050/healthz >/dev/null
fi

# Old combined logs contain bearer URLs. Keep the protected backup and start a
# fresh redacted log after the new format is active.
: > /www/wwwlogs/icloud-mail-access.log
nginx -s reopen

echo "deployment complete"
echo "backup=$BACKUP_DIR"
