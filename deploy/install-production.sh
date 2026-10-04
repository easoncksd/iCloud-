#!/usr/bin/env bash
set -Eeuo pipefail

SOURCE_DIR=${1:?usage: install-production.sh SOURCE_DIR}
PROJECT_DIR=/root/iCloud
NGINX_VHOST=/www/server/panel/vhost/nginx/icloud-pickup.conf
NGINX_ZONES=/www/server/panel/vhost/nginx/00-icloud-security-zones.conf
SYSTEMD_OVERRIDE=/etc/systemd/system/icloud-hme.service.d/pickup.conf
STAMP=$(date +%Y%m%d-%H%M%S)
BACKUP_DIR=/root/icloud-production-backup-$STAMP

if [[ $(id -u) -ne 0 ]]; then
    echo "must run as root" >&2
    exit 1
fi

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
"$PYTHON_BIN" -c 'import pytest' 2>/dev/null || {
    echo "pytest is required; install requirements-dev.txt in the deploy venv" >&2
    exit 1
}
"$PYTHON_BIN" -m pytest -q

if ! command -v rsync >/dev/null 2>&1; then
    echo "rsync is required for complete source deployment" >&2
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

mkdir -m 700 "$BACKUP_DIR"
cp -a "$PROJECT_DIR" "$BACKUP_DIR/project"
cp -a "$NGINX_VHOST" "$BACKUP_DIR/icloud-pickup.conf"
cp -a "$NGINX_ZONES" "$BACKUP_DIR/00-icloud-security-zones.conf"
cp -a "$SYSTEMD_OVERRIDE" "$BACKUP_DIR/icloud-hme-override.conf"
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

rollback() {
    trap - ERR
    echo "deployment failed; restoring $BACKUP_DIR" >&2
    cp -a "$BACKUP_DIR/project/." "$PROJECT_DIR/"
    cp -a "$BACKUP_DIR/icloud-pickup.conf" "$NGINX_VHOST"
    cp -a "$BACKUP_DIR/00-icloud-security-zones.conf" "$NGINX_ZONES"
    cp -a "$BACKUP_DIR/icloud-hme-override.conf" "$SYSTEMD_OVERRIDE"
    systemctl daemon-reload
    nginx -t
    systemctl restart icloud-hme.service
    systemctl reload nginx
}
trap rollback ERR

mkdir -p "$PROJECT_DIR"
rsync -a --delete \
    --exclude='/results/***' --exclude='/logs/***' \
    --exclude='/accounts.json' --exclude='/.credentials.key' \
    --exclude='/.git/***' --exclude='/.venv/***' \
    --exclude='/.pytest_cache/***' --exclude='*/__pycache__/***' \
    --exclude='*.pyc' --exclude='*.tmp' \
    "$SOURCE_DIR/" "$PROJECT_DIR/"

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

nginx -t
systemctl daemon-reload
systemctl restart icloud-hme.service
systemctl reload nginx

for _ in $(seq 1 40); do
    if curl -fsS http://127.0.0.1:5050/healthz >/dev/null; then
        break
    fi
    sleep 0.25
done
curl -fsS http://127.0.0.1:5050/healthz >/dev/null

# Old combined logs contain bearer URLs. Keep the protected backup and start a
# fresh redacted log after the new format is active.
: > /www/wwwlogs/icloud-mail-access.log
nginx -s reopen

trap - ERR
echo "deployment complete"
echo "backup=$BACKUP_DIR"
