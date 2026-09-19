"""Fixed outbound proxy configuration. Never returns credentials to the UI."""
import imaplib
import json
import os
import socket
import ssl
import threading
from pathlib import Path
from urllib.parse import quote

CONFIG_FILE = Path(__file__).resolve().parent / 'results' / 'network_proxy.json'
LOCK = threading.RLock()
DEFAULT = dict(mode='system', protocol='http', host='', port=8080, username='', password='')


def load_config():
    with LOCK:
        if not CONFIG_FILE.exists():
            return dict(DEFAULT)
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding='utf-8'))
            return validate(data, dict(DEFAULT))
        except Exception:
            raise RuntimeError('代理配置无法读取，已停止连接，请检查配置') from None


def validate(data, previous=None):
    if not isinstance(data, dict):
        raise ValueError('代理配置格式错误')
    result = dict(previous or DEFAULT)
    for key in ('mode', 'protocol', 'host', 'port', 'username'):
        if key in data:
            result[key] = data[key]
    if data.get('clear_password'):
        result['password'] = ''
    elif data.get('password'):
        result['password'] = data['password']
    if result['mode'] not in ('system', 'direct', 'proxy'):
        raise ValueError('请选择有效的网络方式')
    if result['protocol'] not in ('http', 'socks5h'):
        raise ValueError('支持 HTTP 和 SOCKS5 代理')
    if not isinstance(result['host'], str):
        raise ValueError('代理主机格式错误')
    result['host'] = result['host'].strip()
    if any(c in result['host'] for c in '/@?#\\ \r\n\t'):
        raise ValueError('主机只填 IP 或域名，不要填写协议、端口或用户名')
    if result['mode'] == 'proxy' and not result['host']:
        raise ValueError('请填写代理主机')
    if type(result['port']) is not int or not 1 <= result['port'] <= 65535:
        raise ValueError('端口必须为 1 到 65535 的整数')
    for key in ('username', 'password'):
        if not isinstance(result[key], str) or len(result[key]) > 1024 or any(c in result[key] for c in '\r\n\x00'):
            raise ValueError('代理认证信息格式错误')
    return result


def public_config(config=None):
    config = config or load_config()
    return {**{k: config[k] for k in ('mode', 'protocol', 'host', 'port', 'username')},
            'password_set': bool(config['password'])}


def save_config(data):
    with LOCK:
        config = validate(data, load_config())
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = CONFIG_FILE.with_suffix('.tmp')
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(config, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, CONFIG_FILE)
        os.chmod(CONFIG_FILE, 0o600)
        return public_config(config)


def proxy_url(config):
    auth = ''
    if config['username'] or config['password']:
        auth = quote(config['username'], safe='') + ':' + quote(config['password'], safe='') + '@'
    host = config['host']
    if ':' in host and not host.startswith('['):
        host = '[' + host + ']'
    return f"{config['protocol']}://{auth}{host}:{config['port']}"


def configure_session(session, config):
    if config['mode'] != 'system':
        session.trust_env = False
    if config['mode'] == 'proxy':
        url = proxy_url(config)
        session.proxies = {'http': url, 'https': url}
    elif config['mode'] == 'direct':
        session.proxies = {}


class ProxyIMAP(imaplib.IMAP4_SSL):
    def __init__(self, host, port, config, timeout):
        self.proxy_config = config
        super().__init__(host, port, ssl_context=ssl.create_default_context(), timeout=timeout)

    def _create_socket(self, timeout):
        import socks
        config = self.proxy_config
        sock = socks.socksocket()
        try:
            sock.set_proxy(socks.HTTP if config['protocol'] == 'http' else socks.SOCKS5,
                           config['host'].strip('[]'), config['port'], rdns=True,
                           username=config['username'] or None, password=config['password'] or None)
            sock.settimeout(timeout)
            sock.connect((self.host, self.port))
            return self.ssl_context.wrap_socket(sock, server_hostname=self.host)
        except Exception:
            sock.close()
            raise RuntimeError('代理 IMAP 连接失败，请检查代理、认证和 993 端口支持') from None


def connect_imap(host, port, timeout, config):
    if config['mode'] == 'proxy':
        return ProxyIMAP(host, port, config, timeout)
    return imaplib.IMAP4_SSL(host, port, timeout=timeout)


def probe(config):
    import requests
    results = {}
    session = requests.Session()
    configure_session(session, config)
    try:
        response = session.get('https://www.icloud.com/', timeout=10, allow_redirects=False, stream=True)
        results['https'] = {'ok': True, 'status': response.status_code}
        response.close()
    except Exception:
        results['https'] = {'ok': False, 'error': 'HTTPS 连接失败，请检查代理和认证'}
    finally:
        session.close()
    conn = None
    try:
        conn = connect_imap('imap.mail.me.com', 993, 10, config)
        results['imap'] = {'ok': True}
    except Exception:
        results['imap'] = {'ok': False, 'error': 'IMAP 连接失败，代理可能不支持 CONNECT 到 993 端口'}
    finally:
        if conn:
            try:
                conn.logout()
            except Exception:
                pass
    return results
