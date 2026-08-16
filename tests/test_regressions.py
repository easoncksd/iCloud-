#!/usr/bin/env python3
"""回归测试 — 覆盖核心流程，发现重构中的破坏性变更。"""

import sys
import json
import os
import tempfile
import time
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


def test_export_history_is_persistent_and_idempotent():
    """导出状态必须持久化，并且同一邮箱不得重复领取"""
    from export_history import ExportHistoryStore
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "export_history.json"
        store = ExportHistoryStore(path)
        claimed, skipped = store.claim([
            {"email": "Alias@icloud.com", "account_id": "a1"},
            {"email": "alias@icloud.com", "account_id": "a1"},
        ])
        assert len(claimed) == 1 and skipped == []

        reloaded = ExportHistoryStore(path)
        claimed, skipped = reloaded.claim([
            {"email": "ALIAS@icloud.com", "account_id": "a2"},
        ])
        assert claimed == [] and skipped == ["alias@icloud.com"]
        assert reloaded.get("alias@icloud.com")["account_id"] == "a1"

        assert reloaded.restore(["alias@icloud.com"]) == ["alias@icloud.com"]
        claimed, skipped = reloaded.claim([
            {"email": "alias@icloud.com", "account_id": "a2"},
        ])
        assert len(claimed) == 1 and skipped == []
        assert claimed[0]["account_id"] == "a2"
    print("  PASS test_export_history_is_persistent_and_idempotent")


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


def test_export_api_prevents_duplicate_downloads():
    """导出接口必须原子防重，恢复后才能再次导出"""
    import web_ui
    from export_history import ExportHistoryStore

    assert "refreshEmails().then(renderAliasTable)" in web_ui.UI_HTML
    assert "#aliasTableContainer{overflow-x:auto}" in web_ui.UI_HTML

    class FakePickupStore:
        def list_all(self):
            return [{
                "alias_email": "one@icloud.com",
                "account_id": "a1",
                "token": "opaque-token",
            }]

    original_pickup = web_ui._pickup_store
    original_export = web_ui._export_store
    original_base = web_ui.PICKUP_BASE_URL
    with tempfile.TemporaryDirectory() as directory:
        web_ui._pickup_store = FakePickupStore()
        web_ui._export_store = ExportHistoryStore(Path(directory) / "exports.json")
        web_ui.PICKUP_BASE_URL = "https://mail.example.test"
        client = web_ui.app.test_client()
        try:
            first = client.post("/api/pickup-links/export", json={"emails": ["one@icloud.com"]}).get_json()
            second = client.post("/api/pickup-links/export", json={"emails": ["ONE@icloud.com"]}).get_json()
            assert first["ok"] and first["count"] == 1
            assert first["lines"] == ["one@icloud.com----https://mail.example.test/pickup/opaque-token"]
            assert second["ok"] and second["count"] == 0
            assert second["skipped"] == ["one@icloud.com"]

            restored = client.post(
                "/api/export-history/restore", json={"emails": ["one@icloud.com"]}
            ).get_json()
            third = client.post("/api/pickup-links/export", json={"emails": ["one@icloud.com"]}).get_json()
            assert restored["ok"] and restored["count"] == 1
            assert third["ok"] and third["count"] == 1
        finally:
            web_ui._pickup_store = original_pickup
            web_ui._export_store = original_export
            web_ui.PICKUP_BASE_URL = original_base
    print("  PASS test_export_api_prevents_duplicate_downloads")


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


def test_create_alias_stops_retrying_on_address_limit():
    """Apple 明确返回地址上限时不得反复重试"""
    from icloud_hme import ICloudHME

    client = ICloudHME({}, verbose=False)
    attempts = []
    client.generate = lambda: "generated@icloud.com"

    def limited_reserve(*_args, **_kwargs):
        attempts.append(1)
        raise RuntimeError("You have reached the limit of addresses you can create right now.")

    client.reserve = limited_reserve
    try:
        client.create_alias(max_retries=5)
        raise AssertionError("地址上限应该抛出异常")
    except RuntimeError as exc:
        assert "reached the limit" in str(exc)
    assert len(attempts) == 1
    print("  PASS test_create_alias_stops_retrying_on_address_limit")


