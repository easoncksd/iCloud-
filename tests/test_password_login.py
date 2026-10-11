"""Password auth handshake, bounded MFA, credential isolation and recovery."""
import base64
import copy
import hashlib
import json
import time
from urllib.parse import urlparse

import pytest
import requests
import srp

import account_manager as am
import apple_login as al
from icloud_hme import WebSessionStorageError


def response(status=200, body=None, headers=None):
    result = requests.Response()
    result.status_code = status
    result._content = json.dumps(body or {}).encode()
    result.headers.update(headers or {})
    return result


def session_data():
    return {'dsInfo': {'appleId': 'demo@example.com', 'primaryEmail': 'demo@icloud.com'},
            'webservices': {'premiummailsettings': {'url': 'https://maildomainws.icloud.com'}}}


@pytest.mark.parametrize('protocol', ['s2k', 's2k_fo'])
def test_real_srp_handshake_and_mfa_keeps_identity_headers(monkeypatch, protocol):
    """A real local SRP verifier accepts the derived password and both proofs."""
    from srp import _pysrp
    salt = b'apple-salt-123456'
    digest = hashlib.sha256(b'primary-password').digest()
    derived = hashlib.pbkdf2_hmac('sha256', digest.hex().encode() if protocol == 's2k_fo' else digest,
                                salt, 1000, 32)
    n, g = _pysrp.get_ng(srp.NG_2048, None, None)
    exponent = int.from_bytes(hashlib.sha256(salt + hashlib.sha256(b':' + derived).digest()).digest(), 'big')
    verification_key = pow(g, exponent, n).to_bytes(256, 'big')
    verifier = None
    calls = []

    def send(session, method, url, **kwargs):
        nonlocal verifier
        path = urlparse(url).path
        calls.append(path)
        assert kwargs['allow_redirects'] is False and 0 < kwargs['timeout'] <= 10
        if path.endswith('/authorize/signin'):
            return response(headers={'scnt': 'scnt-secret', 'X-Apple-ID-Session-Id': 'identity-secret'})
        assert kwargs.get('headers', {}).get('scnt') == 'scnt-secret'
        if path.endswith('/signin/init'):
            verifier = srp.Verifier('demo@example.com', salt, verification_key,
                base64.b64decode(kwargs['json']['a']), hash_alg=srp.SHA256, ng_type=srp.NG_2048)
            _, public_b = verifier.get_challenge()
            return response(body={'salt': base64.b64encode(salt).decode(),
                'b': base64.b64encode(public_b).decode(), 'iteration': 1000,
                'protocol': protocol, 'c': 'challenge-id'})
        if path.endswith('/signin/complete'):
            payload = kwargs['json']
            proof = verifier.verify_session(base64.b64decode(payload['m1']))
            assert proof == base64.b64decode(payload['m2'])
            assert verifier.authenticated()
            assert 'password' not in payload
            return response(409, headers={'X-Apple-Session-Token': 'session-secret'})
        return response(body={'phoneNumberVerification': {'trustedPhoneNumbers': [
            {'id': 1, 'numberWithDialCode': '+86 13812345678'}]}})

    monkeypatch.setattr(requests.Session, 'request', send)
    client = al.AppleLogin('demo@example.com', 'primary-password')
    assert client.start() is False
    assert client.phones == [{'id': '1', 'number': '•••• 78'}]
    assert client.tokens['session_token'] == 'session-secret'
    assert len(calls) == 4
    client.close()


@pytest.fixture
def manager(tmp_path, monkeypatch):
    monkeypatch.setattr(am, 'ACCOUNTS_FILE', tmp_path / 'accounts.json')
    monkeypatch.setattr(am, 'OLD_COOKIES_FILE', tmp_path / 'old.json')
    manager = am.AccountManager()
    manager.accounts = {'a': {'id': 'a', 'cookies': {'old': 'cookie'}, 'host': 'icloud.com',
        'real_email': 'demo@example.com', 'name': 'existing', 'status': 'active',
        'credential_generation': 'old-generation', 'app_password': 'imap-secret',
        'mail_status': 'ok', 'mail_sync_paused': True, 'alias_total': 125}}
    return manager


