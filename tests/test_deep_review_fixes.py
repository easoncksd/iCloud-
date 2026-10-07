"""Behavioral regressions for the 2026-10-07 review; no live accounts/network."""
import imaplib
import json
import os
import shutil
import subprocess
import sys
import tarfile
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def manager(tmp_path, monkeypatch):
    import account_manager as am
    import mail_cache
    monkeypatch.setattr(am, 'ACCOUNTS_FILE', tmp_path / 'accounts.json')
    monkeypatch.setattr(am, 'OLD_COOKIES_FILE', tmp_path / 'old.json')
    monkeypatch.setattr(mail_cache, 'CACHE_FILE', tmp_path / 'headers.json')
    mgr = am.AccountManager()
    mgr._cache = mail_cache.MailCache()
    mgr.accounts = {'a': {'id': 'a', 'app_password': 'fake', 'mail_status': 'ok'}}
    monkeypatch.setattr(mgr, '_save', lambda: None)
    return mgr


def test_real_imap_uidvalidity_contract(monkeypatch):
    import network_proxy
    from icloud_mail import ICloudMail
    config = dict(network_proxy.DEFAULT)
    monkeypatch.setattr(network_proxy, 'load_config', lambda: config)
    class Connection:
        state = 'AUTH'
        debug = 0
        untagged_responses = {'UIDVALIDITY': [b'20261007']}
        response = imaplib.IMAP4.response
        _untagged_response = imaplib.IMAP4._untagged_response
        def select(self, *a, **kw):
            self.state = 'SELECTED'
            return 'OK', [b'2']
        def logout(self): pass
    mail = ICloudMail('fake@example.com', 'fake')
    mail._conn = Connection()
    selected = []
    mail.on_selected = selected.append
    mail._ensure_connected()
    assert mail.uidvalidity == 20261007 and selected == [20261007]
    mail.disconnect()
    assert mail.uidvalidity is None


def test_network_failure_is_not_fresh_success(manager, monkeypatch):
    import web_ui as w
    monkeypatch.setattr(w, '_account_mgr', manager)
    w._apply_mail_watch_result('a', True)
    calls = []
    monkeypatch.setattr(manager, 'test_imap_connection', lambda *a, **kw: calls.append(1) or {'ok': False, 'error': 'timeout'})
    for _ in range(3):
        assert w._apply_mail_watch_result('a', False, 'connection timed out') == 'transient'
        assert w._check_account_mail(manager.accounts['a'], fresh_seconds=3600) == 'transient'
    assert len(calls) == 3
    assert manager.accounts['a']['mail_status'] == 'network_error'
    assert not manager.accounts['a']['mail_sync_paused']


def test_backup_excludes_active_application(tmp_path):
    from process_lock import service_process_lock, maintenance_snapshot_lock, LockAlreadyHeld
    with service_process_lock(tmp_path):
        with pytest.raises(LockAlreadyHeld):
            maintenance_snapshot_lock(tmp_path).acquire()


def test_import_has_no_filesystem_side_effects_with_service_running(tmp_path):
    from process_lock import service_process_lock
    results = tmp_path / 'results'
    results.mkdir()
    links = results / 'pickup_links.json'
    links.write_text(json.dumps({'orphan': {'account_id': 'missing', 'alias_email': 'fake@example.com', 'token': 'fake', 'active': True}}))
    with service_process_lock(tmp_path):
        before = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file() and p.suffix != '.lock'}
        env = dict(os.environ, ICLOUD_DATA_DIR=str(tmp_path))
        result = subprocess.run([sys.executable, '-c', 'import web_ui; assert web_ui._account_mgr is None'],
                                cwd=ROOT, env=env, capture_output=True, text=True, timeout=15)
        assert result.returncode == 0, result.stderr
        after = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file() and p.suffix != '.lock'}
        assert before == after
        assert not (tmp_path / 'logs').exists()


