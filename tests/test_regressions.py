#!/usr/bin/env python3
"""回归测试 — 覆盖核心流程，发现重构中的破坏性变更。"""

import sys
import json
import os
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))


def test_parse_cookie_header_string():
    """Cookie Header String 格式解析"""
    from account_manager import AccountManager
    mgr = AccountManager()
    
    raw = "X_APPLE_WEB_KB=abc123; SESSION_TOKEN=xyz789"
    cookies = mgr.parse_cookie_input(raw)
    assert len(cookies) == 2
    assert cookies["X_APPLE_WEB_KB"] == "abc123"
    assert cookies["SESSION_TOKEN"] == "xyz789"
    print("  PASS test_parse_cookie_header_string")


def test_parse_cookie_json():
    """JSON 格式 Cookie 解析"""
    from account_manager import AccountManager
    mgr = AccountManager()
    
    raw = '{"X_APPLE_WEB_KB":"abc123","SESSION_TOKEN":"xyz789"}'
    cookies = mgr.parse_cookie_input(raw)
    assert len(cookies) == 2
    assert cookies["X_APPLE_WEB_KB"] == "abc123"
    print("  PASS test_parse_cookie_json")


def test_parse_empty_input():
    """空输入应抛出 ValueError"""
    from account_manager import AccountManager
    mgr = AccountManager()
    
    try:
        mgr.parse_cookie_input("")
        assert False, "应该抛出 ValueError"
    except ValueError:
        pass
    print("  PASS test_parse_empty_input")


def test_derive_icloud_email_primary():
    """dsInfo 有 primaryEmail 时直接使用"""
    from account_manager import AccountManager
    info = {"appleId": "user@qq.com", "primaryEmail": "user@icloud.com"}
    result = AccountManager._derive_icloud_email(info)
    assert result == "user@icloud.com"
    print("  PASS test_derive_icloud_email_primary")


def test_derive_icloud_email_appleid_is_icloud():
    """appleId 本身是 @icloud.com"""
    from account_manager import AccountManager
    info = {"appleId": "user@icloud.com", "primaryEmail": ""}
    result = AccountManager._derive_icloud_email(info)
    assert result == "user@icloud.com"
    print("  PASS test_derive_icloud_email_appleid_is_icloud")


def test_derive_icloud_email_third_party():
    """第三方 Apple ID 不能被猜成同名前缀的 iCloud 邮箱"""
    from account_manager import AccountManager
    info = {"appleId": "test@gmail.com", "primaryEmail": ""}
    result = AccountManager._derive_icloud_email(info)
    assert result == ""
    print("  PASS test_derive_icloud_email_third_party")


def test_mail_cache_basic():
    """邮件缓存基本读写"""
    import mail_cache
    old_path = mail_cache.CACHE_FILE
    temp_dir = tempfile.TemporaryDirectory()
    mail_cache.CACHE_FILE = Path(temp_dir.name) / "mail_cache.json"
    cache = mail_cache.MailCache()
    
    emails = [
        {"id": "1", "from": "a@b.com", "to": "x@icloud.com", "subject": "Hello", "date": "2025-01-01T00:00:00"},
        {"id": "2", "from": "c@d.com", "to": "y@icloud.com", "subject": "World", "date": "2025-01-02T00:00:00"},
        {"id": "1", "from": "a@b.com", "to": "x@icloud.com", "subject": "Hello Duplicate", "date": "2025-01-03T00:00:00"},
    ]
    
    cache.set_inbox("test_acc", emails)
    cached = cache.get_inbox("test_acc")
    
    # 应该有 2 封（第 3 封 id 重复被去重）
    assert len(cached) == 2, f"期望 2 封，实际 {len(cached)}"
    
    # 清理
    cache.clear_account("test_acc")
    assert len(cache.get_inbox("test_acc")) == 0
    mail_cache.CACHE_FILE = old_path
    temp_dir.cleanup()
    print("  PASS test_mail_cache_basic")


def test_pickup_rebind_deduplicates_and_revokes():
    """旧账号迁移不得留下无法撤销的重复 token"""
    from pickup_links import PickupLinkStore
    with tempfile.TemporaryDirectory() as directory:
        store = PickupLinkStore(Path(directory) / "pickup_links.json")
        old = store.ensure("old", "same@icloud.com")
        current = store.ensure("current", "same@icloud.com")
        store.rebind_stale_accounts(["current"])
        assert len(store.list_for_account("current")) == 1
        assert store.revoke("current", "same@icloud.com")
        assert store.get_by_token(old["token"]) is None
        assert store.get_by_token(current["token"]) is None
    print("  PASS test_pickup_rebind_deduplicates_and_revokes")


