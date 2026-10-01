"""Regression coverage for the September 12 audit fixes; no real network."""
import pytest


def test_short_valid_mail_is_preserved():
    from icloud_mail import ICloudMail
    raw = b'From: a@example.com\r\nTo: b@icloud.com\r\nSubject: OTP\r\n\r\n123456'
    assert ICloudMail._extract_body([(b'1 (BODY[] {60}', raw), b')']) == raw


def test_search_failure_raises(monkeypatch):
    from icloud_mail import ICloudMail
    mail = ICloudMail('demo@icloud.com', 'fake')
    class Connection:
        def uid(self, *args):
            return 'NO', [b'Temporarily unavailable']
    mail._conn = Connection()
    monkeypatch.setattr(mail, '_ensure_connected', lambda: None)
    with pytest.raises(RuntimeError, match="SEARCH"):
        mail.recent_uids()


def test_missing_body_is_not_cached(tmp_path, monkeypatch):
    import account_manager as am
    import mail_cache
    monkeypatch.setattr(am, 'ACCOUNTS_FILE', tmp_path / 'accounts.json')
    monkeypatch.setattr(am, 'OLD_COOKIES_FILE', tmp_path / 'old.json')
    monkeypatch.setattr(mail_cache, 'CACHE_FILE', tmp_path / 'cache.json')
    mgr = am.AccountManager()
    mgr._cache = mail_cache.MailCache()
    mgr.accounts = {'a': {'id': 'a', 'status': 'active'}}
    class Mail:
        def recent_uids(self, **kwargs): return [b'1']
        def fetch_header(self, uid):
            return {'id': '1', 'recipients': ['demo@icloud.com'], 'subject': 'OTP'}
        def fetch_full(self, uid): return None
    mgr._mail_clients['a'] = Mail()
    result = mgr.sync_pickup_mail('a', ['demo@icloud.com'])
    assert result['bodies'] == {}
    assert result['messages']['demo@icloud.com'][0]['id'] == '1' 


def test_local_write_failure_does_not_return_success(tmp_path, monkeypatch):
    import account_manager as am
    import icloud_hme
    monkeypatch.setattr(am, 'ACCOUNTS_FILE', tmp_path / 'accounts.json')
    monkeypatch.setattr(am, 'OLD_COOKIES_FILE', tmp_path / 'old.json')
    monkeypatch.setattr(am, 'LATEST_EMAILS', tmp_path / 'missing' / 'emails.txt')
    mgr = am.AccountManager()
    mgr.accounts = {'a': {'id': 'a', 'status': 'active', 'cookies': {}}}
    def create(client, **kwargs):
        client.before_reserve('demo@icloud.com')
        return {'email': 'demo@icloud.com'}
    monkeypatch.setattr(icloud_hme.ICloudHME, 'create_alias', create)
    results = mgr.create_aliases_for_account('a')
    assert [r['ok'] for r in results] == [False]
    assert mgr.creation_guard.snapshot()['accounts']['a']['pending'] == 'demo@icloud.com'
    assert not am.LATEST_EMAILS.exists()


def test_corrupt_account_file_fails_closed(tmp_path, monkeypatch):
    import account_manager as am
    path = tmp_path / 'accounts.json'
    path.write_text('{broken', encoding='utf-8')
    monkeypatch.setattr(am, 'ACCOUNTS_FILE', path)
    monkeypatch.setattr(am, 'OLD_COOKIES_FILE', tmp_path / 'old.json')
    with pytest.raises(RuntimeError):
        am.AccountManager()
    assert path.read_text(encoding='utf-8') == '{broken' 


def test_multiple_alias_recipients_match_all():
    from account_manager import AccountManager
    aliases = {'a@icloud.com', 'b@icloud.com'}
    result = AccountManager._match_aliases({'recipients': list(aliases)}, aliases)
    assert set(result) == aliases


def test_export_write_failure_rolls_back(tmp_path, monkeypatch):
    from export_history import ExportHistoryStore
    store = ExportHistoryStore(tmp_path / 'exports.json')
    import durable_json
    original = durable_json.write_object
    def fail(*args): raise OSError('simulated disk failure')
    monkeypatch.setattr(durable_json, 'write_object', fail)
    with pytest.raises(OSError):
        store.claim([{'email': 'demo@icloud.com', 'account_id': 'a'}])
    assert store.get('demo@icloud.com') is None
    monkeypatch.setattr(durable_json, 'write_object', original)
    claimed, skipped = store.claim([{'email': 'demo@icloud.com', 'account_id': 'a'}])
    assert len(claimed) == 1 and skipped == []
