"""Session renewal, durable credential rotation, and mutation replay protection."""
import json
import time
import threading
from urllib.parse import urlparse

import pytest
import requests

import account_manager as am
from icloud_hme import ICloudHME, WebSessionExpired, WebSessionStorageError


def response(status=200, body=None, headers=None):
    result = requests.Response()
    result.status_code = status
    result._content = json.dumps(body if body is not None else {}).encode()
    result.headers.update(headers or {})
    result.url = 'https://setup.icloud.com/setup/ws/1/validate'
    return result


def valid_session():
    return {'webservices': {'premiummailsettings': {'url': 'https://maildomainws.icloud.com'}},
            'dsInfo': {'appleId': 'demo@example.com'}}


@pytest.fixture
def manager(tmp_path, monkeypatch):
    monkeypatch.setattr(am, 'ACCOUNTS_FILE', tmp_path / 'accounts.json')
    monkeypatch.setattr(am, 'OLD_COOKIES_FILE', tmp_path / 'old.json')
    mgr = am.AccountManager()
    mgr.accounts = {'a': {'id': 'a', 'cookies': {'X-APPLE-WEBAUTH-TOKEN': 'old-cookie'},
                          'real_email': 'demo@example.com', 'status': 'active',
                          'credential_generation': 'generation-1'}}
    return mgr


def test_cookie_rotation_is_encrypted_and_survives_restart(manager, monkeypatch):
    client = manager.get_client('a')

    def send(*args, **kwargs):
        client.session.cookies.set('X-APPLE-WEBAUTH-TOKEN', 'rotated-cookie-secret',
                                   domain='.icloud.com', secure=True,
                                   expires=int(time.time()) + 3600)
        return response(body=valid_session(), headers={
            'X-Apple-Session-Token': 'renewal-token-secret',
            'X-Apple-TwoSV-Trust-Token': 'trust-token-secret'})

    monkeypatch.setattr(client.session, 'request', send)
    client.validate_session()
    assert len(list(client.session.cookies)) == 1
    text = am.ACCOUNTS_FILE.read_text()
    for secret in ('rotated-cookie-secret', 'renewal-token-secret', 'trust-token-secret'):
        assert secret not in text
    assert 'web_session' not in json.loads(text)['accounts']['a']
    loaded = am.AccountManager()
    restored = loaded.get_client('a')
    assert restored.session.cookies.get('X-APPLE-WEBAUTH-TOKEN') == 'rotated-cookie-secret'
    assert restored._session_tokens['session_token'] == 'renewal-token-secret'
    assert restored.export_session()['cookies'][0]['domain'] == '.icloud.com'
    assert restored.export_session()['cookies'][0]['secure'] is True


def test_account_api_does_not_expose_session_secrets(manager, monkeypatch):
    import web_ui
    manager.accounts['a']['web_session'] = {'tokens': {'session_token': 'api-secret'}, 'cookies': []}
    monkeypatch.setattr(web_ui, '_account_mgr', manager)
    result = web_ui.app.test_client().get('/api/accounts')
    assert result.status_code == 200
    assert 'api-secret' not in result.get_data(as_text=True)
    assert 'web_session' not in result.json['accounts'][0]


def test_expired_cookie_does_not_fall_back_to_original_import():
    client = ICloudHME({'token': 'old'}, verbose=False)
    client.bind_session({'cookies': [dict(name='token', value='expired', domain='.icloud.com',
                                        path='/', secure=True, expires=1)]})
    assert list(client.session.cookies) == []


def test_deletion_cookie_removes_unscoped_import():
    client = ICloudHME({'token': 'old'}, verbose=False)
    client.bind_session()
    client._capture_session(response(headers={
        'Set-Cookie': 'token=; Domain=.icloud.com; Path=/; Max-Age=0'}))
    assert list(client.session.cookies) == []


