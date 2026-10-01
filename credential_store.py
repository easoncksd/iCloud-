"""Authenticated, account-bound encryption of credentials at rest.

The independent 0600 key must be included in backups. This protects a copied
accounts.json, not an attacker with access to the running service or its key.
"""
import base64
import json
import os
from pathlib import Path
from Crypto.Cipher import AES


def _key(path, create=False):
    path = Path(path)
    if create and not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, 'wb') as handle:
                handle.write(os.urandom(32))
                handle.flush()
                os.fsync(handle.fileno())
    try:
        data = path.read_bytes()
        if len(data) != 32:
            raise ValueError()
        return data
    except (OSError, ValueError):
        raise RuntimeError('账号凭据密钥缺失或损坏，请从完整备份恢复') from None


def seal(account_id, credentials, path):
    cipher = AES.new(_key(path, create=True), AES.MODE_GCM)
    cipher.update(str(account_id).encode())
    encrypted, tag = cipher.encrypt_and_digest(json.dumps(credentials).encode())
    return {'version': 1, 'data': base64.b64encode(cipher.nonce + tag + encrypted).decode()}


def unseal(account_id, envelope, path):
    try:
        if envelope.get('version') != 1:
            raise ValueError()
        raw = base64.b64decode(envelope['data'], validate=True)
        cipher = AES.new(_key(path), AES.MODE_GCM, nonce=raw[:16])
        cipher.update(str(account_id).encode())
        credentials = json.loads(cipher.decrypt_and_verify(raw[32:], raw[16:32]))
        if not isinstance(credentials, dict):
            raise ValueError()
        return credentials
    except (KeyError, TypeError, ValueError):
        raise RuntimeError('账号凭据校验失败，请从完整备份恢复') from None
