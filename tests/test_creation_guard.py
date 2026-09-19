import json
import threading

import pytest
from create_guard import CreationGuard, classify


@pytest.mark.parametrize('error,kind', [
    ('HTTP 429 too many requests', 'throttle'),
    ('You have reached the limit of addresses you can create right now.', 'throttle'),
    ('HTTP 403 please try again later', 'auth'),
    ('HTTP 421 trustTokens', 'auth'),
    ('quota exceeded', 'quota'), ('connection reset', 'network')])
def test_error_classification(error, kind):
    assert classify(error) == kind


def test_cooldown_survives_restart_and_manual_release(tmp_path, monkeypatch):
    import create_guard
    monkeypatch.setattr(create_guard.time, 'time', lambda: 10000)
    path = tmp_path / 'guard.json'
    guard = CreationGuard(path)
    guard.failure('a', 'HTTP 429')
    loaded = CreationGuard(path)
    loaded.unblock('a')
    assert loaded.check('a', {})['retry_after_seconds'] == 1800
    monkeypatch.setattr(create_guard.time, 'time', lambda: 11800)
    assert loaded.check('a', {}) is None
    loaded.failure('a', 'HTTP 429')
    assert loaded.check('a', {})['retry_after_seconds'] == 1800


def test_multi_account_errors_alert_without_blocking_healthy_accounts(tmp_path):
    path = tmp_path / 'guard.json'
    guard = CreationGuard(path)
    for _ in range(4):
        guard.failure('a', 'HTTP 401')
    assert not guard.snapshot()['global_paused']
    guard.failure('b', 'HTTP 401')
    guard.failure('c', 'HTTP 401')
    guard = CreationGuard(path)
    assert guard.snapshot()['multi_account_alert']['accounts'] == 3
    assert guard.check('healthy', {}) is None
    guard.unblock()
    assert guard.check('healthy', {}) is None
    assert guard.check('a', {})['error_kind'] == 'blocked'


def test_legacy_global_pause_no_longer_blocks_other_accounts(tmp_path):
    path = tmp_path / 'guard.json'
    guard = CreationGuard(path)
    guard.data['global_paused'] = True
    guard.failure('bad', 'HTTP 401')
    guard = CreationGuard(path)
    assert guard.check('healthy', {}) is None
    assert guard.check('bad', {})['error_kind'] == 'blocked'


def test_failed_attempts_count_toward_daily_limit(tmp_path):
    guard = CreationGuard(tmp_path / 'guard.json')
    guard.configure(1, 1)
    guard.attempt('a')
    guard.failure('a', 'connection reset')
    guard.unblock('a')
    assert guard.check('a', {})['error_kind'] == 'quota'
    guard.account('a')['day'] = '2000-01-01'
    assert guard.check('a', {}) is None


def test_ambiguous_reserve_remains_after_release(tmp_path):
    guard = CreationGuard(tmp_path / 'guard.json')
    guard.pending('a', 'candidate@icloud.com')
    assert guard.failure('a', 'timeout')['error_kind'] == 'uncertain'
    guard.unblock('a')
    assert CreationGuard(guard.path).snapshot()['accounts']['a']['pending'] == 'candidate@icloud.com'
    assert guard.check('bad', {'mail_status': 'auth_failed'})['error_kind'] == 'blocked'


@pytest.fixture
def manager(tmp_path, monkeypatch):
    import account_manager
    monkeypatch.setattr(account_manager, 'ACCOUNTS_FILE', tmp_path / 'accounts.json')
    monkeypatch.setattr(account_manager, 'LATEST_EMAILS', tmp_path / 'emails.txt')
    monkeypatch.setattr(account_manager, 'CREATE_ALIAS_INTERVAL_SECONDS', 0)
    mgr = account_manager.AccountManager()
    mgr.accounts = {'a': {'id': 'a', 'status': 'active', 'cookies': {}}}
    return mgr