@pytest.fixture
def fake_login(monkeypatch):
    class FakeLogin:
        ready = False
        starts = 0
        closed = False
        phones = [{'id': '1', 'number': '•••• 78'}]
        data = session_data()

        def __init__(self, username, password, host='icloud.com', state=None):
            self.username, self.password, self.host = username, password, host

        def start(self):
            type(self).starts += 1
            return self.ready

        def verify(self, code, method):
            if code != '123456':
                raise al.AppleLoginError('验证码无效或已过期')
            return True

        def send_sms(self, phone_id):
            pass

        def reset_deadline(self):
            pass

        def close(self):
            self.closed = True
            self.password = ''

        def web_state(self):
            return {'version': 1, 'tokens': {'session_token': 'new-session-secret'}, 'cookies': [
                {'name': 'new', 'value': 'new-cookie-secret', 'domain': '.icloud.com', 'path': '/', 'secure': True}]}

        def export_state(self):
            return copy.deepcopy(self.web_state())

    monkeypatch.setattr(al, 'AppleLogin', FakeLogin)
    return FakeLogin


def begin(manager, password='primary-secret'):
    return manager.begin_password_login('demo@example.com', password, acc_id='a')


def test_mfa_saves_encrypted_password_only_after_verification_and_preserves_imap(manager, fake_login, monkeypatch):
    result = begin(manager)
    assert result['requires_code'] and 'apple_login' not in manager.accounts['a']
    result = manager.password_login_action(result['login_id'], 'verify', '123456')
    assert result['id'] == 'a' and not result['requires_code']
    account = manager.get_account('a')
    assert account['app_password'] == 'imap-secret' and account['mail_sync_paused'] is True
    assert account['alias_total'] == 125
    stored = am.ACCOUNTS_FILE.read_text()
    for secret in ('primary-secret', 'new-session-secret', 'new-cookie-secret', 'imap-secret'):
        assert secret not in stored
    restored = am.AccountManager().get_account('a')
    assert restored['apple_login']['password'] == 'primary-secret'
    import web_ui
    monkeypatch.setattr(web_ui, '_account_mgr', manager)
    body = web_ui.app.test_client().get('/api/accounts').get_data(as_text=True)
    assert 'primary-secret' not in body and 'new-session-secret' not in body
    assert '"has_password_login":true' in body


@pytest.mark.parametrize('action', ['reimport', 'delete', 'forget'])
def test_stale_challenge_cannot_restore_removed_or_replaced_credentials(manager, fake_login, action):
    token = begin(manager)['login_id']
    if action == 'delete':
        manager.remove_account('a')
    elif action == 'forget':
        manager.forget_password_login('a')
    else:
        manager.accounts['a']['credential_generation'] = 'new-generation'
    with pytest.raises(al.AppleLoginError):
        manager.password_login_action(token, 'verify', '123456')
    assert 'a' not in manager.accounts or 'apple_login' not in manager.accounts['a']


def test_failed_password_storage_restores_previous_account(manager, fake_login, monkeypatch):
    token = begin(manager)['login_id']
    before = manager.get_account('a')
    monkeypatch.setattr(manager, '_save', lambda: (_ for _ in ()).throw(OSError('disk full')))
    with pytest.raises(OSError):
        manager.password_login_action(token, 'verify', '123456')
    assert manager.get_account('a') == before


def test_code_limits_expiry_cancel_and_login_cooldown(manager, fake_login):
    token = begin(manager)['login_id']
    with pytest.raises(al.AppleLoginError, match='60'):
        begin(manager)
    for _ in range(5):
        with pytest.raises(al.AppleLoginError):
            manager.password_login_action(token, 'verify', '000000')
    with pytest.raises(al.AppleLoginError, match='次数'):
        manager.password_login_action(token, 'verify', '123456')
    task = manager._apple_login_tasks[token]
    task['expires'] = time.time() - 1
    with pytest.raises(al.AppleLoginError, match='过期'):
        manager.password_login_action(token, 'verify', '123456')
    assert task['client'].closed


def test_sms_repeated_sends_are_limited(manager, fake_login):
    token = begin(manager)['login_id']
    assert manager.password_login_action(token, 'sms', phone_id='1')['ok']
    with pytest.raises(al.AppleLoginError, match='60'):
        manager.password_login_action(token, 'sms', phone_id='1')
    assert manager.password_login_action(token, 'cancel')['ok']
    assert token not in manager._apple_login_tasks


def enable_saved_login(manager):
    manager.accounts['a']['apple_login'] = {'username': 'demo@example.com', 'password': 'primary-secret', 'state': {}}


def test_auto_recovery_challenge_does_not_repeatedly_sign_in_after_restart(manager, fake_login):
    enable_saved_login(manager)
    assert manager._auto_apple_login(manager.accounts['a'], 'old-generation') is None
    assert manager.accounts['a']['apple_login_needs_code']
    assert manager.password_login_status('a')['requires_code']
    restored = am.AccountManager()
    assert restored._auto_apple_login(restored.accounts['a'], 'old-generation') is None
    assert fake_login.starts == 1
    # Explicit login can restart the expired challenge with the saved password.
    assert restored.begin_password_login('demo@example.com', '', acc_id='a')['requires_code']


