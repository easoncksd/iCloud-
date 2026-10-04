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
