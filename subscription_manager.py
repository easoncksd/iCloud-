"""Manage the optional Mihomo subscription URL without exposing its token."""
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit


SUBSCRIPTION_FILE = Path(os.environ.get(
    'ICLOUD_PROXY_SUBSCRIPTION_FILE', '/root/icloud-proxy/subscription.url'))
META_FILE = SUBSCRIPTION_FILE.with_name('subscription.meta.json')
LOCK = threading.RLock()


def _validate_url(value):
    if not isinstance(value, str):
        raise ValueError('订阅地址格式错误')
    value = value.strip()
    parts = urlsplit(value)
    if parts.scheme.lower() != 'https' or not parts.netloc:
        raise ValueError('订阅地址必须使用 HTTPS')
    if any(c in value for c in '\r\n\x00') or len(value) > 4096:
        raise ValueError('订阅地址格式错误或过长')
    return value


def _write_private(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        os.chmod(path, 0o600)
    finally:
        if tmp.exists():
            tmp.unlink()


def _public(value):
    if not value:
        return {'configured': False, 'host': '', 'saved_at': ''}
    parts = urlsplit(value)
    return {'configured': True, 'host': parts.hostname or '',
            'saved_at': _saved_at()}


def _saved_at():
    try:
        data = json.loads(META_FILE.read_text(encoding='utf-8'))
        return str(data.get('saved_at', ''))
    except Exception:
        return ''


def status():
    with LOCK:
        try:
            value = SUBSCRIPTION_FILE.read_text(encoding='utf-8').strip() if SUBSCRIPTION_FILE.exists() else ''
        except OSError:
            raise RuntimeError('订阅配置无法读取') from None
        return _public(value)


def save(value):
    value = _validate_url(value)
    with LOCK:
        _write_private(SUBSCRIPTION_FILE, value + '\n')
        now = datetime.now(timezone.utc).isoformat(timespec='seconds')
        _write_private(META_FILE, json.dumps({'saved_at': now}, ensure_ascii=False))
        return _public(value)


def clear():
    with LOCK:
        for path in (SUBSCRIPTION_FILE, META_FILE):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        return _public('')