def test_duplicate_import_keeps_mail_pause_and_epoch(manager, monkeypatch):
    import icloud_hme
    manager.accounts['a'].update(real_email='same@example.com', mail_status='auth_failed',
        mail_sync_paused=True, mail_uidvalidity=99, credential_generation='old')
    class Client:
        def __init__(self, *a, **kw): pass
        def validate_session(self): pass
        def get_account_info(self): return {'appleId': 'same@example.com'}
        def list_aliases(self): return []
        def close(self): pass
    monkeypatch.setattr(icloud_hme, 'ICloudHME', Client)
    result = manager.add_account('name', 'cookie=fake')
    assert result['id'] == 'a'
    assert result['mail_sync_paused'] and result['mail_status'] == 'auth_failed'
    assert result['app_password'] == 'fake' and result['mail_uidvalidity'] == 99
    assert result['credential_generation'] != 'old'


def test_uid_reuse_never_returns_old_alias_body(manager, tmp_path, monkeypatch):
    import web_ui as w
    from mail_body_store import MailBodyStore
    manager.accounts['a']['mail_uidvalidity'] = 1
    manager._cache.set_inbox('a', [{'id': '6', 'recipients': ['new@example.com'], '_uidvalidity': 1}])
    class Mail:
        uidvalidity = 2
        def _ensure_connected(self): pass
        def recent_uids(self, **kw): return [b'8', b'7']
        def fetch_header(self, uid): return {'id': uid.decode(), 'recipients': ['new@example.com']}
        def fetch_full(self, uid): return {'body': 'NEW MAIL'}
    manager._mail_clients['a'] = Mail()
    store = MailBodyStore(tmp_path / 'body.sqlite3')
    monkeypatch.setattr(w, '_account_mgr', manager)
    monkeypatch.setattr(w, '_pickup_body_store', store)
    monkeypatch.setattr(w, '_pickup_body_cache', OrderedDict())
    monkeypatch.setattr(w, '_pickup_body_cache_bytes', 0)
    monkeypatch.setattr(w, '_pickup_store', SimpleNamespace(get_by_token=lambda _: {'account_id':'a', 'alias_email':'new@example.com'}))
    monkeypatch.setattr(w, '_pickup_executor', SimpleNamespace(submit=lambda *a: None))
    monkeypatch.setattr(w, '_pickup_body_refreshing', set())
    monkeypatch.setattr(w, '_pickup_pending', 0)
    try:
        store.put('a', '7', {'body': 'PRIVATE LEGACY MAIL'})
        store.put('a', '1:7', {'body': 'PRIVATE OLD ALIAS MAIL', '_uidvalidity': 1})
        synced = manager.sync_pickup_mail('a', ['new@example.com'])
        assert {m['id'] for m in manager._cache.get_alias_mail('a', 'new@example.com')} == {'7', '8'}
        for uid, msg in synced['bodies'].items(): w._store_pickup_body('a', uid, msg)
        response = w.app.test_client().get('/pickup/token/message/7')
        assert response.status_code == 202
        assert 'PRIVATE' not in response.get_data(as_text=True)
        with pytest.raises(RuntimeError, match='世代'):
            w._store_pickup_body('a', '7', {'body': 'late old request', '_uidvalidity': 1})
        w._store_pickup_body('a', '7', {'body': 'NEW 7', '_uidvalidity': 2})
        assert w.app.test_client().get('/pickup/token/message/7').json['message']['body'] == 'NEW 7'
    finally:
        store.close()


def test_evicted_headers_are_not_fetched_again(manager):
    class Mail:
        uidvalidity = 1
        fetched = 0
        hole = None
        def _ensure_connected(self): pass
        def recent_uids(self, **kw): return [str(n).encode() for n in range(1100, 0, -1)]
        def fetch_header(self, uid):
            self.fetched += 1
            if int(uid) == self.hole: return None
            return {'id': uid.decode(), 'recipients': ['ordinary@example.com'],
                    'date': (datetime(2026, 10, 7, tzinfo=timezone.utc) + timedelta(seconds=int(uid))).isoformat()}
    mail = Mail()
    manager._mail_clients['a'] = mail
    manager.sync_pickup_mail('a', ['alias@example.com'])
    assert mail.fetched == 1100
    assert len(manager._cache.get_inbox('a')) == 1000
    mail.fetched = 0
    for _ in range(3): manager.sync_pickup_mail('a', ['alias@example.com'])
    assert mail.fetched == 0
    # Changing linked aliases invalidates the cursor and backfills old mail.
    manager.sync_pickup_mail('a', ['another@example.com'])
    assert mail.fetched == 100


