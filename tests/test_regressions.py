#!/usr/bin/env python3
"""回归测试 — 覆盖核心流程，发现重构中的破坏性变更。"""

import sys
import json
import os
import tempfile
import threading
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



def test_validate_skips_lock_when_create_in_progress():
    """检查登录不能卡在正在创建的账号锁上。"""
    import web_ui

    original = web_ui._account_create_in_progress
    web_ui._account_create_in_progress = lambda _acc_id: True
    try:
        payload = web_ui.app.test_client().post("/api/accounts/busy-acc/validate").get_json()
        assert payload["ok"] is False
        assert payload["busy"] is True
        assert "正在创建邮箱" in payload["error"]
        assert "toast(t('status.checking_login'))" in web_ui.UI_HTML
    finally:
        web_ui._account_create_in_progress = original
    print("  PASS test_validate_skips_lock_when_create_in_progress")


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
    original_flag = web_ui._SCHEDULER_FLAG_FILE
    with tempfile.TemporaryDirectory() as td:
        web_ui._SCHEDULER_FLAG_FILE = Path(td) / "scheduler_enabled.json"
        client = web_ui.app.test_client()
        first_result = client.post("/api/scheduler/start").get_json()
        first_thread = web_ui._scheduler_thread
        second_result = client.post("/api/scheduler/start").get_json()
        second_thread = web_ui._scheduler_thread
        try:
            assert first_result["already_running"] is False
            assert second_result["already_running"] is True
            assert first_thread is second_thread and first_thread.is_alive()
            assert web_ui._load_scheduler_enabled() is True
        finally:
            client.post("/api/scheduler/stop")
            first_thread.join(timeout=2)
            web_ui._SCHEDULER_FLAG_FILE = original_flag
        assert not first_thread.is_alive()
        assert not web_ui._shutdown_event.is_set()
        assert web_ui._load_scheduler_enabled.__name__ == " _load_scheduler_enabled".strip()
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

        def create_aliases_for_account(self, account_id, count, _label, **_kwargs):
            if account_id == "limited":
                return [{"ok": False, "limited": True, "error": "address limit"}]
            return [
                {"ok": True, "email": f"ok-{index}@icloud.com", "account_id": account_id}
                for index in range(count)
            ]

    original_manager = web_ui._account_mgr
    original_state_file = web_ui._BATCH_STATE_FILE
    temp_dir = tempfile.TemporaryDirectory()
    web_ui._BATCH_STATE_FILE = Path(temp_dir.name) / "batch_jobs.json"
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
        web_ui._BATCH_STATE_FILE = original_state_file
        temp_dir.cleanup()
    print("  PASS test_async_batch_skips_limited_account_and_continues")


def test_async_batch_retries_temporary_limit_after_cooldown():
    """临时频率限制应等待后继续创建剩余数量。"""
    import web_ui

    class FakeManager:
        calls = 0

        def get_account(self, _account_id):
            return {"id": "temporary", "name": "temporary", "status": "active"}

        def create_aliases_for_account(self, account_id, count, _label, **_kwargs):
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
    original_state_file = web_ui._BATCH_STATE_FILE
    temp_dir = tempfile.TemporaryDirectory()
    web_ui._BATCH_STATE_FILE = Path(temp_dir.name) / "batch_jobs.json"
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
        assert "等待 Apple 限制解除" in web_ui.UI_HTML
        assert "每次等待 1 分钟后自动续建" in web_ui.UI_HTML
    finally:
        web_ui._account_mgr = original_manager
        web_ui._BATCH_RETRY_DELAY_SECONDS = original_delay
        with web_ui._batch_lock:
            web_ui._batch_jobs = original_jobs
            web_ui._batch_active_id = original_active
        web_ui._BATCH_STATE_FILE = original_state_file
        temp_dir.cleanup()
    print("  PASS test_async_batch_retries_temporary_limit_after_cooldown")


