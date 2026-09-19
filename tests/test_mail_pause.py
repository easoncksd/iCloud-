import imaplib
import threading
import time
from types import SimpleNamespace

import pytest


@pytest.fixture
def setup(monkeypatch):
    import web_ui as w
    import account_manager as am
    mgr = am.AccountManager()
    mgr.accounts = {'a': {'id': 'a', 'icloud_email': 'test@icloud.com',
                          'app_password': 'test-password', 'mail_status': 'ok'}}
    monkeypatch.setattr(mgr, '_save', lambda: None)
    monkeypatch.setattr(w, '_account_mgr', mgr)
    monkeypatch.setattr(w, 'ADMIN_ACCESS_TOKEN', '')
    monkeypatch.setattr(w, '_mail_watch_hold_accounts', set())
    monkeypatch.setattr(w, '_pickup_refreshing_accounts', set())
    monkeypatch.setattr(w, '_mail_probe_accounts', set())
    monkeypatch.setattr(w, '_mail_resume_last', {})
    monkeypatch.setattr(w, '_mail_resume_inflight', set())
    monkeypatch.setattr(w, '_pickup_pending', 0)
    monkeypatch.setattr(w, '_pickup_last_account_refresh', {})
    return w, mgr, mgr.accounts['a']


def test_failure_blocks_new_connections_and_background_submissions(setup, monkeypatch):
    w, mgr, a = setup
    calls = []
    monkeypatch.setattr(w, '_pickup_executor', SimpleNamespace(submit=lambda *x: calls.append(x)))
    assert w._apply_mail_watch_result('a', False, 'AUTHENTICATIONFAILED') == 'auth_failed'
    assert a['mail_sync_paused'] and a['mail_next_retry_at'] > time.time() + 3500
    for _ in range(10):
        assert not w._schedule_pickup_account_refresh('a')
    assert calls == []
    with pytest.raises(RuntimeError, match='收信已暂停'):
        mgr.get_mail_client('a')
    # Even a pre-existing cached IMAP client cannot bypass the pause.
    mgr._mail_clients['a'] = object()
    with pytest.raises(RuntimeError, match='收信已暂停'):
        mgr.fetch_pickup_message('a', '7')
    with pytest.raises(RuntimeError, match='收信已暂停'):
        mgr.sync_pickup_mail('a', ['alias@icloud.com'])


def test_probe_waits_until_due_then_resumes_only_after_success(setup, monkeypatch):
    w, mgr, a = setup
    w._apply_mail_watch_result('a', False, 'AUTHENTICATIONFAILED')
    calls = []
    monkeypatch.setattr(mgr, 'test_imap_connection', lambda *x, **kw: calls.append(kw) or {'ok': True})
    assert w._check_account_mail(a) == 'paused'
    assert calls == []
    a['mail_next_retry_at'] = time.time() - 1
    assert w._check_account_mail(a) == 'ok'
    assert calls == [{'allow_paused': True}]
    assert not a['mail_sync_paused'] and a['mail_next_retry_at'] is None


@pytest.mark.parametrize('error', ['AUTHENTICATIONFAILED', 'connection timed out'])
def test_unsuccessful_probe_defers_another_attempt(setup, monkeypatch, error):
    w, mgr, a = setup
    w._apply_mail_watch_result('a', False, 'AUTHENTICATIONFAILED')
    a['mail_next_retry_at'] = 1
    calls = []
    monkeypatch.setattr(mgr, 'test_imap_connection', lambda *x, **kw: calls.append(1) or {'ok': False, 'error': error})
    w._check_account_mail(a)
    assert a['mail_sync_paused'] and a['mail_next_retry_at'] > time.time()
    assert w._check_account_mail(a) == 'paused'
    assert len(calls) == 1


def test_network_error_does_not_pause_healthy_account_or_stale_success_resume(setup):
    w, mgr, a = setup
    assert w._apply_mail_watch_result('a', False, 'connection timed out') == 'transient'
    assert not mgr.mail_sync_paused('a')
    w._apply_mail_watch_result('a', False, 'AUTHENTICATIONFAILED')
    assert w._apply_mail_watch_result('a', True) == 'paused'
    assert mgr.mail_sync_paused('a')