def test_failed_header_does_not_advance_cursor(manager):
    class Mail:
        uidvalidity = 1
        def _ensure_connected(self): pass
        def recent_uids(self, **kw): return [b'2', b'1']
        def fetch_header(self, uid): return None if uid == b'1' else {'id': '2'}
    manager._mail_clients['a'] = Mail()
    manager.sync_pickup_mail('a', ['alias@example.com'])
    assert manager._cache.sync_cursor('a', 1, {'alias@example.com'}) == 0


def test_backup_includes_key_and_verifies_decryption(tmp_path):
    from ops.backup import run, verify_archive
    from credential_store import seal
    root = tmp_path / 'source'
    project = root / 'root/iCloud'
    project.mkdir(parents=True)
    envelope = seal('a', {'cookies': {'test': 'fake'}, 'app_password': 'fake'}, project / '.credentials.key')
    (project / 'accounts.json').write_text(json.dumps({'accounts': {'a': {'credentials_encrypted': envelope}}}))
    output = run(root, tmp_path / 'backups')
    with tarfile.open(output) as archive:
        assert 'root/iCloud/.credentials.key' in archive.getnames()
        manifest = json.load(archive.extractfile('backup-manifest.json'))
        assert manifest['_snapshot']['restore_verification']['accounts'] == 1
    assert verify_archive(output)['credentials_verified']
    (project / '.credentials.key').unlink()
    with pytest.raises(RuntimeError, match='密钥'):
        run(root, tmp_path / 'missing-key-backups')


def bash_run(script, tmp_path):
    bash = 'E:/git/Git/bin/bash.exe' if os.name == 'nt' else shutil.which('bash')
    assert bash and Path(bash).exists(), 'Bash required for deployment behavior tests'
    return subprocess.run([bash, '-c', script], cwd=tmp_path, capture_output=True, text=True, timeout=15)


def test_manifest_explicit_exit_runs_rollback(tmp_path):
    script = (ROOT / 'deploy/install-production.sh').read_text()
    handler = script[script.index('deployment_exit() {'):script.index('\ntrap deployment_exit EXIT')]
    start = script.index('if ! diff -u ')
    branch = script[start:script.index('\nfi', start) + 3]
    result = bash_run('''set -Eeuo pipefail
SOURCE_MANIFEST=unused
BACKUP_DIR=backup
SERVICE_WAS_ACTIVE=1
CHANGES_STARTED=1
rollback() { echo ROLLED_BACK; }
diff() { return 1; }
''' + handler + '\ntrap deployment_exit EXIT\n' + branch, tmp_path)
    assert result.returncode == 1 and 'ROLLED_BACK' in result.stdout


def test_rollback_stops_service_and_preserves_new_runtime(tmp_path):
    script = (ROOT / 'deploy/install-production.sh').read_text()
    start = script.index('rollback() {')
    function = script[start:script.index('\n}\n', start) + 3]
    for base in ('project', 'backup/project'):
        (tmp_path / base / 'results').mkdir(parents=True)
        (tmp_path / base / 'web_ui.py').write_text(base)
        (tmp_path / base / 'accounts.json').write_text(base)
        (tmp_path / base / 'results/state.json').write_text(base)
        (tmp_path / base / 'results/db-wal').write_text(base)
    for name in ('icloud-pickup.conf', '00-icloud-security-zones.conf', 'icloud-hme-override.conf'):
        (tmp_path / 'backup' / name).write_text('old')
    result = bash_run('''set -Eeuo pipefail
BACKUP_DIR=backup
PROJECT_DIR=project
NGINX_VHOST=vhost
NGINX_ZONES=zones
SYSTEMD_OVERRIDE=override
BACKUP_LAUNCHER=launcher
SERVICE_WAS_ACTIVE=1
DATA_LOCK_HELD=1
unlock_runtime() { :; }
systemctl() { echo systemctl_$1; }
nginx() { :; }
remove_source_managed_paths() { echo REMOVE_CODE; rm project/web_ui.py; }
''' + function + '\nrollback', tmp_path)
    assert result.returncode == 0, result.stderr
    assert result.stdout.index('systemctl_stop') < result.stdout.index('REMOVE_CODE')
    assert (tmp_path / 'project/web_ui.py').read_text() == 'backup/project'
    for path in ('accounts.json', 'results/state.json', 'results/db-wal'):
        assert (tmp_path / 'project' / path).read_text() == 'project'