def test_scoped_cookie_metadata_is_preserved_without_cross_region_restore():
    client = ICloudHME({}, verbose=False)
    client.bind_session({'cookies': [
        dict(name='regional', value='cn', domain='.icloud.com.cn'),
        dict(name='scoped', value='value', domain='setup.icloud.com', path='/setup', secure=True)]})
    jar = list(client.session.cookies)
    assert len(jar) == 1
    assert (jar[0].domain, jar[0].path, jar[0].secure) == ('setup.icloud.com', '/setup', True)


@pytest.mark.parametrize('change', ['reimport', 'delete'])
def test_stale_client_cannot_overwrite_new_credentials(manager, change):
    client = manager.get_client('a')
    if change == 'delete':
        manager.accounts.pop('a')
    else:
        manager.accounts['a'].update(cookies={'token': 'new-import'},
                                     credential_generation='generation-2')
    client.session.cookies.set('token', 'stale-write', domain='.icloud.com')
    client._capture_session(response())
    if change == 'delete':
        assert 'a' not in manager.accounts
    else:
        assert manager.accounts['a']['cookies'] == {'token': 'new-import'}


def test_failed_persistence_rolls_back_in_memory_credentials(manager, monkeypatch):
    client = manager.get_client('a')
    client.session.cookies.set('token', 'new', domain='.icloud.com')
    monkeypatch.setattr(manager, '_save', lambda: (_ for _ in ()).throw(OSError('disk full')))
    with pytest.raises(WebSessionStorageError):
        client._capture_session(response())
    assert manager.accounts['a']['cookies'] == {'X-APPLE-WEBAUTH-TOKEN': 'old-cookie'}
    assert 'web_session' not in manager.accounts['a']


def test_renewal_then_validate_retries_once_and_keeps_new_tokens(monkeypatch):
    client = ICloudHME({}, verbose=False)
    calls = []
    responses = [response(421, {'trustTokens': ['never-expose-this']},
                          {'X-Apple-Session-Token': 'session-token',
                           'X-Apple-TwoSV-Trust-Token': 'trust-token'}),
                 response(body=valid_session()), response(body=valid_session())]

    def send(method, url, **kwargs):
        calls.append((method, urlparse(url).path, kwargs.get('data')))
        return responses.pop(0)

    monkeypatch.setattr(client.session, 'request', send)
    assert client.validate_session()['dsInfo']['appleId'] == 'demo@example.com'
    assert [item[1] for item in calls] == ['/setup/ws/1/validate', '/setup/ws/1/accountLogin',
                                         '/setup/ws/1/validate']
    assert json.loads(calls[1][2]) == {'dsWebAuthToken': 'session-token',
                                      'extended_login': True, 'trustToken': 'trust-token'}


@pytest.mark.parametrize('challenge', [False, True])
def test_failed_or_challenged_renewal_stops_without_token_leaks(monkeypatch, challenge):
    client = ICloudHME({}, verbose=False)
    client.bind_session({'tokens': {'session_token': 'session-secret'}})
    calls = []

    def send(method, url, **kwargs):
        path = urlparse(url).path
        calls.append(path)
        if path.endswith('/accountLogin') and challenge:
            return response(body=dict(valid_session(), hsaChallengeRequired=True))
        return response(421, {'trustTokens': ['response-secret']})

    monkeypatch.setattr(client.session, 'request', send)
    for _ in range(2):
        with pytest.raises(WebSessionExpired) as error:
            client.validate_session()
        assert 'secret' not in str(error.value)
        assert '重新导入' in str(error.value)
    assert calls.count('/setup/ws/1/accountLogin') == 1
    assert calls.count('/setup/ws/1/validate') == 2


@pytest.mark.parametrize('operation', ['reserve', 'delete', 'deactivate'])
def test_mutating_request_is_never_automatically_replayed(monkeypatch, operation):
    client = ICloudHME({}, verbose=False)
    client.bind_session({'tokens': {'session_token': 'session-secret'}})
    calls = []
    monkeypatch.setattr(client.session, 'request', lambda *args, **kwargs:
                        calls.append(args) or response(421))
    with pytest.raises(WebSessionExpired):
        client._request('POST', 'https://maildomainws.icloud.com/v1/hme/' + operation)
    assert len(calls) == 1