def test_batch_retries_every_minute_after_repeated_limit():
    """首次限制短探测，连续限制后应按小时窗口等待。"""
    import web_ui

    class FakeManager:
        calls = 0

        def get_account(self, _account_id):
            return {"id": "paced", "name": "paced", "status": "active"}

        def create_aliases_for_account(self, account_id, count, _label, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                return [{
                    "ok": False, "limited": True, "retryable": True,
                    "error": "rate limit",
                }]
            if self.calls == 2:
                return [
                    {"ok": True, "email": "one@icloud.com", "account_id": account_id},
                    {"ok": False, "limited": True, "retryable": True,
                     "error": "rate limit"},
                ]
            return [
                {"ok": True, "email": "two@icloud.com", "account_id": account_id}
                for _ in range(count)
            ]

    original_manager = web_ui._account_mgr
    original_short = web_ui._BATCH_RETRY_DELAY_SECONDS
    original_state_file = web_ui._BATCH_STATE_FILE
    temp_dir = tempfile.TemporaryDirectory()
    with web_ui._batch_lock:
        original_jobs = web_ui._batch_jobs
        original_active = web_ui._batch_active_id
        web_ui._batch_jobs = web_ui.OrderedDict()
        web_ui._batch_active_id = None
    try:
        web_ui._account_mgr = FakeManager()
        web_ui._BATCH_RETRY_DELAY_SECONDS = 0.01
        web_ui._BATCH_STATE_FILE = Path(temp_dir.name) / "batch_jobs.json"
        client = web_ui.app.test_client()
        started = client.post("/api/create-batch", json={
            "account_ids": ["paced"], "count_per_account": 2, "interval": 0,
        })
        assert started.status_code == 202
        job_id = started.get_json()["job_id"]
        deadline = time.time() + 3
        while time.time() < deadline:
            job = client.get(f"/api/create-batch/{job_id}").get_json()["job"]
            if job["status"] not in ("queued", "running"):
                break
            time.sleep(0.01)
        entry = job["accounts"]["paced"]
        assert job["status"] == "completed"
        assert job["total_created"] == 2
        assert entry["retry_count"] == 2
        assert entry["retry_delay_seconds"] == 0.01
        assert web_ui._account_mgr.calls == 3
    finally:
        web_ui._account_mgr = original_manager
        web_ui._BATCH_RETRY_DELAY_SECONDS = original_short
        web_ui._BATCH_STATE_FILE = original_state_file
        with web_ui._batch_lock:
            web_ui._batch_jobs = original_jobs
            web_ui._batch_active_id = original_active
        temp_dir.cleanup()
    print("  PASS test_batch_retries_every_minute_after_repeated_limit")


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
            assert "t('table.created')" in web_ui.UI_HTML
            assert "创建时间" in web_ui.UI_HTML
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

            payload = web_ui.app.test_client().get("/api/logs?after=0").get_json()
            assert payload["ok"] is True
            assert payload["logs"][0]["msg"] == "日志回放测试"
            assert payload["seq"] == 1

            response = web_ui.app.test_client().get(
                "/api/log-stream?after=0", buffered=False
            )
            buf = ""
            for raw in response.response:
                buf += raw.decode("utf-8")
                if "日志回放测试" in buf:
                    break
            response.close()
            assert "id: 1" in buf
            assert "日志回放测试" in buf
            assert "/api/logs?after=" in web_ui.UI_HTML
            assert "await loadLogs()" in web_ui.UI_HTML
            assert "EventSource.CLOSED" in web_ui.UI_HTML
            assert "function appendLog(" in web_ui.UI_HTML
        finally:
            web_ui._RUNTIME_LOG_FILE = original_file
            with web_ui._log_condition:
                web_ui._log_entries = web_ui.deque(original_entries, maxlen=1000)
                web_ui._log_seq = original_seq
    print("  PASS test_runtime_logs_persist_replay_and_resume")


def test_batch_state_persists_and_resumes_remaining_count():
    """重启恢复时只能继续尚未创建的数量，不能重复创建。"""
    import web_ui

    class FakeManager:
        requested = []

        def get_account(self, _account_id):
            return {"id": "one", "name": "one", "status": "active"}

        def create_aliases_for_account(self, account_id, count, _label, **_kwargs):
            self.requested.append(count)
            return [
                {"ok": True, "email": f"new-{i}@icloud.com", "account_id": account_id}
                for i in range(count)
            ]

    original_manager = web_ui._account_mgr
    original_jobs = web_ui._batch_jobs
    original_active = web_ui._batch_active_id
    original_state_file = web_ui._BATCH_STATE_FILE
    with tempfile.TemporaryDirectory() as td:
        state_file = Path(td) / "batch_jobs.json"
        job = {
            "id": "resume-job",
            "status": "running",
            "account_ids": ["one"],
            "count_per_account": 5,
            "interval": 0,
            "label": "",
            "total_accounts": 1,
            "completed_accounts": 0,
            "total_created": 0,
            "total_errors": 0,
            "created_at": "2026-08-17T00:00:00+08:00",
            "updated_at": "2026-08-17T00:00:00+08:00",
            "accounts": {
                "one": {
                    "account_id": "one", "name": "one", "status": "waiting",
                    "created": 4, "errors": 0, "limited": False, "error": "",
                    "retry_count": 2, "retry_at": None,
                }
            },
        }
        try:
            web_ui._account_mgr = FakeManager()
            web_ui._BATCH_STATE_FILE = state_file
            web_ui._batch_jobs = web_ui.OrderedDict([("resume-job", job)])
            web_ui._batch_active_id = "resume-job"
            with web_ui._batch_lock:
                web_ui._save_batch_state_locked()
            loaded, active = web_ui._load_batch_state(state_file)
            assert active == "resume-job"
            assert loaded["resume-job"]["accounts"]["one"]["created"] == 4
            web_ui._run_batch_job("resume-job")
            assert web_ui._account_mgr.requested == [1]
            assert job["total_created"] == 5
            assert job["accounts"]["one"]["status"] == "completed"
        finally:
            web_ui._account_mgr = original_manager
            web_ui._batch_jobs = original_jobs
            web_ui._batch_active_id = original_active
            web_ui._BATCH_STATE_FILE = original_state_file
    print("  PASS test_batch_state_persists_and_resumes_remaining_count")


def test_invalid_imap_credentials_are_not_persisted():
    """新 IMAP 凭据测试失败时不能覆盖原有可用配置。"""
    import icloud_mail
    import web_ui

    class FakeManager:
        updated = False

        def get_account(self, _account_id):
            return {"id": "one", "icloud_email": "old@icloud.com"}

        def _drop_mail_client(self, _account_id):
            raise AssertionError("失败凭据不应替换连接")

        def update_account(self, _account_id, **_kwargs):
            self.updated = True

    class RejectingMail:
        def __init__(self, _email, _password):
            pass

        def test_connection(self):
            return {"ok": False, "error": "login failed"}

    original_manager = web_ui._account_mgr
    original_mail = icloud_mail.ICloudMail
    try:
        web_ui._account_mgr = FakeManager()
        icloud_mail.ICloudMail = RejectingMail
        response = web_ui.app.test_client().post(
            "/api/accounts/one/app-password",
            json={"icloud_email": "new@icloud.com", "app_password": "wrong"},
        )
        assert response.status_code == 400
        assert web_ui._account_mgr.updated is False
    finally:
        web_ui._account_mgr = original_manager
        icloud_mail.ICloudMail = original_mail
    print("  PASS test_invalid_imap_credentials_are_not_persisted")


def test_fetch_full_uses_single_imap_fetch():
    """完整邮件已经含有头部，正文读取不应再发第二次 IMAP 请求。"""
    from icloud_mail import ICloudMail

    raw = (
        b"From: Sender <sender@example.com>\r\n"
        b"To: alias@icloud.com\r\n"
        b"Subject: Single fetch\r\n"
        b"Date: Sun, 17 Aug 2026 00:00:00 +0800\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
        + b"x" * 600
    )

    class FakeConnection:
        state = "SELECTED"
        calls = 0

        def uid(self, command, _uid, _query):
            assert command == "FETCH"
            self.calls += 1
            return "OK", [(b"1 (UID 7 BODY[])", raw), b")"]

    mail = ICloudMail("user@icloud.com", "password")
    mail._conn = FakeConnection()
    message = mail.fetch_full(b"7")
    assert mail._conn.calls == 1
    assert message["subject"] == "Single fetch"
    assert message["recipients"] == ["alias@icloud.com"]
    print("  PASS test_fetch_full_uses_single_imap_fetch")


def test_manual_create_rejects_invalid_counts():
    import web_ui

    client = web_ui.app.test_client()
    for value in (0, 751, "bad"):
        response = client.post("/api/accounts/one/create", json={"count": value})
        assert response.status_code == 400
        response = client.post(
            "/api/create-batch",
            json={"account_ids": ["one"], "count_per_account": value},
        )
        assert response.status_code == 400
    assert "每次等待 1 分钟后自动续建" in web_ui.UI_HTML
    print("  PASS test_manual_create_rejects_invalid_counts")


def test_mail_body_store_persists_and_prunes():
    from mail_body_store import MailBodyStore

    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "bodies.sqlite3"
        store = MailBodyStore(path, max_items=2, max_bytes=1024 * 1024)
        store.put("acc", "1", {"body": "one"})
        store.put("acc", "2", {"body": "two"})
        store.put("acc", "3", {"body": "three"})
        assert store.get("acc", "1") is None
        assert store.get("acc", "3")["body"] == "three"
        assert store.stats()["count"] == 2
        store.close()

        reopened = MailBodyStore(path, max_items=2, max_bytes=1024 * 1024)
        assert reopened.get("acc", "2")["body"] == "two"
        assert reopened.get("acc", "3")["body"] == "three"
        reopened.close()
    print("  PASS test_mail_body_store_persists_and_prunes")


def test_account_creation_is_paced_between_aliases():
    import account_manager
    import icloud_hme

    class FakeHME:
        counter = 0

        def __init__(self, *_args, **_kwargs):
            pass

        def create_alias(self, **_kwargs):
            self.counter += 1
            return {"email": f"paced-{self.counter}@icloud.com"}

    originals = (
        account_manager.ACCOUNTS_FILE,
        account_manager.LATEST_EMAILS,
        account_manager.CREATE_ALIAS_INTERVAL_SECONDS,
        account_manager.CREATE_ALIAS_JITTER_SECONDS,
        account_manager.time.sleep,
        account_manager.random.uniform,
        icloud_hme.ICloudHME,
    )
    sleeps = []
    with tempfile.TemporaryDirectory() as td:
        try:
            account_manager.ACCOUNTS_FILE = Path(td) / "accounts.json"
            account_manager.LATEST_EMAILS = Path(td) / "latest_emails.txt"
            account_manager.CREATE_ALIAS_INTERVAL_SECONDS = 3
            account_manager.CREATE_ALIAS_JITTER_SECONDS = 2
            account_manager.time.sleep = lambda seconds: sleeps.append(seconds)
            account_manager.random.uniform = lambda _start, _end: 1
            icloud_hme.ICloudHME = FakeHME
            manager = account_manager.AccountManager()
            manager.accounts["acc"] = {
                "id": "acc", "name": "acc", "status": "active",
                "cookies": {}, "host": "icloud.com",
                "alias_total": 0, "alias_active": 0,
            }
            progress = []
            results = manager.create_aliases_for_account(
                "acc", count=3,
                progress_callback=lambda item: progress.append(item["email"]),
            )
            assert len([item for item in results if item.get("ok")]) == 3
            assert sleeps == [4, 4]
            assert progress == [
                "paced-1@icloud.com", "paced-2@icloud.com", "paced-3@icloud.com"
            ]
        finally:
            (
                account_manager.ACCOUNTS_FILE,
                account_manager.LATEST_EMAILS,
                account_manager.CREATE_ALIAS_INTERVAL_SECONDS,
                account_manager.CREATE_ALIAS_JITTER_SECONDS,
                account_manager.time.sleep,
                account_manager.random.uniform,
                icloud_hme.ICloudHME,
            ) = originals
    print("  PASS test_account_creation_is_paced_between_aliases")


def test_pickup_uses_persistent_body_and_deduplicates_sync():
    import web_ui

    class FakePickupStore:
        def get_by_token(self, token):
            if token == "valid":
                return {"account_id": "acc", "alias_email": "alias@icloud.com"}
            return None

    class FakeCache:
        def get_alias_mail(self, _account_id, _alias):
            return [{"id": "7", "subject": "cached"}]

    class FakeManager:
        _cache = FakeCache()

    class FakeBodyStore:
        def get(self, account_id, message_id):
            if account_id == "acc" and message_id == "7":
                return {"id": "7", "body": "persisted body", "html": ""}
            return None

    class FakeExecutor:
        calls = 0

        def submit(self, _fn, _account_id):
            self.calls += 1

    originals = (
        web_ui._pickup_store, web_ui._account_mgr, web_ui._pickup_body_store,
        web_ui._pickup_executor, web_ui._pickup_refreshing_accounts,
        web_ui._pickup_last_account_refresh, web_ui._pickup_pending,
    )
    try:
        web_ui._pickup_store = FakePickupStore()
        web_ui._account_mgr = FakeManager()
        web_ui._pickup_body_store = FakeBodyStore()
        web_ui._pickup_executor = FakeExecutor()
        web_ui._pickup_refreshing_accounts = set()
        web_ui._pickup_last_account_refresh = {}
        web_ui._pickup_pending = 0
        client = web_ui.app.test_client()

        first = client.get("/pickup/valid/messages")
        second = client.get("/pickup/valid/messages")
        assert first.status_code == 200 and second.status_code == 200
        assert web_ui._pickup_executor.calls == 1

        body = client.get("/pickup/valid/message/7")
        assert body.status_code == 200
        assert body.get_json()["ready"] is True
        assert body.get_json()["message"]["body"] == "persisted body"
        page = client.get("/pickup/valid")
        page_html = page.get_data(as_text=True)
        assert "idlePolls" in page_html
        assert "document.hidden?15000" in page_html
    finally:
        (
            web_ui._pickup_store, web_ui._account_mgr, web_ui._pickup_body_store,
            web_ui._pickup_executor, web_ui._pickup_refreshing_accounts,
            web_ui._pickup_last_account_refresh, web_ui._pickup_pending,
        ) = originals
    print("  PASS test_pickup_uses_persistent_body_and_deduplicates_sync")


def test_batch_runs_accounts_in_parallel_and_creates_pickup_links():
    import web_ui

    class FakeManager:
        accounts = {
            "one": {"id": "one", "name": "one", "status": "active"},
            "two": {"id": "two", "name": "two", "status": "active"},
        }
        active = 0
        max_active = 0

        def get_account(self, account_id):
            return self.accounts.get(account_id)

        def create_aliases_for_account(self, account_id, count, _label, progress_callback=None, **_kwargs):
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            try:
                time.sleep(0.12)
                result = {
                    "ok": True,
                    "email": f"{account_id}@icloud.com",
                    "account_id": account_id,
                }
                if progress_callback:
                    progress_callback(result)
                return [result]
            finally:
                self.active -= 1

    class FakePickupStore:
        def __init__(self):
            self.created = []

        def ensure(self, account_id, email):
            self.created.append((account_id, email))
            return {"account_id": account_id, "alias_email": email, "token": "token"}

    original_manager = web_ui._account_mgr
    original_pickup = web_ui._pickup_store
    original_jobs = web_ui._batch_jobs
    original_active = web_ui._batch_active_id
    original_state_file = web_ui._BATCH_STATE_FILE
    original_workers = web_ui._BATCH_MAX_ACCOUNT_WORKERS
    with tempfile.TemporaryDirectory() as td:
        now = "2026-08-17T00:00:00+08:00"
        job = {
            "id": "parallel", "status": "queued",
            "account_ids": ["one", "two"], "count_per_account": 1,
            "interval": 0, "label": "", "total_accounts": 2,
            "completed_accounts": 0, "total_created": 0, "total_errors": 0,
            "created_at": now, "updated_at": now,
            "accounts": {
                account_id: {
                    "account_id": account_id, "name": account_id,
                    "status": "queued", "created": 0, "errors": 0,
                    "limited": False, "error": "", "retry_count": 0,
                    "retry_delay_seconds": 0, "retry_at": None,
                }
                for account_id in ("one", "two")
            },
        }
        try:
            manager = FakeManager()
            pickup = FakePickupStore()
            web_ui._account_mgr = manager
            web_ui._pickup_store = pickup
            web_ui._batch_jobs = web_ui.OrderedDict([("parallel", job)])
            web_ui._batch_active_id = "parallel"
            web_ui._BATCH_STATE_FILE = Path(td) / "batch_jobs.json"
            web_ui._BATCH_MAX_ACCOUNT_WORKERS = 2
            started = time.monotonic()
            web_ui._run_batch_job("parallel")
            elapsed = time.monotonic() - started
            assert manager.max_active == 2
            assert elapsed < 0.22
            assert job["status"] == "completed"
            assert job["total_created"] == 2
            assert sorted(pickup.created) == [
                ("one", "one@icloud.com"), ("two", "two@icloud.com")
            ]
        finally:
            web_ui._account_mgr = original_manager
            web_ui._pickup_store = original_pickup
            web_ui._batch_jobs = original_jobs
            web_ui._batch_active_id = original_active
            web_ui._BATCH_STATE_FILE = original_state_file
            web_ui._BATCH_MAX_ACCOUNT_WORKERS = original_workers
    print("  PASS test_batch_runs_accounts_in_parallel_and_creates_pickup_links")


def test_stale_account_storage_rebinds_without_duplicates():
    import mail_cache
    from export_history import ExportHistoryStore
    from mail_body_store import MailBodyStore

    original_cache_file = mail_cache.CACHE_FILE
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        try:
            mail_cache.CACHE_FILE = root / "mail_cache.json"
            cache = mail_cache.MailCache()
            message = {"id": "7", "subject": "code"}
            cache.set_inbox("old", [message])
            cache.set_alias_mail("old", "alias@icloud.com", [message])
            assert cache.rebind_accounts({"old": "new"}) == 1
            assert cache.get_inbox("old") == []
            assert cache.get_inbox("new") == [message]
            assert cache.get_alias_mail("new", "alias@icloud.com") == [message]

            bodies = MailBodyStore(root / "bodies.sqlite3")
            bodies.put("old", "7", {"body": "hello"})
            assert bodies.rebind_accounts({"old": "new"}) == 1
            assert bodies.get("old", "7") is None
            assert bodies.get("new", "7")["body"] == "hello"
            bodies.close()

            exports = ExportHistoryStore(root / "exports.json")
            exports.claim([{"email": "alias@icloud.com", "account_id": "old"}])
            assert exports.rebind_accounts(
                {"old": "new"}, {"alias@icloud.com": "new"}
            ) == 1
            assert exports.get("alias@icloud.com")["account_id"] == "new"
        finally:
            mail_cache.CACHE_FILE = original_cache_file
    print("  PASS test_stale_account_storage_rebinds_without_duplicates")


def test_pickup_page_uses_adaptive_refresh():
    import web_ui

    class FakePickupStore:
        def get_by_token(self, _token):
            return {"account_id": "one", "alias_email": "alias@icloud.com"}

    original_pickup = web_ui._pickup_store
    try:
        web_ui._pickup_store = FakePickupStore()
        response = web_ui.app.test_client().get("/pickup/test-token")
        html = response.get_data(as_text=True)
        assert response.status_code == 200
        assert "idlePolls" in html
        assert "document.hidden?15000" in html
        assert "visibilitychange" in html
    finally:
        web_ui._pickup_store = original_pickup
    print("  PASS test_pickup_page_uses_adaptive_refresh")


def test_export_actions_respect_visible_filter_and_mobile_controls():
    import web_ui

    html = web_ui.UI_HTML
    assert "function copyAll(){var filtered=visibleAliases();" in html
    assert "function exportCSV(){var filtered=visibleAliases();" in html
    assert 'class="inbox-tools"' in html
    assert "inboxRequestCurrent(seq,accId)" in html
    assert "toggleEmail(domId,msgId,accountId)" in html
    assert 'href="https://account.apple.com/"' in html
    assert 'id="btnAliasSync"' in html
    print("  PASS test_export_actions_respect_visible_filter_and_mobile_controls")


def test_account_alias_sync_is_parallel_and_reports_failures():
    from account_manager import AccountManager

    manager = object.__new__(AccountManager)
    manager.accounts = {
        "one": {"name": "One", "real_email": "one@icloud.com"},
        "two": {"name": "Two", "real_email": "two@icloud.com"},
        "bad": {"name": "Bad", "real_email": "bad@icloud.com"},
    }

    def fetch(acc_id, raise_errors=False):
        time.sleep(0.15)
        if acc_id == "bad":
            raise RuntimeError("expired session")
        return [{"email": f"{acc_id}@icloud.com"}]

    manager.get_aliases_for_account = fetch
    started = time.monotonic()
    aliases, statuses = manager.get_all_aliases_with_status(max_workers=5)
    elapsed = time.monotonic() - started
    assert elapsed < 0.35
    assert {item["account_id"] for item in aliases} == {"one", "two"}
    assert statuses["one"]["ok"] is True
    assert statuses["bad"]["ok"] is False
    assert "expired session" in statuses["bad"]["error"]
    print("  PASS test_account_alias_sync_is_parallel_and_reports_failures")


def test_manual_create_conflict_and_input_status_codes():
    import web_ui

    class FakeManager:
        def get_account(self, acc_id):
            return {"id": acc_id} if acc_id == "busy" else None

    original_manager = web_ui._account_mgr
    original_busy = set(web_ui._manual_creating_accounts)
    try:
        web_ui._account_mgr = FakeManager()
        web_ui._manual_creating_accounts.clear()
        web_ui._manual_creating_accounts.add("busy")
        client = web_ui.app.test_client()
        response = client.post("/api/accounts/busy/create", json={"count": 1})
        assert response.status_code == 409
        assert response.get_json()["ok"] is False
        assert client.post("/api/accounts/add", json={}).status_code == 400
        assert client.post(
            "/api/accounts/busy/app-password", json={"app_password": ""}
        ).status_code == 400
        assert client.post("/api/accounts/missing/remove").status_code == 404
    finally:
        web_ui._account_mgr = original_manager
        web_ui._manual_creating_accounts.clear()
        web_ui._manual_creating_accounts.update(original_busy)
    print("  PASS test_manual_create_conflict_and_input_status_codes")


def test_remove_account_purges_all_local_data():
    import web_ui

    class FakeCache:
        def __init__(self):
            self.cleared = []

        def clear_account(self, acc_id):
            self.cleared.append(acc_id)

    class FakeManager:
        def __init__(self):
            import threading
            self._latest_emails_lock = threading.Lock()
            self._cache = FakeCache()
            self.removed = []

        def get_account(self, acc_id):
            return {"id": acc_id} if acc_id == "one" else None

        def remove_account(self, acc_id):
            self.removed.append(acc_id)
            return True

    class FakePickup:
        def __init__(self):
            self.revoked = []

        def revoke_account(self, acc_id):
            self.revoked.append(acc_id)
            return 2

    class FakeExports:
        def __init__(self):
            self.deleted = []

        def delete_account(self, acc_id):
            self.deleted.append(acc_id)
            return 2

    class FakeBodies:
        def __init__(self):
            self.deleted = []

        def delete_account(self, acc_id):
            self.deleted.append(acc_id)

    originals = (
        web_ui._account_mgr, web_ui._pickup_store, web_ui._export_store,
        web_ui._pickup_body_store, web_ui.RESULTS_DIR, web_ui._emit_log,
        set(web_ui._removed_account_ids),
    )
    with tempfile.TemporaryDirectory() as td:
        try:
            manager = FakeManager()
            pickup = FakePickup()
            exports = FakeExports()
            bodies = FakeBodies()
            web_ui._account_mgr = manager
            web_ui._pickup_store = pickup
            web_ui._export_store = exports
            web_ui._pickup_body_store = bodies
            web_ui.RESULTS_DIR = Path(td)
            web_ui._emit_log = lambda *_args: None
            web_ui._removed_account_ids.clear()
            (Path(td) / "latest_emails.txt").write_text(
                "a@icloud.com\tone\t2026-08-17\n"
                "b@icloud.com\ttwo\t2026-08-17\n",
                encoding="utf-8",
            )
            response = web_ui.app.test_client().post("/api/accounts/one/remove")
            payload = response.get_json()
            assert response.status_code == 200
            assert payload["cleanup"] == {
                "pickup_links": 2,
                "latest_emails": 1,
                "export_history": 2,
            }
            assert pickup.revoked == ["one"]
            assert exports.deleted == ["one"]
            assert bodies.deleted == ["one"]
            assert manager._cache.cleared == ["one"]
            remaining = (Path(td) / "latest_emails.txt").read_text(encoding="utf-8")
            assert "\tone\t" not in remaining
            assert "\ttwo\t" in remaining
        finally:
            (
                web_ui._account_mgr, web_ui._pickup_store, web_ui._export_store,
                web_ui._pickup_body_store, web_ui.RESULTS_DIR, web_ui._emit_log,
                removed_ids,
            ) = originals
            web_ui._removed_account_ids.clear()
            web_ui._removed_account_ids.update(removed_ids)
    print("  PASS test_remove_account_purges_all_local_data")


def test_alias_api_returns_partial_failure_details():
    import web_ui

    class FakeManager:
        def get_all_aliases_with_status(self, max_workers=5):
            assert max_workers == 5
            return ([{"email": "ok@icloud.com"}], {
                "one": {"ok": True, "count": 1},
                "two": {"ok": False, "count": 0, "error": "expired"},
            })

    original = web_ui._account_mgr
    try:
        web_ui._account_mgr = FakeManager()
        response = web_ui.app.test_client().get("/api/aliases")
        payload = response.get_json()
        assert response.status_code == 200
        assert payload["ok"] is False
        assert payload["count"] == 1
        assert payload["failures"]["two"]["error"] == "expired"
    finally:
        web_ui._account_mgr = original
    print("  PASS test_alias_api_returns_partial_failure_details")



def test_public_bind_requires_admin_token():
    import web_ui
    assert web_ui._public_bind_blocked("0.0.0.0", "") is True
    assert web_ui._public_bind_blocked("::", "") is True
    assert web_ui._public_bind_blocked("127.0.0.1", "") is False
    assert web_ui._public_bind_blocked("0.0.0.0", "secret") is False
    source = Path(web_ui.__file__).read_text(encoding="utf-8")
    assert 'os.environ.get("HOST","127.0.0.1")' in source
    print("  PASS test_public_bind_requires_admin_token")


def test_scheduler_skips_accounts_with_active_create():
    import web_ui
    original_busy = set(web_ui._manual_creating_accounts)
    try:
        web_ui._manual_creating_accounts.clear()
        web_ui._manual_creating_accounts.add("busy")
        assert web_ui._account_create_in_progress("busy") is True
        assert web_ui._account_create_in_progress("free") is False
        source = Path(web_ui.__file__).read_text(encoding="utf-8")
        assert "if _account_create_in_progress(acc_id):" in source
        assert "create_aliases_for_account(" in source
    finally:
        web_ui._manual_creating_accounts.clear()
        web_ui._manual_creating_accounts.update(original_busy)
    print("  PASS test_scheduler_skips_accounts_with_active_create")


def test_ui_create_entry_and_scheduler_copy():
    import web_ui
    html = web_ui.UI_HTML
    assert "pendingBatchAccountId=accId||null" in html
    assert "function copyText(" in html
    assert "function copyAll(){var filtered=visibleAliases();" in html
    assert ",5)\">创建邮箱" not in html
    assert "北京时间 7:00 到 20:00" in html
    assert "正在批量创建的账号会自动跳过" in html
    assert "toast(t('status.checking_login'))" in html
    assert "d.error||t('status.login_expired')" in html
    assert "最多 10 个并行" in html
    assert "Up to 10 accounts run in parallel" in html
    assert 'os.environ.get("BATCH_MAX_ACCOUNT_WORKERS", "10")' in Path(web_ui.__file__).read_text(encoding="utf-8")
    assert "accHostInput" in html
    assert "await refreshEmails();emails.forEach" in html
    assert "_load_scheduler_enabled()" in Path(web_ui.__file__).read_text(encoding="utf-8")
    assert 'class="social-links"' in html
    assert 'href="https://x.com/fangao798"' in html
    assert 'href="https://t.co/fd6OPHgvKm"' in html
    print("  PASS test_ui_create_entry_and_scheduler_copy")


def test_parse_cookie_editor_array():
    from account_manager import AccountManager
    mgr = AccountManager()
    raw = """[{"name":"X_APPLE_WEB_KB","value":"abc123","domain":".icloud.com.cn"},{"Name":"SESSION_TOKEN","Value":"xyz789","domain":".icloud.com.cn"}]"""
    cookies = mgr.parse_cookie_input(raw)
    assert cookies["X_APPLE_WEB_KB"] == "abc123"
    assert cookies["SESSION_TOKEN"] == "xyz789"
    assert mgr.detect_icloud_host(raw) == "icloud.com.cn"
    assert mgr.detect_icloud_host("X_APPLE_WEB_KB=abc") == "icloud.com"
    print("  PASS test_parse_cookie_editor_array")


def test_record_known_aliases_and_failed_add():
    import account_manager
    import icloud_hme

    class FakeHME:
        def __init__(self, *_args, **_kwargs):
            pass
        def validate_session(self):
            return {}
        def get_account_info(self):
            return {"appleId": "user@icloud.com", "primaryEmail": "user@icloud.com"}
        def list_aliases(self):
            return [
                {"email": "old@icloud.com", "active": True, "createdAt": 1710000000000},
                {"email": "OLD@icloud.com", "active": True},
            ]

    class BoomHME(FakeHME):
        def validate_session(self):
            raise RuntimeError("expired cookie")

    originals = (account_manager.ACCOUNTS_FILE, account_manager.LATEST_EMAILS, icloud_hme.ICloudHME)
    with tempfile.TemporaryDirectory() as td:
        try:
            account_manager.ACCOUNTS_FILE = Path(td) / "accounts.json"
            account_manager.LATEST_EMAILS = Path(td) / "latest_emails.txt"
            icloud_hme.ICloudHME = BoomHME
            mgr = account_manager.AccountManager()
            try:
                mgr.add_account("bad", "a=b")
                assert False, "should reject invalid cookies"
            except ValueError:
                pass
            assert mgr.accounts == {}

            icloud_hme.ICloudHME = FakeHME
            first = mgr.add_account("one", "a=b")
            second = mgr.add_account("two", "a=b")
            assert first["id"] == second["id"]
            assert len(mgr.accounts) == 1
            lines = account_manager.LATEST_EMAILS.read_text(encoding="utf-8").strip().splitlines()
            assert len(lines) == 1
            assert lines[0].startswith("old@icloud.com" + chr(9))
            added = mgr.record_known_aliases(first["id"], [{"email": "old@icloud.com"}])
            assert added == 0
        finally:
            account_manager.ACCOUNTS_FILE, account_manager.LATEST_EMAILS, icloud_hme.ICloudHME = originals
    print("  PASS test_record_known_aliases_and_failed_add")


def test_find_by_recipient_matches_delivered_to():
    from icloud_mail import ICloudMail

    header = (
        b"From: Sender <sender@example.com>\r\n"
        b"To: hidden-forward@icloud.com\r\n"
        b"Delivered-To: hide@icloud.com\r\n"
        b"Subject: Code 123456\r\n"
        b"Date: Sun, 17 Aug 2026 00:00:00 +0800\r\n"
        b"X-Pad: " + (b"x" * 80) + b"\r\n"
    )

    class FakeConnection:
        state = "SELECTED"

        def uid(self, command, *args):
            if command == "SEARCH":
                criteria = " ".join(str(item) for item in args)
                if "TO" in criteria:
                    return "OK", [b""]
                return "OK", [b"7"]
            if command == "FETCH":
                return "OK", [(b"1 (UID 7 BODY[HEADER])", header), b")"]
            raise AssertionError(command)

    mail = ICloudMail("user@icloud.com", "password")
    mail._conn = FakeConnection()
    found = mail.find_by_recipient("hide@icloud.com", limit=5)
    assert len(found) == 1
    assert "hide@icloud.com" in found[0]["recipients"]
    print("  PASS test_find_by_recipient_matches_delivered_to")


def test_check_all_aliases_mail_raises_without_cache():
    from account_manager import AccountManager

    mgr = object.__new__(AccountManager)
    class Cache:
        def get_all_alias_mail(self, _acc_id):
            return {}
        def cache_age_seconds(self, _acc_id):
            return 9999
    mgr._cache = Cache()
    def boom(_acc_id, verbose=False):
        raise RuntimeError("imap down")
    mgr.get_client = boom
    try:
        mgr.check_all_aliases_mail("acc", force=True)
        assert False, "should raise"
    except RuntimeError as exc:
        assert "imap down" in str(exc)
    print("  PASS test_check_all_aliases_mail_raises_without_cache")



def test_mail_newest_first():
    import mail_cache
    old_path = mail_cache.CACHE_FILE
    temp_dir = tempfile.TemporaryDirectory()
    mail_cache.CACHE_FILE = Path(temp_dir.name) / "mail_cache.json"
    cache = mail_cache.MailCache()
    cache.set_alias_mail("acc", "a@icloud.com", [
        {"id": "old", "date": "2026-08-19T14:37:23", "subject": "old"},
        {"id": "new", "date": "2026-08-22T08:30:03", "subject": "new"},
        {"id": "mid", "date": "2026-08-21T17:56:57", "subject": "mid"},
    ])
    got = cache.get_alias_mail("acc", "a@icloud.com")
    assert [item["id"] for item in got] == ["new", "mid", "old"], [item["id"] for item in got]
    many = [{"id": str(i), "date": "2026-08-01T00:00:%02d" % (i % 60)} for i in range(mail_cache.MAX_ALIAS_MESSAGES)]
    many.append({"id": "fresh", "date": "2026-08-22T12:00:00"})
    cache.set_alias_mail("acc", "b@icloud.com", many)
    kept_ids = [item["id"] for item in cache.get_alias_mail("acc", "b@icloud.com")]
    assert "fresh" in kept_ids
    assert len(kept_ids) == mail_cache.MAX_ALIAS_MESSAGES
    mail_cache.CACHE_FILE = old_path
    temp_dir.cleanup()
    print("  PASS test_mail_newest_first")


def test_health_loop_retries_error_accounts():
    import web_ui
    source = Path(web_ui.__file__).read_text(encoding="utf-8")
    assert "if result.get(\"status\") == \"active\":" in source
    assert "if account.get(\"status\") != \"active\": continue" not in source.split("def _health_loop():", 1)[1].split("# ----- HTML -----", 1)[0]
    assert "_manual_creating_accounts.add(acc_id)" in source.split("def _scheduler_loop():", 1)[0] + source.split("def _scheduler_loop():", 1)[1][:2500]
    print("  PASS test_health_loop_retries_error_accounts")




def test_batch_pause_keeps_remaining_and_stop_discards_it():
    import threading
    import web_ui

    started = {"keep": threading.Event()}

    class FakeManager:
        accounts = {
            "keep": {"id": "keep", "name": "keep", "status": "active"},
            "drop": {"id": "drop", "name": "drop", "status": "active"},
        }

        def get_account(self, account_id):
            return self.accounts.get(account_id)

        def create_aliases_for_account(self, account_id, count, _label, progress_callback=None, should_stop=None, wait=None):
            results = []
            for index in range(count):
                if callable(should_stop) and should_stop():
                    break
                item = {"ok": True, "email": "%s-%s@icloud.com" % (account_id, index), "account_id": account_id}
                results.append(item)
                if progress_callback:
                    progress_callback(item)
                if account_id == "keep":
                    started["keep"].set()
                if callable(wait):
                    wait(0.2)
                else:
                    time.sleep(0.2)
                if callable(should_stop) and should_stop():
                    break
            return results

    original_manager = web_ui._account_mgr
    original_state_file = web_ui._BATCH_STATE_FILE
    original_control_file = web_ui._CREATE_CONTROL_FILE
    temp_dir = tempfile.TemporaryDirectory()
    web_ui._BATCH_STATE_FILE = Path(temp_dir.name) / "batch_jobs.json"
    web_ui._CREATE_CONTROL_FILE = Path(temp_dir.name) / "create_controls.json"
    with web_ui._batch_lock:
        original_jobs = web_ui._batch_jobs
        original_active = web_ui._batch_active_id
        original_runners = set(web_ui._batch_runner_jobs)
        web_ui._batch_jobs = web_ui.OrderedDict()
        web_ui._batch_active_id = None
        web_ui._batch_runner_jobs.clear()
    with web_ui._account_control_lock:
        original_paused = set(web_ui._paused_account_ids)
        original_controls = dict(web_ui._account_controls)
        web_ui._paused_account_ids = set()
        web_ui._account_controls = {}
    web_ui._account_mgr = FakeManager()
    client = web_ui.app.test_client()
    try:
        started_job = client.post("/api/create-batch", json={
            "account_ids": ["keep", "drop"],
            "count_per_account": 4,
            "interval": 0,
        })
        assert started_job.status_code == 202
        job_id = started_job.get_json()["job_id"]
        assert started["keep"].wait(2)
        paused = client.post("/api/create-control", json={
            "action": "pause",
            "account_ids": ["keep"],
        })
        assert paused.status_code == 200
        deadline = time.time() + 4
        job = None
        while time.time() < deadline:
            job = client.get("/api/create-batch/%s" % job_id).get_json()["job"]
            if job["accounts"]["keep"]["status"] == "paused":
                break
            time.sleep(0.02)
        assert job["accounts"]["keep"]["status"] == "paused"
        assert job["accounts"]["keep"]["created"] >= 1
        remaining = job["accounts"]["keep"]["target"] - job["accounts"]["keep"]["created"]
        assert remaining > 0
        paused_created = job["accounts"]["keep"]["created"]
        resumed = client.post("/api/create-batch", json={
            "account_ids": ["keep"],
            "count_per_account": 4,
            "interval": 0,
        })
        assert resumed.status_code == 202
        deadline = time.time() + 4
        while time.time() < deadline:
            job = client.get("/api/create-batch/%s" % job_id).get_json()["job"]
            if job["accounts"]["keep"]["created"] > paused_created or job["accounts"]["keep"]["status"] in ("completed", "partial"):
                break
            time.sleep(0.02)
        assert job["accounts"]["keep"]["created"] >= paused_created
        html = web_ui.UI_HTML
        assert "btnBatchPause" in html
        assert "btnBatchStop" in html
        assert "function controlBatchCreate" in html
    finally:
        web_ui._account_mgr = original_manager
        with web_ui._batch_lock:
            web_ui._batch_jobs = original_jobs
            web_ui._batch_active_id = original_active
            web_ui._batch_runner_jobs.clear()
            web_ui._batch_runner_jobs.update(original_runners)
        with web_ui._account_control_lock:
            web_ui._paused_account_ids = original_paused
            web_ui._account_controls = original_controls
        web_ui._BATCH_STATE_FILE = original_state_file
        web_ui._CREATE_CONTROL_FILE = original_control_file
        temp_dir.cleanup()
    print("  PASS test_batch_pause_keeps_remaining_and_stop_discards_it")


def test_create_control_stop_cancels_remaining():
    import threading
    import web_ui

    started = {"slow": threading.Event()}

    class FakeManager:
        accounts = {"slow": {"id": "slow", "name": "slow", "status": "active"}}

        def get_account(self, account_id):
            return self.accounts.get(account_id)

        def create_aliases_for_account(self, account_id, count, _label, progress_callback=None, should_stop=None, wait=None):
            results = []
            for index in range(count):
                if callable(should_stop) and should_stop():
                    break
                item = {"ok": True, "email": "slow-%s@icloud.com" % index, "account_id": account_id}
                results.append(item)
                if progress_callback:
                    progress_callback(item)
                started["slow"].set()
                if callable(wait):
                    wait(0.3)
                if callable(should_stop) and should_stop():
                    break
            return results

    original_manager = web_ui._account_mgr
    original_state_file = web_ui._BATCH_STATE_FILE
    original_control_file = web_ui._CREATE_CONTROL_FILE
    temp_dir = tempfile.TemporaryDirectory()
    web_ui._BATCH_STATE_FILE = Path(temp_dir.name) / "batch_jobs.json"
    web_ui._CREATE_CONTROL_FILE = Path(temp_dir.name) / "create_controls.json"
    with web_ui._batch_lock:
        original_jobs = web_ui._batch_jobs
        original_active = web_ui._batch_active_id
        original_runners = set(web_ui._batch_runner_jobs)
        web_ui._batch_jobs = web_ui.OrderedDict()
        web_ui._batch_active_id = None
        web_ui._batch_runner_jobs.clear()
    with web_ui._account_control_lock:
        original_paused = set(web_ui._paused_account_ids)
        original_controls = dict(web_ui._account_controls)
        web_ui._paused_account_ids = set()
        web_ui._account_controls = {}
    web_ui._account_mgr = FakeManager()
    client = web_ui.app.test_client()
    try:
        posted = client.post("/api/create-batch", json={
            "account_ids": ["slow"],
            "count_per_account": 6,
            "interval": 0,
        })
        assert posted.status_code == 202
        job_id = posted.get_json()["job_id"]
        assert started["slow"].wait(2)
        stopped = client.post("/api/create-control", json={
            "action": "stop",
            "account_ids": ["slow"],
        })
        assert stopped.status_code == 200
        deadline = time.time() + 3
        job = None
        while time.time() < deadline:
            job = client.get("/api/create-batch/%s" % job_id).get_json()["job"]
            if job["accounts"]["slow"]["status"] == "stopped":
                break
            time.sleep(0.02)
        assert job["accounts"]["slow"]["status"] == "stopped"
        assert job["accounts"]["slow"]["created"] < 6
        assert job["accounts"]["slow"].get("finished_at")
    finally:
        web_ui._account_mgr = original_manager
        with web_ui._batch_lock:
            web_ui._batch_jobs = original_jobs
            web_ui._batch_active_id = original_active
            web_ui._batch_runner_jobs.clear()
            web_ui._batch_runner_jobs.update(original_runners)
        with web_ui._account_control_lock:
            web_ui._paused_account_ids = original_paused
            web_ui._account_controls = original_controls
        web_ui._BATCH_STATE_FILE = original_state_file
        web_ui._CREATE_CONTROL_FILE = original_control_file
        temp_dir.cleanup()
    print("  PASS test_create_control_stop_cancels_remaining")



def test_reimport_keeps_account_and_rejects_mismatch():
    import account_manager
    import icloud_hme

    class FakeHME:
        info = {"appleId": "keep@163.com", "primaryEmail": "keep@163.com"}
        aliases = [{"email": "alias@icloud.com", "active": True}]

        def __init__(self, cookies, host="icloud.com", verbose=False):
            self.cookies = cookies
            self.host = host

        def validate_session(self):
            if self.cookies.get("fail"):
                raise RuntimeError("HTTP 421: trustTokens")
            return True

        def get_account_info(self):
            return dict(self.info)

        def list_aliases(self):
            return list(self.aliases)

    originals = (
        account_manager.ACCOUNTS_FILE,
        account_manager.LATEST_EMAILS,
        icloud_hme.ICloudHME,
    )
    with tempfile.TemporaryDirectory() as td:
        try:
            account_manager.ACCOUNTS_FILE = Path(td) / "accounts.json"
            account_manager.LATEST_EMAILS = Path(td) / "latest_emails.txt"
            icloud_hme.ICloudHME = FakeHME
            manager = account_manager.AccountManager()
            manager.accounts["acc"] = {
                "id": "acc",
                "name": "keep",
                "real_email": "keep@163.com",
                "icloud_email": "keep@icloud.com",
                "cookies": {"old": "1"},
                "host": "icloud.com",
                "status": "error",
                "last_error": "HTTP 421",
                "app_password": "secret-app-password",
                "alias_total": 5,
                "alias_active": 5,
                "created_at": "2026-01-01T00:00:00",
            }
            updated = manager.reimport_account(
                "acc",
                '{"a":"new-cookie"}',
                host="icloud.com.cn",
            )
            assert updated["id"] == "acc"
            assert updated["status"] == "active"
            assert updated["cookies"] == {"a": "new-cookie"}
            assert updated["host"] == "icloud.com.cn"
            assert updated["app_password"] == "secret-app-password"
            assert updated["created_at"] == "2026-01-01T00:00:00"
            assert updated["alias_total"] == 1
            assert updated["last_error"] is None
            saved = json.loads(account_manager.ACCOUNTS_FILE.read_text(encoding="utf-8"))
            assert saved["accounts"]["acc"]["cookies"] == {"a": "new-cookie"}
            assert saved["accounts"]["acc"]["app_password"] == "secret-app-password"

            FakeHME.info = {"appleId": "other@163.com"}
            try:
                manager.reimport_account("acc", '{"a":"other"}')
                raise AssertionError("mismatch should fail")
            except ValueError as exc:
                assert "不一致" in str(exc)
            assert manager.accounts["acc"]["cookies"] == {"a": "new-cookie"}

            try:
                manager.reimport_account("acc", '{"fail":"1"}')
                raise AssertionError("invalid cookie should fail")
            except ValueError as exc:
                assert "421" in str(exc)
            assert manager.accounts["acc"]["cookies"] == {"a": "new-cookie"}
            assert manager.accounts["acc"]["status"] == "active"
        finally:
            (
                account_manager.ACCOUNTS_FILE,
                account_manager.LATEST_EMAILS,
                icloud_hme.ICloudHME,
            ) = originals
    print("  PASS test_reimport_keeps_account_and_rejects_mismatch")


def test_reimport_api_and_expired_account_button():
    import web_ui

    class FakeManager:
        accounts = {
            "acc": {
                "id": "acc",
                "name": "keep",
                "real_email": "keep@163.com",
                "status": "error",
                "app_password": "secret",
                "alias_total": 5,
            }
        }

        def get_account(self, acc_id):
            return self.accounts.get(acc_id)

        def reimport_account(self, acc_id, cookie_input, host="icloud.com"):
            if acc_id not in self.accounts:
                raise KeyError(acc_id)
            if "mismatch" in cookie_input:
                raise ValueError("Cookie 属于 other@163.com，与当前账号 keep@163.com 不一致")
            item = self.accounts[acc_id]
            item["status"] = "active"
            item["last_error"] = None
            item["host"] = host
            return dict(item)

    original = web_ui._account_mgr
    web_ui._account_mgr = FakeManager()
    client = web_ui.app.test_client()
    try:
        missing = client.post("/api/accounts/missing/reimport", json={"cookie_input": "a=b"})
        assert missing.status_code == 404
        bad = client.post("/api/accounts/acc/reimport", json={"cookie_input": "mismatch"})
        assert bad.status_code == 400
        assert "不一致" in bad.get_json()["error"]
        ok = client.post("/api/accounts/acc/reimport", json={
            "cookie_input": "a=b",
            "host": "icloud.com.cn",
        })
        assert ok.status_code == 200
        body = ok.get_json()
        assert body["ok"] is True
        assert body["id"] == "acc"
        assert body["real_email"] == "keep@163.com"
        html = web_ui.UI_HTML
        assert "function showReimportModal" in html
        assert "a.status!=='active'" in html or 'a.status!=="active"' in html
        assert "action.reimport" in html
    finally:
        web_ui._account_mgr = original
    print("  PASS test_reimport_api_and_expired_account_button")

def test_batch_can_add_second_account_while_first_is_running():
    import threading
    import web_ui

    started_one = threading.Event()
    release_one = threading.Event()

    class FakeManager:
        accounts = {
            "one": {"id": "one", "name": "one", "status": "active"},
            "two": {"id": "two", "name": "two", "status": "active"},
        }

        def get_account(self, account_id):
            return self.accounts.get(account_id)

        def create_aliases_for_account(self, account_id, count, _label, **_kwargs):
            if account_id == "one":
                started_one.set()
                if not release_one.wait(5):
                    raise AssertionError("timed out waiting to release account one")
            return [
                {"ok": True, "email": "%s-%s@icloud.com" % (account_id, index), "account_id": account_id}
                for index in range(count)
            ]

    original_manager = web_ui._account_mgr
    original_state_file = web_ui._BATCH_STATE_FILE
    temp_dir = tempfile.TemporaryDirectory()
    web_ui._BATCH_STATE_FILE = Path(temp_dir.name) / "batch_jobs.json"
    with web_ui._batch_lock:
        original_jobs = web_ui._batch_jobs
        original_active = web_ui._batch_active_id
        original_runners = set(web_ui._batch_runner_jobs)
        web_ui._batch_jobs = web_ui.OrderedDict()
        web_ui._batch_active_id = None
        web_ui._batch_runner_jobs.clear()
    web_ui._account_mgr = FakeManager()
    client = web_ui.app.test_client()
    try:
        first = client.post("/api/create-batch", json={
            "account_ids": ["one"],
            "count_per_account": 1,
            "interval": 0,
        })
        assert first.status_code == 202, first.get_json()
        assert started_one.wait(3)
        second = client.post("/api/create-batch", json={
            "account_ids": ["two"],
            "count_per_account": 1,
            "interval": 0,
        })
        assert second.status_code == 202, second.get_json()
        payload = second.get_json()
        assert payload["ok"] is True
        assert "two" in payload["job"]["accounts"]
        deadline = time.time() + 3
        two_done = False
        while time.time() < deadline:
            job = client.get("/api/create-batch/%s" % payload["job_id"]).get_json()["job"]
            if job["accounts"]["two"].get("status") == "completed":
                two_done = True
                break
            time.sleep(0.02)
        assert two_done, job["accounts"]
        assert job["accounts"]["one"]["status"] in ("queued", "running", "waiting")
        same = client.post("/api/create-batch", json={
            "account_ids": ["one"],
            "count_per_account": 1,
        })
        assert same.status_code == 409
        release_one.set()
        deadline = time.time() + 3
        while time.time() < deadline:
            job = client.get("/api/create-batch/%s" % payload["job_id"]).get_json()["job"]
            if job["status"] not in ("queued", "running"):
                break
            time.sleep(0.02)
        assert job["status"] == "completed"
        assert job["total_created"] == 2
        html = web_ui.UI_HTML
        assert "function batchBusyAccountIds" in html
        assert "E('btnBatchExec').disabled=!!(batchJob&&(batchJob.status==='queued'||batchJob.status==='running'))" not in html
    finally:
        release_one.set()
        web_ui._account_mgr = original_manager
        with web_ui._batch_lock:
            web_ui._batch_jobs = original_jobs
            web_ui._batch_active_id = original_active
            web_ui._batch_runner_jobs.clear()
            web_ui._batch_runner_jobs.update(original_runners)
        web_ui._BATCH_STATE_FILE = original_state_file
        temp_dir.cleanup()
    print("  PASS test_batch_can_add_second_account_while_first_is_running")



def test_icloud_request_does_not_retry_http_421():
    from icloud_hme import ICloudHME

    client = ICloudHME({}, verbose=False)
    calls = []

    class FakeResp:
        ok = False
        status_code = 421
        text = '{"success":false,"trustTokens":[]}'

    def fake_request(*_args, **_kwargs):
        calls.append(1)
        return FakeResp()

    client.session.request = fake_request
    try:
        client._request("POST", "https://setup.icloud.com/setup/ws/1/validate")
        raise AssertionError("HTTP 421 should raise")
    except RuntimeError as exc:
        assert "421" in str(exc)
    assert len(calls) == 1
    print("  PASS test_icloud_request_does_not_retry_http_421")


def test_create_aliases_stops_after_first_failure():
    import account_manager
    import icloud_hme

    class FakeHME:
        calls = 0

        def __init__(self, *_args, **_kwargs):
            pass

        def create_alias(self, **_kwargs):
            type(self).calls += 1
            raise RuntimeError('generate failed: HTTP 421: {"trustTokens":[]}')

    originals = (
        account_manager.ACCOUNTS_FILE,
        account_manager.LATEST_EMAILS,
        icloud_hme.ICloudHME,
    )
    with tempfile.TemporaryDirectory() as td:
        try:
            account_manager.ACCOUNTS_FILE = Path(td) / "accounts.json"
            account_manager.LATEST_EMAILS = Path(td) / "latest_emails.txt"
            icloud_hme.ICloudHME = FakeHME
            manager = account_manager.AccountManager()
            manager.accounts["acc"] = {
                "id": "acc", "name": "didj", "status": "active",
                "cookies": {}, "host": "icloud.com",
                "alias_total": 0, "alias_active": 0,
            }
            results = manager.create_aliases_for_account("acc", count=8)
            assert len(results) == 1
            assert results[0]["ok"] is False
            assert results[0]["retryable"] is True
            assert FakeHME.calls == 1
        finally:
            (
                account_manager.ACCOUNTS_FILE,
                account_manager.LATEST_EMAILS,
                icloud_hme.ICloudHME,
            ) = originals
    print("  PASS test_create_aliases_stops_after_first_failure")


def test_batch_create_emits_heartbeat_while_apple_call_hangs():
    import web_ui

    started = threading.Event()
    release = threading.Event()

    class FakeManager:
        def get_account(self, account_id):
            return {"id": "slow", "name": "didjndndn@163.com", "status": "active"}

        def create_aliases_for_account(self, account_id, count, _label, **_kwargs):
            started.set()
            if not release.wait(2):
                raise TimeoutError("test hung")
            return [{
                "ok": True,
                "email": "alive@icloud.com",
                "account_id": account_id,
            } for _ in range(count)]

    original_manager = web_ui._account_mgr
    original_state_file = web_ui._BATCH_STATE_FILE
    original_heartbeat = web_ui._BATCH_CREATE_HEARTBEAT_SECONDS
    original_delay = web_ui._BATCH_RETRY_DELAY_SECONDS
    temp_dir = tempfile.TemporaryDirectory()
    web_ui._BATCH_STATE_FILE = Path(temp_dir.name) / "batch_jobs.json"
    web_ui._BATCH_CREATE_HEARTBEAT_SECONDS = 0.05
    web_ui._BATCH_RETRY_DELAY_SECONDS = 0.01
    with web_ui._batch_lock:
        original_jobs = web_ui._batch_jobs
        original_active = web_ui._batch_active_id
        web_ui._batch_jobs = web_ui.OrderedDict()
        web_ui._batch_active_id = None
    web_ui._account_mgr = FakeManager()
    client = web_ui.app.test_client()
    try:
        started_job = client.post("/api/create-batch", json={
            "account_ids": ["slow"],
            "count_per_account": 1,
            "interval": 0,
        })
        assert started_job.status_code == 202
        assert started.wait(1)
        deadline = time.time() + 1
        saw_heartbeat = False
        while time.time() < deadline:
            with web_ui._log_condition:
                msgs = [entry["msg"] for entry in web_ui._log_entries]
            if any("didjndndn@163.com" in msg and "Apple" in msg for msg in msgs):
                saw_heartbeat = True
                break
            time.sleep(0.02)
        release.set()
        job_id = started_job.get_json()["job_id"]
        done_deadline = time.time() + 3
        while time.time() < done_deadline:
            job = client.get("/api/create-batch/%s" % job_id).get_json()["job"]
            if job["status"] not in ("queued", "running"):
                break
            time.sleep(0.02)
        assert saw_heartbeat
        assert job["status"] == "completed"
        source = Path(web_ui.__file__).read_text(encoding="utf-8")
        assert "stop_heartbeat" in source
        assert "_BATCH_CREATE_HEARTBEAT_SECONDS" in source
    finally:
        release.set()
        web_ui._account_mgr = original_manager
        web_ui._BATCH_STATE_FILE = original_state_file
        web_ui._BATCH_CREATE_HEARTBEAT_SECONDS = original_heartbeat
        web_ui._BATCH_RETRY_DELAY_SECONDS = original_delay
        with web_ui._batch_lock:
            web_ui._batch_jobs = original_jobs
            web_ui._batch_active_id = original_active
        temp_dir.cleanup()
    print("  PASS test_batch_create_emits_heartbeat_while_apple_call_hangs")


if __name__ == "__main__":
    tests = [
        ("parse_cookie_header_string", test_parse_cookie_header_string),
        ("parse_cookie_json", test_parse_cookie_json),
        ("parse_empty_input", test_parse_empty_input),
        ("derive_icloud_email_primary", test_derive_icloud_email_primary),
        ("derive_icloud_email_appleid_is_icloud", test_derive_icloud_email_appleid_is_icloud),
        ("derive_icloud_email_third_party", test_derive_icloud_email_third_party),
        ("mail_cache_basic", test_mail_cache_basic),
        ("mail_newest_first", test_mail_newest_first),
        ("pickup_rebind_deduplicates_and_revokes", test_pickup_rebind_deduplicates_and_revokes),
        ("export_history_is_persistent_and_idempotent", test_export_history_is_persistent_and_idempotent),
        ("validate_skips_lock_when_create_in_progress", test_validate_skips_lock_when_create_in_progress),
        ("account_api_reports_validation_failure", test_account_api_reports_validation_failure),
        ("export_api_prevents_duplicate_downloads", test_export_api_prevents_duplicate_downloads),
        ("scheduler_start_is_idempotent", test_scheduler_start_is_idempotent),
        ("strip_html", test_strip_html),
        ("strip_html_with_link", test_strip_html_with_link),
        ("icloud_hme_account_info", test_icloud_hme_account_info),
        ("create_alias_stops_retrying_on_address_limit", test_create_alias_stops_retrying_on_address_limit),
        ("async_batch_skips_limited_account_and_continues", test_async_batch_skips_limited_account_and_continues),
        ("async_batch_retries_temporary_limit_after_cooldown", test_async_batch_retries_temporary_limit_after_cooldown),
        ("batch_retries_every_minute_after_repeated_limit", test_batch_retries_every_minute_after_repeated_limit),
        ("email_api_uses_saved_and_pickup_creation_times", test_email_api_uses_saved_and_pickup_creation_times),
        ("pickup_links_api_does_not_call_icloud", test_pickup_links_api_does_not_call_icloud),
        ("runtime_logs_persist_replay_and_resume", test_runtime_logs_persist_replay_and_resume),
        ("batch_state_persists_and_resumes_remaining_count", test_batch_state_persists_and_resumes_remaining_count),
        ("invalid_imap_credentials_are_not_persisted", test_invalid_imap_credentials_are_not_persisted),
        ("fetch_full_uses_single_imap_fetch", test_fetch_full_uses_single_imap_fetch),
        ("manual_create_rejects_invalid_counts", test_manual_create_rejects_invalid_counts),
        ("mail_body_store_persists_and_prunes", test_mail_body_store_persists_and_prunes),
        ("account_creation_is_paced_between_aliases", test_account_creation_is_paced_between_aliases),
        ("pickup_uses_persistent_body_and_deduplicates_sync", test_pickup_uses_persistent_body_and_deduplicates_sync),
        ("batch_runs_accounts_in_parallel_and_creates_pickup_links", test_batch_runs_accounts_in_parallel_and_creates_pickup_links),
        ("stale_account_storage_rebinds_without_duplicates", test_stale_account_storage_rebinds_without_duplicates),
        ("pickup_page_uses_adaptive_refresh", test_pickup_page_uses_adaptive_refresh),
        ("export_actions_respect_visible_filter_and_mobile_controls", test_export_actions_respect_visible_filter_and_mobile_controls),
        ("account_alias_sync_is_parallel_and_reports_failures", test_account_alias_sync_is_parallel_and_reports_failures),
        ("manual_create_conflict_and_input_status_codes", test_manual_create_conflict_and_input_status_codes),
        ("remove_account_purges_all_local_data", test_remove_account_purges_all_local_data),
        ("alias_api_returns_partial_failure_details", test_alias_api_returns_partial_failure_details),
        ("public_bind_requires_admin_token", test_public_bind_requires_admin_token),
        ("scheduler_skips_accounts_with_active_create", test_scheduler_skips_accounts_with_active_create),
        ("ui_create_entry_and_scheduler_copy", test_ui_create_entry_and_scheduler_copy),
        ("parse_cookie_editor_array", test_parse_cookie_editor_array),
        ("record_known_aliases_and_failed_add", test_record_known_aliases_and_failed_add),
        ("find_by_recipient_matches_delivered_to", test_find_by_recipient_matches_delivered_to),
        ("check_all_aliases_mail_raises_without_cache", test_check_all_aliases_mail_raises_without_cache),
        ("health_loop_retries_error_accounts", test_health_loop_retries_error_accounts),
        ("batch_pause_keeps_remaining_and_stop_discards_it", test_batch_pause_keeps_remaining_and_stop_discards_it),
        ("create_control_stop_cancels_remaining", test_create_control_stop_cancels_remaining),
        ("reimport_keeps_account_and_rejects_mismatch", test_reimport_keeps_account_and_rejects_mismatch),
        ("reimport_api_and_expired_account_button", test_reimport_api_and_expired_account_button),
        ("batch_can_add_second_account_while_first_is_running", test_batch_can_add_second_account_while_first_is_running),
        ("icloud_request_does_not_retry_http_421", test_icloud_request_does_not_retry_http_421),
        ("create_aliases_stops_after_first_failure", test_create_aliases_stops_after_first_failure),
        ("batch_create_emits_heartbeat_while_apple_call_hangs", test_batch_create_emits_heartbeat_while_apple_call_hangs),
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