def test_async_batch_skips_limited_account_and_continues():
    """某个主账号限额后应自动继续下一个账号"""
    import web_ui

    class FakeManager:
        accounts = {
            "limited": {"id": "limited", "name": "limited", "status": "active"},
            "working": {"id": "working", "name": "working", "status": "active"},
        }

        def get_account(self, account_id):
            return self.accounts.get(account_id)

        def create_aliases_for_account(self, account_id, count, _label):
            if account_id == "limited":
                return [{"ok": False, "limited": True, "error": "address limit"}]
            return [
                {"ok": True, "email": f"ok-{index}@icloud.com", "account_id": account_id}
                for index in range(count)
            ]

    original_manager = web_ui._account_mgr
    with web_ui._batch_lock:
        original_jobs = web_ui._batch_jobs
        original_active = web_ui._batch_active_id
        web_ui._batch_jobs = web_ui.OrderedDict()
        web_ui._batch_active_id = None
    web_ui._account_mgr = FakeManager()
    client = web_ui.app.test_client()
    try:
        started = client.post("/api/create-batch", json={
            "account_ids": ["limited", "working"],
            "count_per_account": 2,
            "interval": 0,
        })
        assert started.status_code == 202
        job_id = started.get_json()["job_id"]
        deadline = time.time() + 3
        while time.time() < deadline:
            job = client.get(f"/api/create-batch/{job_id}").get_json()["job"]
            if job["status"] not in ("queued", "running"):
                break
            time.sleep(0.02)
        assert job["status"] == "completed"
        assert job["total_created"] == 2
        assert job["accounts"]["limited"]["status"] == "limited"
        assert job["accounts"]["working"]["status"] == "completed"
        assert "create-batch-current" in web_ui.UI_HTML
        assert 'max="750"' in web_ui.UI_HTML
    finally:
        web_ui._account_mgr = original_manager
        with web_ui._batch_lock:
            web_ui._batch_jobs = original_jobs
            web_ui._batch_active_id = original_active
    print("  PASS test_async_batch_skips_limited_account_and_continues")


def test_async_batch_retries_temporary_limit_after_cooldown():
    """临时频率限制应等待后继续创建剩余数量。"""
    import web_ui

    class FakeManager:
        calls = 0

        def get_account(self, _account_id):
            return {"id": "temporary", "name": "temporary", "status": "active"}

        def create_aliases_for_account(self, account_id, count, _label):
            self.calls += 1
            if self.calls == 1:
                return [{
                    "ok": False,
                    "limited": True,
                    "error": "You have reached the limit of addresses you can create right now.",
                }]
            return [
                {"ok": True, "email": f"ok-{index}@icloud.com", "account_id": account_id}
                for index in range(count)
            ]

    original_manager = web_ui._account_mgr
    original_delay = web_ui._BATCH_RETRY_DELAY_SECONDS
    with web_ui._batch_lock:
        original_jobs = web_ui._batch_jobs
        original_active = web_ui._batch_active_id
        web_ui._batch_jobs = web_ui.OrderedDict()
        web_ui._batch_active_id = None
    web_ui._account_mgr = FakeManager()
    web_ui._BATCH_RETRY_DELAY_SECONDS = 0.01
    client = web_ui.app.test_client()
    try:
        started = client.post("/api/create-batch", json={
            "account_ids": ["temporary"],
            "count_per_account": 2,
            "interval": 0,
        })
        assert started.status_code == 202
        job_id = started.get_json()["job_id"]
        deadline = time.time() + 3
        while time.time() < deadline:
            job = client.get(f"/api/create-batch/{job_id}").get_json()["job"]
            if job["status"] not in ("queued", "running"):
                break
            time.sleep(0.01)
        assert job["status"] == "completed"
        assert job["total_created"] == 2
        assert job["total_errors"] == 0
        assert job["accounts"]["temporary"]["retry_count"] == 1
        assert job["accounts"]["temporary"]["status"] == "completed"
        assert web_ui._account_mgr.calls == 2
        assert "暂停 30 分钟" in web_ui.UI_HTML
    finally:
        web_ui._account_mgr = original_manager
        web_ui._BATCH_RETRY_DELAY_SECONDS = original_delay
        with web_ui._batch_lock:
            web_ui._batch_jobs = original_jobs
            web_ui._batch_active_id = original_active
    print("  PASS test_async_batch_retries_temporary_limit_after_cooldown")


def test_email_api_uses_saved_and_pickup_creation_times():
    """新记录读取保存时间，旧记录使用取件链接时间兜底。"""
    import web_ui

    class FakePickupStore:
        def list_all(self):
            return [{
                "account_id": "acc-current",
                "alias_email": "old@icloud.com",
                "token": "existing-old-token",
                "created_at": "2026-08-01T02:03:04+00:00",
            }]

        def ensure(self, account_id, alias_email):
            return {
                "account_id": account_id,
                "alias_email": alias_email,
                "token": f"token-{account_id}",
            }

    class FakeExportStore:
        def status_map(self, _emails):
            return {}

    class FakeAccountManager:
        accounts = {"acc-current": {}, "acc-new": {}}

    original_results = web_ui.RESULTS_DIR
    original_pickup = web_ui._pickup_store
    original_export = web_ui._export_store
    original_manager = web_ui._account_mgr
    with tempfile.TemporaryDirectory() as td:
        results_dir = Path(td)
        (results_dir / "latest_emails.txt").write_text(
            "old@icloud.com\tacc-old\n"
            "new@icloud.com\tacc-new\t2026-08-16T12:30:00+08:00\n",
            encoding="utf-8",
        )
        web_ui.RESULTS_DIR = results_dir
        web_ui._pickup_store = FakePickupStore()
        web_ui._export_store = FakeExportStore()
        web_ui._account_mgr = FakeAccountManager()
        try:
            payload = web_ui.app.test_client().get("/api/emails").get_json()
            by_email = {item["email"]: item for item in payload["emails"]}
            assert by_email["new@icloud.com"]["created_at"] == "2026-08-16T12:30:00+08:00"
            assert by_email["old@icloud.com"]["created_at"] == "2026-08-01T02:03:04+00:00"
            assert by_email["new@icloud.com"]["pickup_url"].endswith("/pickup/token-acc-new")
            assert by_email["old@icloud.com"]["pickup_url"].endswith("/pickup/existing-old-token")
            assert by_email["old@icloud.com"]["account_id"] == "acc-current"
            assert "<th>创建时间</th>" in web_ui.UI_HTML
            assert "formatExportTime(e.created_at)" in web_ui.UI_HTML
            assert "pickupLinksLoaded=true" in web_ui.UI_HTML
        finally:
            web_ui.RESULTS_DIR = original_results
            web_ui._pickup_store = original_pickup
            web_ui._export_store = original_export
            web_ui._account_mgr = original_manager
    print("  PASS test_email_api_uses_saved_and_pickup_creation_times")