def test_auto_recovery_returns_new_session_once_and_preserves_mail_state(manager, fake_login, monkeypatch):
    enable_saved_login(manager)
    fake_login.ready = True
    client = manager.get_client('a')
    assert client.session.headers['User-Agent'] == al.USER_AGENT
    replies = iter([response(421), response(body=session_data())])
    monkeypatch.setattr(client.session, 'request', lambda *a, **k: next(replies))
    client.validate_session()
    assert fake_login.starts == 1
    assert manager.accounts['a']['cookies'] == {'new': 'new-cookie-secret'}
    assert manager.accounts['a']['mail_sync_paused'] is True
    assert manager._auto_apple_login(manager.accounts['a'], 'old-generation') is None


def test_bad_saved_password_pauses_auto_attempts_and_hides_upstream_errors(manager, fake_login, monkeypatch):
    enable_saved_login(manager)
    monkeypatch.setattr(fake_login, 'start', lambda self: (_ for _ in ()).throw(al.AppleLoginError('Apple 拒绝登录')))
    assert manager._auto_apple_login(manager.accounts['a'], 'old-generation') is None
    assert manager.accounts['a']['apple_login_auto_paused']
    assert manager.accounts['a']['apple_login_error'] == 'Apple 拒绝登录'


def test_web_cookie_updates_keep_idms_state_and_roll_back_password_on_disk_failure(manager, fake_login, monkeypatch):
    enable_saved_login(manager)
    manager.accounts['a']['apple_login']['state'] = {'tokens': {'session_id': 'idms-secret'}, 'cookies': [
        {'name': 'idms', 'value': 'auth-cookie', 'domain': 'idmsa.apple.com'}]}
    client = manager.get_client('a')
    client.session.cookies.set('rotated', 'cookie-secret', domain='.icloud.com')
    client._capture_session(response(headers={'X-Apple-Session-Token': 'new-token-secret'}))
    login = manager.get_account('a')['apple_login']
    assert login['state']['tokens']['session_id'] == 'idms-secret'
    assert login['state']['tokens']['session_token'] == 'new-token-secret'
    assert any(c['name'] == 'idms' for c in login['state']['cookies'])
    monkeypatch.setattr(manager, '_save', lambda: (_ for _ in ()).throw(OSError('disk full')))
    client.session.cookies.set('rotated', 'failed-write', domain='.icloud.com')
    with pytest.raises(WebSessionStorageError):
        client._capture_session(response())
    assert manager.get_account('a')['apple_login'] == login


def test_password_login_routes_require_admin_and_same_origin_json(manager, fake_login, monkeypatch):
    import web_ui
    monkeypatch.setattr(web_ui, '_account_mgr', manager)
    client = web_ui.app.test_client()
    payload = {'username': 'demo@example.com', 'password': 'primary-secret', 'account_id': 'a'}
    assert client.post('/api/password-login/start', json=payload, headers={'Origin': 'https://evil.test'}).status_code == 403
    assert client.post('/api/password-login/start', data=payload).status_code == 403
    proxy = client.post('/api/password-login/start', json=payload,
        headers={'Host': 'mail.fangao.fun', 'Origin': 'https://mail.fangao.fun', 'X-Forwarded-Proto': 'https'},
        environ_base={'REMOTE_ADDR': '127.0.0.1'})
    assert proxy.status_code == 200 and proxy.json['requires_code']
    monkeypatch.setattr(web_ui, 'ADMIN_ACCESS_TOKEN', 'admin-test-token')
    assert client.post('/api/password-login/start', json=payload).status_code == 404


def test_public_https_origin_works_when_waitress_strips_forwarded_proto(manager, monkeypatch):
    import web_ui
    monkeypatch.setattr(web_ui, '_account_mgr', manager)
    monkeypatch.setattr(web_ui, 'PICKUP_BASE_URL', 'https://mail.fangao.fun')
    client = web_ui.app.test_client()
    result = client.post('/api/password-login/start', json={'username': ''},
        headers={'Host': 'mail.fangao.fun', 'Origin': 'https://mail.fangao.fun'})
    assert result.status_code == 400
    rejected = client.post('/api/password-login/start', json={'username': ''},
        headers={'Host': 'mail.fangao.fun', 'Origin': 'http://mail.fangao.fun'})
    assert rejected.status_code == 403


