import threading
from datetime import datetime, timedelta


def test_pending_batch_entries_skip_stale_ids_and_future_cooldowns():
    import web_ui

    future = (datetime.now(web_ui._BJ_TZ) + timedelta(minutes=5)).isoformat()
    job = {
        "account_ids": ["missing", "waiting", "due", "done"],
        "accounts": {
            "waiting": {"status": "waiting", "retry_at": future},
            "due": {"status": "waiting", "retry_at": "not-a-date"},
            "done": {"status": "completed", "finished_at": "now"},
        },
    }
    assert web_ui._pending_batch_account_ids(job) == ["due"]


def test_requeue_clears_transient_guard_block(monkeypatch):
    import web_ui

    calls = []

    class Guard:
        def reset_task_account(self, task_id, acc_id):
            calls.append(("reset", task_id, acc_id))

        def unblock(self, acc_id):
            calls.append(("unblock", acc_id))

        def snapshot(self):
            return {"accounts": {"acc": {"blocked": "network"}}}

    class Manager:
        creation_guard = Guard()

        @staticmethod
        def get_account(_acc_id):
            return {"name": "test"}

    monkeypatch.setattr(web_ui, "_account_mgr", Manager())
    job = {"id": "job"}
    replacement = web_ui._requeue_finished_batch_account(
        job, "acc", 10, {"created": 4}
    )
    assert replacement["target"] == 14
    assert calls == [("reset", "job", "acc"), ("unblock", "acc")]


def test_requeue_preserves_auth_guard_block(monkeypatch):
    import web_ui

    calls = []

    class Guard:
        def reset_task_account(self, *_args):
            calls.append("reset")

        def unblock(self, *_args):
            calls.append("unblock")

        def snapshot(self):
            return {"accounts": {"acc": {"blocked": "auth"}}}

    class Manager:
        creation_guard = Guard()

        @staticmethod
        def get_account(_acc_id):
            return {"name": "test"}

    monkeypatch.setattr(web_ui, "_account_mgr", Manager())
    web_ui._requeue_finished_batch_account({"id": "job"}, "acc", 1, {"created": 0})
    assert calls == ["reset"]


def test_resume_nonexistent_account_is_not_reported_as_success(monkeypatch):
    import web_ui

    monkeypatch.setattr(web_ui._account_mgr, "get_account", lambda _acc_id: None)
    assert web_ui._resume_account_create("deleted-account") is False


def test_release_guard_requeues_finished_auth_account(monkeypatch):
    import web_ui

    class FakeGuard:
        def __init__(self):
            self.lock = threading.RLock()
            self.data = {"accounts": {}}
            self.unblocked = []

        def prune_accounts(self, _ids):
            return 0

        def unblock(self, acc_id=None):
            self.unblocked.append(acc_id)

        def reset_task_account(self, _task_id, _acc_id):
            return True

        def snapshot(self):
            return {"accounts": {}, "settings": {}, "events": []}

    class FakeManager:
        def __init__(self, guard):
            self.creation_guard = guard

        def list_accounts(self):
            return [{"id": "acc"}]

        def get_account(self, acc_id):
            return {"id": acc_id, "name": "test", "create_status": "available"}

        def update_account(self, *_args, **_kwargs):
            return None

    guard = FakeGuard()
    job = {
        "id": "job",
        "status": "running",
        "account_ids": ["acc"],
        "total_created": 5,
        "total_errors": 1,
        "accounts": {
            "acc": {
                "account_id": "acc",
                "name": "test",
                "status": "partial",
                "created": 5,
                "errors": 1,
                "target": 300,
                "error": "创建已暂停：auth，请检查后解除",
                "finished_at": "2026-10-06T00:00:00+08:00",
            }
        },
    }
    monkeypatch.setattr(web_ui, "_account_mgr", FakeManager(guard))
    monkeypatch.setattr(web_ui, "_batch_jobs", {"job": job})
    monkeypatch.setattr(web_ui, "_batch_active_id", "job")
    monkeypatch.setattr(web_ui, "_batch_runner_jobs", {"job"})
    monkeypatch.setattr(web_ui, "_save_batch_state_locked", lambda: None)
    monkeypatch.setattr(web_ui, "_emit_log", lambda *_args, **_kwargs: None)

    response = web_ui.app.test_client().post(
        "/api/creation-guard", json={"action": "release", "account_id": "acc"}
    )

    assert response.status_code == 200
    assert response.get_json()["requeued"] is True
    entry = job["accounts"]["acc"]
    assert guard.unblocked == ["acc"]
    assert entry["status"] == "queued"
    assert entry["created"] == 5
    assert entry["target"] == 300
    assert entry["errors"] == 0
    assert entry["error"] == ""
    assert entry["finished_at"] is None