def test_restart_initializes_old_failures_without_resetting_deadline(setup):
    w, mgr, a = setup
    a['mail_status'] = 'auth_failed'
    w._initialize_mail_pauses()
    deadline = a['mail_next_retry_at']
    assert a['mail_sync_paused'] and deadline > time.time()
    w._initialize_mail_pauses()
    assert a['mail_next_retry_at'] == deadline


def test_manual_resume_checks_and_throttles_duplicate_clicks(setup, monkeypatch):
    w, mgr, a = setup
    w._apply_mail_watch_result('a', False, 'AUTHENTICATIONFAILED')
    calls = []
    monkeypatch.setattr(mgr, 'test_imap_connection', lambda *x, **kw: calls.append(1) or {'ok': True})
    monkeypatch.setattr(w, '_schedule_pickup_account_refresh', lambda _: True)
    client = w.app.test_client()
    assert client.post('/api/accounts/a/mail-resume').status_code == 200
    assert not mgr.mail_sync_paused('a')
    assert client.post('/api/accounts/a/mail-resume').status_code == 429
    assert len(calls) == 1
    assert client.post('/api/accounts/missing/mail-resume').status_code == 404


def test_failed_manual_resume_keeps_pause(setup, monkeypatch):
    w, mgr, a = setup
    w._apply_mail_watch_result('a', False, 'AUTHENTICATIONFAILED')
    monkeypatch.setattr(mgr, 'test_imap_connection', lambda *x, **kw: {'ok': False, 'error': 'AUTHENTICATIONFAILED'})
    assert w.app.test_client().post('/api/accounts/a/mail-resume').status_code == 409
    assert mgr.mail_sync_paused('a')


def test_paused_pickup_returns_persisted_body_without_network(setup, monkeypatch):
    w, mgr, a = setup
    w._apply_mail_watch_result('a', False, 'AUTHENTICATIONFAILED')
    monkeypatch.setattr(w, '_pickup_store', SimpleNamespace(get_by_token=lambda _: {'account_id': 'a', 'alias_email': 'alias@icloud.com'}))
    monkeypatch.setattr(mgr, '_cache', SimpleNamespace(get_alias_mail=lambda *x: [{'id': '7'}, {'id': '8'}]))
    monkeypatch.setattr(w, '_pickup_body_store', SimpleNamespace(get=lambda a, m: {'body': 'cached'} if m == '7' else None))
    monkeypatch.setattr(w, '_pickup_body_cache', {})
    monkeypatch.setattr(w, '_pickup_executor', SimpleNamespace(submit=lambda *x: pytest.fail('unexpected network task')))
    client = w.app.test_client()
    assert client.get('/pickup/token/message/7').get_json()['message']['body'] == 'cached'
    assert client.get('/pickup/token/message/8').status_code == 503
    assert '暂停' in client.get('/pickup/token/messages').get_json()['warning']


def test_authentication_rejection_records_pause_and_does_not_retry(setup, monkeypatch):
    import icloud_mail
    w, mgr, a = setup
    calls = []
    class Connection:
        def login(self, *args):
            calls.append(1)
            raise imaplib.IMAP4.error('[AUTHENTICATIONFAILED] Authentication Failed')
        def logout(self):
            pass
    monkeypatch.setattr(icloud_mail.imaplib, 'IMAP4_SSL', lambda *x, **kw: Connection())
    monkeypatch.setattr(mgr, '_cache', SimpleNamespace(get_all_alias_mail=lambda _: {}, get_inbox=lambda _: []))
    with pytest.raises(icloud_mail.MailAuthenticationError):
        mgr.sync_pickup_mail('a', ['alias@icloud.com'])
    assert len(calls) == 1
    assert a['mail_sync_paused']


def test_parallel_probe_is_rejected_without_releasing_first_hold(setup, monkeypatch):
    w, mgr, a = setup
    w._apply_mail_watch_result('a', False, 'AUTHENTICATIONFAILED')
    entered, release = threading.Event(), threading.Event()
    def check(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return {'ok': True}
    monkeypatch.setattr(mgr, 'test_imap_connection', check)
    worker = threading.Thread(target=w._check_account_mail, args=(a,), kwargs={'force': True})
    worker.start()
    try:
        assert entered.wait(3)
        assert w._check_account_mail(a, force=True) == 'busy'
        assert w._mail_pulls_held('a')
    finally:
        release.set()
        worker.join(3)
    assert not w._mail_pulls_held('a')