@pytest.mark.parametrize('field,value', [('password', ['bad']), ('username', ['bad']), ('password', 'x' * 1025), ('host', 'evil.test')])
def test_password_login_rejects_malformed_inputs_without_network(manager, fake_login, monkeypatch, field, value):
    import web_ui
    monkeypatch.setattr(web_ui, '_account_mgr', manager)
    data = {'username': 'demo@example.com', 'password': 'primary-secret'}
    data[field] = value
    assert web_ui.app.test_client().post('/api/password-login/start', json=data).status_code == 400
    assert fake_login.starts == 0


def test_verify_accepts_only_explicit_valid_409_and_never_logs_upstream(monkeypatch):
    client = al.AppleLogin('demo@example.com', 'primary-secret')
    client.tokens['session_token'] = 'session-secret'
    replies = iter([response(409, {'securityCode': {'valid': True}}), response(), response(body=session_data())])
    monkeypatch.setattr(client.session, 'request', lambda *a, **k: next(replies))
    assert client.verify('123456')
    monkeypatch.setattr(client.session, 'request', lambda *a, **k: response(401, {'password': 'primary-secret', 'trustTokens': ['private-token']}))
    with pytest.raises(al.AppleLoginError) as caught:
        client._request('POST', client.auth_url + '/signin/init')
    assert 'primary-secret' not in str(caught.value) and 'private-token' not in str(caught.value)
    client.close()


def test_auth_cookie_scope_survives_restart_without_leaking_into_hme():
    client = al.AppleLogin('demo@example.com', 'password', state={'cookies': [
        {'name': 'myacinfo', 'value': 'apple-secret', 'domain': '.apple.com'},
        {'name': 'web', 'value': 'web-secret', 'domain': '.icloud.com'},
        {'name': 'foreign', 'value': 'foreign-secret', 'domain': '.evil.test'},
        {'name': 'china', 'value': 'china-secret', 'domain': '.icloud.com.cn'}]})
    assert {c['name'] for c in client.export_state()['cookies']} == {'myacinfo', 'web'}
    assert {c['name'] for c in client.web_state()['cookies']} == {'web'}
    merged = al.merge_web_session(client.export_state(), {'cookies': [], 'tokens': {}})
    assert [c['name'] for c in merged['cookies']] == ['myacinfo']
    client.close()


def test_untrusted_account_login_still_requires_user_verification(monkeypatch):
    client = al.AppleLogin('demo@example.com', 'password')
    client.tokens['session_token'] = 'session-secret'
    data = session_data()
    data['dsInfo']['hsaVersion'] = 2
    data['hsaTrustedBrowser'] = False
    monkeypatch.setattr(client.session, 'request', lambda *a, **k: response(body=data))
    assert client._account_login() is False
    client.close()


def test_primary_password_recovery_does_not_replay_mutations(manager, fake_login, monkeypatch):
    enable_saved_login(manager)
    fake_login.ready = True
    client = manager.get_client('a')
    monkeypatch.setattr(client.session, 'request', lambda *a, **k: response(421))
    with pytest.raises(RuntimeError):
        client._request('POST', 'https://maildomainws.icloud.com/v2/hme/generate')
    assert fake_login.starts == 0


def test_manual_cookie_reimport_removes_saved_primary_password(manager, fake_login, monkeypatch):
    enable_saved_login(manager)
    monkeypatch.setattr(requests.Session, 'request', lambda *a, **k: response(body=session_data()))
    manager.reimport_account('a', 'token=manual-cookie')
    assert 'apple_login' not in manager.get_account('a')
    assert manager.get_account('a')['app_password'] == 'imap-secret'


def test_creation_in_progress_blocks_password_login_before_apple_request(manager, fake_login):
    manager.set_creation_activity_checker(lambda acc_id: acc_id == 'a')
    with pytest.raises(al.AppleLoginError, match='创建任务'):
        begin(manager)
    assert fake_login.starts == 0


def test_new_account_is_committed_only_after_success_and_duplicate_challenge_cannot_overwrite(manager, fake_login):
    manager.accounts.clear()
    token = manager.begin_password_login('demo@example.com', 'primary-secret')['login_id']
    manager._apple_login_recent['demo@example.com'] -= 61
    second = manager.begin_password_login('demo@example.com', 'older-secret')['login_id']
    assert not manager.accounts
    result = manager.password_login_action(token, 'verify', '123456')
    with pytest.raises(al.AppleLoginError, match='另一请求'):
        manager.password_login_action(second, 'verify', '123456')
    assert manager.get_account(result['id'])['apple_login']['password'] == 'primary-secret'