def test_storage_failure_is_local_not_apple_auth():
    from create_guard import classify
    assert classify(WebSessionStorageError('网页登录凭据保存失败，请检查存储')) == 'local'


def test_legacy_error_is_redacted_on_load(manager):
    manager.accounts['a']['last_error'] = 'HTTP 421: {"trustTokens":["old-response-secret"]}'
    manager._save()
    loaded = am.AccountManager()
    assert 'old-response-secret' not in loaded.accounts['a']['last_error']
    assert '重新导入' in loaded.accounts['a']['last_error']


def test_read_request_can_resume_after_renewal(monkeypatch):
    client = ICloudHME({}, verbose=False)
    client.bind_session({'tokens': {'session_token': 'stored-token'}})
    calls = []
    replies = [response(421), response(body=valid_session()), response(body={'result': 'ok'})]
    monkeypatch.setattr(client.session, 'request', lambda *args, **kwargs:
                        calls.append(args) or replies.pop(0))
    assert client._request('GET', 'https://maildomainws.icloud.com/v2/hme/list') == {'result': 'ok'}
    assert [args[0] for args in calls] == ['GET', 'POST', 'GET']


def test_network_failure_during_renewal_is_not_marked_auth(monkeypatch):
    from create_guard import classify
    client = ICloudHME({}, verbose=False)
    client.bind_session({'tokens': {'session_token': 'stored-token'}})

    def send(method, url, **kwargs):
        if urlparse(url).path.endswith('/accountLogin'):
            raise requests.exceptions.Timeout('private upstream details')
        return response(421)

    monkeypatch.setattr(client.session, 'request', send)
    with pytest.raises(RuntimeError) as error:
        client.validate_session()
    assert classify(error.value) == 'network'
    assert 'private' not in str(error.value)


def test_reimport_replaces_old_session_and_preserves_imap(manager, monkeypatch):
    manager.accounts['a'].update(web_session={'tokens': {'session_token': 'old-session'}},
                                  app_password='imap-password', mail_status='ok')

    def send(session, method, url, **kwargs):
        session.cookies.set('token', 'rotated-import', domain='.icloud.com')
        return response(body=valid_session() if urlparse(url).path.endswith('/validate') else {},
                        headers={'X-Apple-Session-Token': 'new-session'})

    monkeypatch.setattr(requests.Session, 'request', send)
    account = manager.reimport_account('a', 'token=new-import')
    assert account['web_session']['tokens']['session_token'] == 'new-session'
    assert account['cookies']['token'] == 'rotated-import'
    assert account['app_password'] == 'imap-password'
    assert account['mail_status'] == 'ok'
    assert account['credential_generation'] != 'generation-1'


def test_duplicate_add_uses_rotated_credentials_for_second_validation(manager, monkeypatch):
    validates = []

    def send(session, method, url, **kwargs):
        if urlparse(url).path.endswith('/validate'):
            validates.append(session.cookies.get('token'))
            session.cookies.set('token', 'fresh-import', domain='.icloud.com')
            return response(body=valid_session(), headers={'X-Apple-Session-Token': 'fresh-session'})
        return response()

    monkeypatch.setattr(requests.Session, 'request', send)
    result = manager.add_account('updated', 'token=original-import')
    assert result['id'] == 'a'
    assert validates == ['original-import', 'fresh-import']
    assert result['web_session']['tokens']['session_token'] == 'fresh-session'


def test_alias_mail_lookup_serializes_web_session_requests(manager, monkeypatch):
    started, entered = threading.Event(), threading.Event()

    class Client:
        def list_aliases(self):
            entered.set()
            return []

        def close(self):
            pass

    monkeypatch.setattr(manager, 'get_client', lambda *args, **kwargs: Client())
    results = []

    def lookup():
        started.set()
        results.append(manager.check_all_aliases_mail('a', force=True))

    with manager._operation_lock('a'):
        thread = threading.Thread(target=lookup)
        thread.start()
        assert started.wait(2)
        assert not entered.wait(0.1)
    thread.join(2)
    assert not thread.is_alive()
    assert entered.is_set()
    assert results == [{}]