def test_pickup_links_api_does_not_call_icloud():
    """普通查看取件链接只能读取本地存储，不能隐式请求 Apple。"""
    import web_ui

    class OfflineManager:
        def get_all_aliases(self):
            raise AssertionError("不应访问 iCloud")

    class FakePickupStore:
        def list_all(self):
            return [{
                "account_id": "acc-1",
                "alias_email": "one@icloud.com",
                "token": "local-token",
                "created_at": "2026-08-16T10:00:00+00:00",
            }]

    original_manager = web_ui._account_mgr
    original_pickup = web_ui._pickup_store
    web_ui._account_mgr = OfflineManager()
    web_ui._pickup_store = FakePickupStore()
    try:
        payload = web_ui.app.test_client().get("/api/pickup-links").get_json()
        assert payload["count"] == 1
        assert payload["links"][0]["url"].endswith("/pickup/local-token")
    finally:
        web_ui._account_mgr = original_manager
        web_ui._pickup_store = original_pickup
    print("  PASS test_pickup_links_api_does_not_call_icloud")


def test_runtime_logs_persist_replay_and_resume():
    """运行日志应落盘，SSE 首次连接回放历史，前端按序号续接。"""
    import web_ui

    original_file = web_ui._RUNTIME_LOG_FILE
    original_entries = list(web_ui._log_entries)
    original_seq = web_ui._log_seq
    with tempfile.TemporaryDirectory() as td:
        web_ui._RUNTIME_LOG_FILE = Path(td) / "runtime.jsonl"
        with web_ui._log_condition:
            web_ui._log_entries = web_ui.deque(maxlen=1000)
            web_ui._log_seq = 0
        try:
            web_ui._emit_log("info", "日志回放测试")
            loaded = web_ui._load_runtime_logs(web_ui._RUNTIME_LOG_FILE)
            assert len(loaded) == 1
            assert loaded[0]["msg"] == "日志回放测试"
            assert len(loaded[0]["time"]) == 19

            response = web_ui.app.test_client().get(
                "/api/log-stream?after=0", buffered=False
            )
            chunk = next(response.response).decode("utf-8")
            response.close()
            assert "id: 1" in chunk
            assert "日志回放测试" in chunk
            assert "?after='+logCursor" in web_ui.UI_HTML
            assert "entry.seq||0)<=logCursor" in web_ui.UI_HTML
        finally:
            web_ui._RUNTIME_LOG_FILE = original_file
            with web_ui._log_condition:
                web_ui._log_entries = web_ui.deque(original_entries, maxlen=1000)
                web_ui._log_seq = original_seq
    print("  PASS test_runtime_logs_persist_replay_and_resume")


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
        ("export_history_is_persistent_and_idempotent", test_export_history_is_persistent_and_idempotent),
        ("account_api_reports_validation_failure", test_account_api_reports_validation_failure),
        ("export_api_prevents_duplicate_downloads", test_export_api_prevents_duplicate_downloads),
        ("scheduler_start_is_idempotent", test_scheduler_start_is_idempotent),
        ("strip_html", test_strip_html),
        ("strip_html_with_link", test_strip_html_with_link),
        ("icloud_hme_account_info", test_icloud_hme_account_info),
        ("create_alias_stops_retrying_on_address_limit", test_create_alias_stops_retrying_on_address_limit),
        ("async_batch_skips_limited_account_and_continues", test_async_batch_skips_limited_account_and_continues),
        ("async_batch_retries_temporary_limit_after_cooldown", test_async_batch_retries_temporary_limit_after_cooldown),
        ("email_api_uses_saved_and_pickup_creation_times", test_email_api_uses_saved_and_pickup_creation_times),
        ("pickup_links_api_does_not_call_icloud", test_pickup_links_api_does_not_call_icloud),
        ("runtime_logs_persist_replay_and_resume", test_runtime_logs_persist_replay_and_resume),
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