def test_deployment_installs_versioned_backup_launcher():
    script = (ROOT / 'deploy/install-production.sh').read_text()
    assert 'install -m 700 deploy/icloud-hme-backup "$BACKUP_LAUNCHER"' in script
    launcher = (ROOT / 'deploy/icloud-hme-backup').read_text()
    assert '/root/iCloud/ops/backup.py' in launcher
    assert launcher.index('systemctl stop') < launcher.index('/root/iCloud/ops/backup.py')


def test_epoch_switch_rejects_late_header_publication(manager):
    manager._observe_mail_epoch('a', 2)
    manager._cache.set_inbox('a', [{'id': '7', '_uidvalidity': 1}])
    manager._cache.set_alias_mail_batch('a', {'alias@example.com': [{'id': '7', '_uidvalidity': 1}]})
    assert not manager._cache.get_inbox('a')
    assert not manager._cache.get_alias_mail('a', 'alias@example.com')


def test_body_fetch_rejects_reconnected_epoch(manager):
    class Mail:
        uidvalidity = 2
        def _ensure_connected(self): pass
        def fetch_full(self, uid): pytest.fail('must not fetch old UID in a new epoch')
        def disconnect(self): pass
    manager.accounts['a']['mail_uidvalidity'] = 1
    manager._mail_clients['a'] = Mail()
    # Retry also reconnects to the new epoch, never silently accepts old UID.
    manager.get_mail_client = lambda *a, **kw: Mail()
    with pytest.raises(RuntimeError, match='重置'):
        manager.fetch_pickup_message('a', '7', expected_epoch=1)


def test_bulk_header_fetch_maps_server_uid_not_sequence_number(monkeypatch):
    from icloud_mail import ICloudMail
    mail = ICloudMail('fake@example.com', 'fake')
    monkeypatch.setattr(mail, '_ensure_connected', lambda: None)
    mail.uidvalidity = 4
    calls = []
    def uid(*args):
        calls.append(args)
        return 'OK', [(b'1 (UID 42 BODY[HEADER] {39}', b'To: alias@example.com\r\nSubject: test\r\n'),
                      b')', (b'2 (UID 99 BODY[HEADER] {23}', b'To: other@example.com\r\n'), b')']
    mail._conn = SimpleNamespace(uid=uid)
    result = mail.fetch_headers(['42'])
    assert list(result) == ['42'] and result['42']['id'] == '42'
    assert result['42']['_uidvalidity'] == 4
    assert len(calls) == 1 and calls[0][0] == 'FETCH'


def test_bootstrap_batches_headers_and_bounds_body_warmup(manager):
    class Mail:
        uidvalidity = 1
        header_calls = 0
        body_calls = 0
        def _ensure_connected(self): pass
        def recent_uids(self, **kw): return [str(n).encode() for n in range(250, 0, -1)]
        def fetch_headers(self, batch):
            self.header_calls += 1
            return {uid: {'id': uid, 'recipients': [f'alias{uid}@example.com']} for uid in batch}
        def fetch_full(self, uid):
            self.body_calls += 1
            return {'body': 'fake'}
    mail = Mail()
    manager._mail_clients['a'] = mail
    aliases = [f'alias{uid}@example.com' for uid in range(1, 251)]
    manager.sync_pickup_mail('a', aliases)
    assert mail.header_calls == 3 and mail.body_calls == 8
    assert len(manager._cache.get_inbox('a')) == 250
    assert manager._cache.sync_cursor('a', 1, set(aliases)) == 250
