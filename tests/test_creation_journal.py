import pytest
from .test_creation_guard import manager


def test_durable_completion_repairs_lost_batch_progress(manager, monkeypatch, tmp_path):
    import web_ui as w
    import icloud_hme
    monkeypatch.setattr(w, '_account_mgr', manager)
    monkeypatch.setattr(w, '_BATCH_STATE_FILE', tmp_path / 'batch.json')
    job = {'id': 'job-a', 'accounts': {'a': {'created': 2}}}
    w._sync_creation_journal(job, 'a')
    def create(client, **kwargs):
        client.before_reserve('new@icloud.com')
        return {'email': 'new@icloud.com'}
    monkeypatch.setattr(icloud_hme.ICloudHME, 'create_alias', create)
    def failed_progress(result):
        raise OSError('simulated interrupted progress write')
    failed_progress.task_id = job['id']
    results = manager.create_aliases_for_account('a', 2, progress_callback=failed_progress)
    assert len([r for r in results if r['ok']]) == 1
    assert results[-1]['error_kind'] == 'local'
    # Reopen the journal to prove recovery depends on disk, not callbacks.
    from create_guard import CreationGuard
    manager._creation_guard = CreationGuard(manager.creation_guard.path)
    w._sync_creation_journal(job, 'a')
    assert job['accounts']['a']['created'] == 3
    w._sync_creation_journal(job, 'a')
    assert job['accounts']['a']['created'] == 3


def test_completion_journal_failure_keeps_pending(manager, monkeypatch):
    guard = manager.creation_guard
    guard.pending('a', 'candidate@icloud.com', 'task')
    def fail(): raise OSError('disk failure')
    monkeypatch.setattr(guard, 'save', fail)
    with pytest.raises(OSError):
        guard.success('a', 'candidate@icloud.com', 'task')
    assert guard.snapshot()['accounts']['a']['pending'] == 'candidate@icloud.com'
    assert guard.task_count('task', 'a') == 0


def test_requeue_finished_account_starts_a_new_incremental_segment(manager, monkeypatch):
    import web_ui as w

    monkeypatch.setattr(w, '_account_mgr', manager)
    guard = manager.creation_guard
    guard.pending('a', 'old@icloud.com', 'job')
    guard.success('a', 'old@icloud.com', 'job')
    job = {'id': 'job', 'accounts': {'a': {'created': 20}}}

    replacement = w._requeue_finished_batch_account(
        job, 'a', 1, {'created': 20, 'status': 'partial', 'finished_at': 'done'}
    )

    assert replacement['created'] == 20
    assert replacement['target'] == 21
    assert replacement['journal_base_created'] == 20
    assert guard.task_count('job', 'a') == 0


def test_duplicate_completion_is_idempotent(manager):
    guard = manager.creation_guard
    guard.pending('a', 'candidate@icloud.com', 'task')
    guard.success('a', 'candidate@icloud.com', 'task')
    guard.success('a', 'candidate@icloud.com', 'task')
    assert guard.task_count('task', 'a') == 1


def test_old_header_only_body_is_treated_as_missing(tmp_path):
    from mail_body_store import MailBodyStore
    store = MailBodyStore(tmp_path / 'body.sqlite3')
    try:
        store.put('a', '1', {'subject': 'old invalid entry'})
        assert not store.contains('a', '1')
        assert store.get('a', '1') is None
        store.put('a', '1', {'body': ''})
        assert store.contains('a', '1')
    finally:
        store.close()


@pytest.mark.parametrize('name', ['pickup', 'export'])
def test_corrupt_authoritative_store_is_not_overwritten(tmp_path, name):
    from pickup_links import PickupLinkStore
    from export_history import ExportHistoryStore
    path = tmp_path / 'state.json'
    path.write_text('{broken', encoding='utf-8')
    with pytest.raises(RuntimeError):
        (PickupLinkStore if name == 'pickup' else ExportHistoryStore)(path)
    assert path.read_text(encoding='utf-8') == '{broken'

