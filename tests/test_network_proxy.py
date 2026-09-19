import json
import pytest
import requests
import network_proxy as proxy


@pytest.fixture
def config_path(tmp_path, monkeypatch):
    monkeypatch.setattr(proxy, 'CONFIG_FILE', tmp_path / 'proxy.json')
    return proxy.CONFIG_FILE


def fixed(**kwargs):
    return {**proxy.DEFAULT, 'mode': 'proxy', 'host': 'proxy.example', 'username': 'user@name', 'password': 'secret:/@', **kwargs}


def test_config_preserves_password_without_echo(config_path):
    public = proxy.save_config(fixed())
    assert 'password' not in public and public['password_set']
    proxy.save_config({'port': 1234, 'password': ''})
    assert proxy.load_config()['password'] == 'secret:/@'
    assert proxy.load_config()['port'] == 1234
    proxy.save_config({'clear_password': True})
    assert proxy.load_config()['password'] == ''


@pytest.mark.parametrize('change', [{'host': 'http://proxy.example'}, {'host': 'a\r\nb'}, {'port': 0}, {'port': True}, {'mode': 'unknown'}])
def test_invalid_config_rejected(config_path, change):
    with pytest.raises(ValueError):
        proxy.save_config(fixed(**change))
    assert not config_path.exists()


def test_corrupt_config_fails_closed(config_path):
    config_path.write_text('not json')
    with pytest.raises(RuntimeError):
        proxy.load_config()


def test_proxy_overrides_environment_and_encodes_credentials(config_path):
    session = requests.Session()
    proxy.configure_session(session, fixed(protocol='socks5h'))
    assert not session.trust_env
    assert session.proxies['https'] == 'socks5h://user%40name:secret%3A%2F%40@proxy.example:8080'
    assert session.proxies['http'] == session.proxies['https']


def test_hme_uses_fixed_proxy_and_sanitizes_failure(config_path, monkeypatch):
    from icloud_hme import ICloudHME
    proxy.save_config(fixed())
    client = ICloudHME({}, verbose=False)
    calls = []
    def fail(*args, **kwargs):
        calls.append(1)
        raise requests.exceptions.ProxyError('secret:/@')
    monkeypatch.setattr(client.session, 'request', fail)
    with pytest.raises(RuntimeError) as exc:
        client._request('GET', 'https://www.icloud.com/')
    assert 'secret' not in str(exc.value)
    assert calls == [1]
    assert client.session.proxies['https'].startswith('http://')


def test_imap_uses_proxy_and_keeps_tls_hostname(config_path, monkeypatch):
    import socks
    calls = []
    class FakeSocket:
        def set_proxy(self, *a, **kw): calls.append(('proxy', a, kw))
        def settimeout(self, timeout): calls.append(('timeout', timeout))
        def connect(self, target): calls.append(('connect', target))
        def close(self): calls.append(('closed',))
    class FakeTLS:
        def wrap_socket(self, sock, server_hostname):
            calls.append(('tls', server_hostname)); return sock
    monkeypatch.setattr(socks, 'socksocket', FakeSocket)
    conn = object.__new__(proxy.ProxyIMAP)
    conn.proxy_config = fixed(protocol='socks5h')
    conn.host = 'imap.mail.me.com'; conn.port = 993; conn.ssl_context = FakeTLS()
    conn._create_socket(10)
    assert calls[0][1][0] == socks.SOCKS5 and calls[0][2]['rdns']
    assert ('connect', ('imap.mail.me.com', 993)) in calls
    assert ('tls', 'imap.mail.me.com') in calls


def test_mail_reconnects_after_config_change(config_path, monkeypatch):
    from icloud_mail import ICloudMail
    class Conn:
        state = 'SELECTED'
        closed = False
        def logout(self): self.closed = True
        def login(self, *args): pass
    old, new = Conn(), Conn()
    mail = ICloudMail('test@icloud.com', 'not-real')
    mail._conn = old
    proxy.save_config(fixed())
    seen = []
    def connect(host, port, timeout, config):
        seen.append(config['mode']); return new
    monkeypatch.setattr(proxy, 'connect_imap', connect)
    mail._ensure_connected()
    assert old.closed and mail._conn is new and seen == ['proxy']


def test_admin_api_never_returns_secret_or_saves_test_config(config_path, monkeypatch):
    import web_ui
    from create_guard import CreationGuard
    from types import SimpleNamespace
    monkeypatch.setattr(web_ui, '_account_mgr', SimpleNamespace(creation_guard=CreationGuard(config_path.parent / 'guard.json')))
    monkeypatch.setattr(proxy, 'probe', lambda config: {'https': {'ok': True, 'status': 200}, 'imap': {'ok': True}})
    client = web_ui.app.test_client()
    response = client.post('/api/network-proxy', json={**fixed(), 'action': 'test'})
    assert response.json['ok'] and not config_path.exists()
    response = client.post('/api/network-proxy', json={**fixed(), 'action': 'save'})
    assert response.json['ok']
    assert 'secret' not in response.get_data(as_text=True)
    assert 'secret' not in client.get('/api/network-proxy').get_data(as_text=True)
    web_ui._account_mgr.creation_guard.active.add('busy')
    assert client.post('/api/network-proxy', json={'action': 'save', 'mode': 'direct'}).status_code == 409
    assert proxy.load_config()['mode'] == 'proxy'