def test_account_api_reports_validation_failure():
    """账号底层状态为 error 时 API 不能返回 ok=true"""
    import web_ui
    client = web_ui.app.test_client()

    class FakeManager:
        def add_account(self, *_args, **_kwargs):
            return {"id": "x", "name": "bad", "status": "error", "last_error": "expired"}

        def validate_account(self, *_args, **_kwargs):
            return {"id": "x", "status": "error", "last_error": "expired"}

    original = web_ui._account_mgr
    web_ui._account_mgr = FakeManager()
    try:
        added = client.post("/api/accounts/add", json={"name": "bad", "cookie_input": "a=b"})
        validated = client.post("/api/accounts/x/validate")
        assert added.status_code == 400 and added.get_json()["ok"] is False
        assert validated.status_code == 400 and validated.get_json()["ok"] is False
    finally:
        web_ui._account_mgr = original
    print("  PASS test_account_api_reports_validation_failure")


def test_scheduler_start_is_idempotent():
    """重复启动只能保留一个调度线程，停止不得触发全局关机事件"""
    import web_ui
    client = web_ui.app.test_client()
    first_result = client.post("/api/scheduler/start").get_json()
    first_thread = web_ui._scheduler_thread
    second_result = client.post("/api/scheduler/start").get_json()
    second_thread = web_ui._scheduler_thread
    try:
        assert first_result["already_running"] is False
        assert second_result["already_running"] is True
        assert first_thread is second_thread and first_thread.is_alive()
    finally:
        client.post("/api/scheduler/stop")
        first_thread.join(timeout=2)
    assert not first_thread.is_alive()
    assert not web_ui._shutdown_event.is_set()
    print("  PASS test_scheduler_start_is_idempotent")


def test_strip_html():
    """HTML 标签剥离"""
    from icloud_mail import _strip_html
    
    html = "<html><body><p>Hello</p><br><div>World</div></body></html>"
    text = _strip_html(html)
    assert "Hello" in text
    assert "World" in text
    assert "<p>" not in text
    assert "<html>" not in text
    print("  PASS test_strip_html")


def test_strip_html_with_link():
    """HTML 链接保留文字"""
    from icloud_mail import _strip_html
    
    html = '<a href="https://example.com">Click here</a>'
    text = _strip_html(html)
    assert "Click here" in text
    assert "example.com" in text
    print("  PASS test_strip_html_with_link")


def test_icloud_hme_account_info():
    """ICloudHME 客户端有 get_account_info 方法"""
    from icloud_hme import ICloudHME
    client = ICloudHME({}, verbose=False)
    assert hasattr(client, "get_account_info")
    # 未校验前应返回 None
    assert client.get_account_info() is None
    print("  PASS test_icloud_hme_account_info")


if __name__ == "__main__":
    tests = [
        ("parse_cookie_header_string", test_parse_cookie_header_string),
        ("parse_cookie_json", test_parse_cookie_json),
        ("parse_empty_input", test_parse_empty_input),
        ("derive_icloud_email_primary", test_derive_icloud_email_primary),
        ("derive_icloud_email_appleid_is_icloud", test_derive_icloud_email_appleid_is_icloud),
        ("derive_icloud_email_third_party", test_derive_icloud_email_third_party),
        ("mail_cache_basic", test_mail_cache_basic),
        ("pickup_rebind_deduplicates_and_revokes", test_pickup_rebind_deduplicates_and_revokes),
        ("account_api_reports_validation_failure", test_account_api_reports_validation_failure),
        ("scheduler_start_is_idempotent", test_scheduler_start_is_idempotent),
        ("strip_html", test_strip_html),
        ("strip_html_with_link", test_strip_html_with_link),
        ("icloud_hme_account_info", test_icloud_hme_account_info),
    ]
    
    passed = 0
    failed = 0
    
    for name, fn in tests:
        try:
            fn()
            passed += 1
        except Exception as e:
            print(f"  FAIL {name}: {e}")
            failed += 1
    
    print(f"\n{'='*40}")
    print(f"结果: {passed} 通过, {failed} 失败")
    
    if failed:
        sys.exit(1)
