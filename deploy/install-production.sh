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
/root/iCloud/.venv/bin/python -m py_compile \
    account_manager.py export_history.py icloud_hme.py icloud_mail.py mail_body_store.py mail_cache.py \
    pickup_links.py scheduler.py web_ui.py
mkdir -p results
/root/iCloud/.venv/bin/python tests/test_regressions.py

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

for file in README.md requirements.txt requirements-dev.txt \
    account_manager.py export_history.py icloud_hme.py icloud_mail.py mail_body_store.py mail_cache.py \
    pickup_links.py scheduler.py web_ui.py; do
    install -m 644 "$file" "$PROJECT_DIR/$file"
done
mkdir -p "$PROJECT_DIR/tests" "$PROJECT_DIR/.github/workflows" "$PROJECT_DIR/deploy"
install -m 644 tests/*.py "$PROJECT_DIR/tests/"
install -m 644 .github/workflows/tests.yml "$PROJECT_DIR/.github/workflows/tests.yml"
install -m 644 deploy/*.conf "$PROJECT_DIR/deploy/"
install -m 755 deploy/install-production.sh "$PROJECT_DIR/deploy/install-production.sh"
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
