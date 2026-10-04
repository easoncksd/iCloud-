"""Focused backend regression coverage for the follow-up audit fixes."""

import json

import pytest


def test_network_error_classifier_covers_dns_ssl_and_reset():
    from create_guard import classify

    assert classify("Temporary failure in name resolution") == "network"
    assert classify("SSL: CERTIFICATE_VERIFY_FAILED") == "network"
    assert classify("Connection reset by peer") == "network"


def test_creation_guard_normalizes_defaults_and_bounds_journal(tmp_path):
    from create_guard import CreationGuard

    path = tmp_path / "guard.json"
    tasks = {f"task-{i}": {"a": 1} for i in range(1100)}
    tasks["active-task"] = {"a": 3}
    path.write_text(json.dumps({
        "accounts": {"a": {"pending": "candidate@icloud.com", "pending_task": "active-task"}},
        "task_successes": tasks,
    }), encoding="utf-8")
    guard = CreationGuard(path)
    snapshot = guard.snapshot()
    assert snapshot["settings"] == {"concurrency": 3, "daily_limit": 50}
    assert len(snapshot["task_successes"]) <= guard._MAX_TASK_SUCCESS_JOURNAL
    assert snapshot["task_successes"]["active-task"]["a"] == 3
    assert snapshot["accounts"]["a"]["attempts"] == 0


def test_creation_guard_rejects_invalid_schema(tmp_path):
    from create_guard import CreationGuard

    path = tmp_path / "guard.json"
    path.write_text(json.dumps({"accounts": []}), encoding="utf-8")
    with pytest.raises(ValueError):
        CreationGuard(path)


def test_icloud_hme_close_releases_session():
    from icloud_hme import ICloudHME

    client = ICloudHME({}, verbose=False)
    session = client.session
    client.close()
    assert client.session is None
    # requests.Session.close() is idempotent and should not be called twice.
    client.close()
    assert session is not None


def test_recent_uids_without_limit_does_not_drop_older_messages(monkeypatch):
    from icloud_mail import ICloudMail

    mail = ICloudMail("demo@icloud.com", "fake", verbose=False)

    class Connection:
        state = "SELECTED"

        def uid(self, *_args):
            return "OK", [b"1 2 3 4"]

    mail._conn = Connection()
    monkeypatch.setattr(mail, "_ensure_connected", lambda: None)
    assert mail.recent_uids(limit=None) == [b"4", b"3", b"2", b"1"]
    assert mail.recent_uids(limit=2) == [b"4", b"3"]


def test_check_all_aliases_mail_returns_cache_when_scan_has_no_matches(tmp_path, monkeypatch):
    import account_manager as am
    import mail_cache

    monkeypatch.setattr(am, "ACCOUNTS_FILE", tmp_path / "accounts.json")
    monkeypatch.setattr(am, "OLD_COOKIES_FILE", tmp_path / "old.json")
    monkeypatch.setattr(mail_cache, "CACHE_FILE", tmp_path / "cache.json")
    manager = am.AccountManager()
    manager._cache = mail_cache.MailCache()
    manager.accounts = {"a": {"id": "a", "status": "active", "cookies": {}}}
    manager._cache.set_alias_mail("a", "alias@icloud.com", [{"id": "1", "date": "2026-01-01T00:00:00"}])

    class AliasClient:
        def list_aliases(self):
            return [{"email": "alias@icloud.com"}]

        def close(self):
            pass

    class EmptyMail:
        def check_inbox(self, **kwargs):
            return []

        def disconnect(self):
            pass

    monkeypatch.setattr(manager, "get_client", lambda *args, **kwargs: AliasClient())
    manager._mail_clients["a"] = EmptyMail()
    result = manager.check_all_aliases_mail("a", force=True)
    assert result["alias@icloud.com"][0]["id"] == "1"


def test_duplicate_account_add_is_rejected_when_checker_reports_active(tmp_path, monkeypatch):
    import account_manager as am
    import icloud_hme

    monkeypatch.setattr(am, "ACCOUNTS_FILE", tmp_path / "accounts.json")
    monkeypatch.setattr(am, "OLD_COOKIES_FILE", tmp_path / "old.json")

    class FakeHME:
        def __init__(self, *args, **kwargs):
            pass

        def validate_session(self):
            pass

        def get_account_info(self):
            return {"appleId": "same@icloud.com"}

        def list_aliases(self):
            return []

        def close(self):
            pass

    monkeypatch.setattr(icloud_hme, "ICloudHME", FakeHME)
    manager = am.AccountManager()
    manager.accounts = {"a": {"id": "a", "real_email": "same@icloud.com", "status": "active"}}
    manager.set_creation_activity_checker(lambda _acc_id: True)
    with pytest.raises(ValueError, match="创建任务"):
        manager.add_account("replacement", "a=b")
