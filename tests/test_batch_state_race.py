def test_batch_worker_skips_missing_account_state(monkeypatch):
    import web_ui

    job = {
        "id": "race-job",
        "completed_accounts": 0,
        "accounts": {},
    }

    # A stale task may still contain an account id after its state entry was
    # lost during a restart or account removal.  The worker must not take down
    # the whole batch thread with KeyError.
    assert web_ui._run_batch_account(job, "removed-account", 1, "") == 0


def test_batch_worker_failure_is_recorded_per_account(monkeypatch, tmp_path):
    import web_ui

    monkeypatch.setattr(web_ui, "_BATCH_STATE_FILE", tmp_path / "batch.json")
    monkeypatch.setattr(web_ui, "_save_batch_state_locked", lambda: None)
    job = {
        "id": "worker-job",
        "completed_accounts": 0,
        "total_errors": 0,
        "accounts": {
            "acc": {
                "status": "running",
                "created": 0,
                "errors": 0,
                "finished_at": None,
            }
        },
    }

    assert web_ui._mark_batch_account_failed(job, "acc", RuntimeError("boom")) == 1
    assert job["accounts"]["acc"]["status"] == "failed"
    assert job["accounts"]["acc"]["errors"] == 1
    assert job["total_errors"] == 1


def test_deleted_account_is_removed_from_batch_snapshot(monkeypatch, tmp_path):
    import web_ui

    original_jobs = web_ui._batch_jobs
    original_active = web_ui._batch_active_id
    try:
        web_ui._BATCH_STATE_FILE = tmp_path / "batch.json"
        web_ui._batch_jobs = web_ui.OrderedDict({
            "job": {
                "account_ids": ["gone", "keep"],
                "accounts": {
                    "gone": {"created": 10, "errors": 1, "finished_at": "done"},
                    "keep": {"created": 20, "errors": 0, "finished_at": "done"},
                },
                "total_accounts": 2,
                "completed_accounts": 2,
                "total_created": 30,
                "total_errors": 1,
            }
        })
        web_ui._batch_active_id = None

        assert web_ui._remove_account_from_batch_jobs("gone") == 1
        job = web_ui._batch_jobs["job"]
        assert job["account_ids"] == ["keep"]
        assert "gone" not in job["accounts"]
        assert job["total_accounts"] == 1
        assert job["completed_accounts"] == 1
        assert job["total_created"] == 20
        assert job["total_errors"] == 0
        assert "gone" not in web_ui._BATCH_STATE_FILE.read_text(encoding="utf-8")
    finally:
        web_ui._batch_jobs = original_jobs
        web_ui._batch_active_id = original_active