def test_manager_never_calls_network_during_cooldown(manager, monkeypatch):
    import icloud_hme
    monkeypatch.setattr(icloud_hme.ICloudHME, 'create_alias', lambda *a, **k: pytest.fail('network called'))
    manager.creation_guard.failure('a', 'HTTP 429')
    assert manager.create_aliases_for_account('a')[0]['retryable']


def test_uncertain_reserve_reconciles_exact_candidate(manager, monkeypatch):
    import icloud_hme
    manager.creation_guard.pending('a', 'candidate@icloud.com')
    monkeypatch.setattr(icloud_hme.ICloudHME, 'create_alias', lambda *a, **k: pytest.fail('created twice'))
    monkeypatch.setattr(icloud_hme.ICloudHME, 'list_aliases', lambda self: [{'email': 'candidate@icloud.com'}])
    result = manager.create_aliases_for_account('a')
    assert result[0]['email'] == 'candidate@icloud.com'
    assert manager.creation_guard.snapshot()['accounts']['a']['pending'] is None


def test_missing_candidate_stays_blocked_without_new_create(manager, monkeypatch):
    import icloud_hme
    manager.creation_guard.pending('a', 'candidate@icloud.com')
    monkeypatch.setattr(icloud_hme.ICloudHME, 'create_alias', lambda *a, **k: pytest.fail('created twice'))
    monkeypatch.setattr(icloud_hme.ICloudHME, 'list_aliases', lambda self: [])
    assert manager.create_aliases_for_account('a')[0]['error_kind'] == 'uncertain'
    assert manager.creation_guard.snapshot()['accounts']['a']['pending']


def test_reconciliation_does_not_duplicate_local_export(manager, monkeypatch):
    import account_manager
    import icloud_hme
    account_manager.LATEST_EMAILS.write_text('candidate@icloud.com\ta\t2026-09-12\n', encoding='utf-8')
    manager.creation_guard.pending('a', 'candidate@icloud.com')
    monkeypatch.setattr(icloud_hme.ICloudHME, 'list_aliases', lambda self: [{'email': 'candidate@icloud.com'}])
    assert manager.create_aliases_for_account('a')[0]['ok']
    assert len(account_manager.LATEST_EMAILS.read_text(encoding='utf-8').splitlines()) == 1


def test_duplicate_account_rejected_while_waiting_for_global_slot(manager):
    guard = manager.creation_guard
    guard.configure(1, 50)
    guard.claim('other')
    started = threading.Event()
    stop = threading.Event()
    def wait(_):
        started.set()
        stop.wait(2)
    thread = threading.Thread(target=lambda: manager.create_aliases_for_account('a', wait=wait, should_stop=stop.is_set))
    thread.start()
    try:
        assert started.wait(2)
        assert manager.create_aliases_for_account('a')[0]['error_kind'] == 'busy'
    finally:
        stop.set()
        thread.join(3)
        guard.release('other')
    assert not guard.queued


def test_create_client_does_not_retry_reserve(monkeypatch):
    from icloud_hme import ICloudHME
    client = ICloudHME({}, verbose=False)
    calls = []
    client.generate = lambda: 'candidate@icloud.com'
    client.before_reserve = lambda email: calls.append(('pending', email))
    def reserve(*args):
        calls.append(('reserve', args[0]))
        raise RuntimeError('timeout')
    client.reserve = reserve
    with pytest.raises(RuntimeError):
        client.create_alias(max_retries=5)
    assert calls == [('pending', 'candidate@icloud.com'), ('reserve', 'candidate@icloud.com')]


def test_guard_admin_endpoint(manager, monkeypatch):
    import web_ui
    monkeypatch.setattr(web_ui, '_account_mgr', manager)
    client = web_ui.app.test_client()
    assert client.post('/api/creation-guard', json={'action': 'settings', 'concurrency': 0, 'daily_limit': 50}).status_code == 400
    assert client.post('/api/creation-guard', json={'action': 'settings', 'concurrency': 2, 'daily_limit': 20}).status_code == 200
    assert client.get('/api/creation-guard').json['protection']['settings']['daily_limit'] == 20
