"""Focused tests for durable state, process coordination, and backups."""

import importlib.util
import json
import os
import sqlite3
import tarfile
import threading
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[1]


def _load_backup_module():
    spec = importlib.util.spec_from_file_location("backup_persistence_audit", ROOT / "ops" / "backup.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_durable_json_uses_unique_temps_and_preserves_unicode_errors(tmp_path):
    import durable_json

    target = tmp_path / "state.json"
    errors = []

    def write(value):
        try:
            durable_json.write_object(target, {"value": value})
        except Exception as exc:  # pragma: no cover - diagnostic guard
            errors.append(exc)

    threads = [threading.Thread(target=write, args=(n,)) for n in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors
    assert json.loads(target.read_text(encoding="utf-8"))["value"] in range(12)
    assert not list(tmp_path.glob(".*.tmp"))

    target.write_bytes(b"\xff\xfe")
    with pytest.raises(RuntimeError, match="编码损坏"):
        durable_json.read_object(target)
    assert target.read_bytes() == b"\xff\xfe"


def test_process_lock_is_exclusive_and_reusable(tmp_path):
    from process_lock import LockAlreadyHeld, ProcessLock

    path = tmp_path / "service.lock"
    first = ProcessLock(path)
    second = ProcessLock(path)
    first.acquire()
    try:
        with pytest.raises(LockAlreadyHeld):
            second.acquire()
    finally:
        first.release()
    second.acquire()
    second.release()
    assert path.exists()  # diagnostic lock files are intentionally retained


def test_scheduler_state_loader_fails_closed_and_writes_atomically(tmp_path, monkeypatch):
    import scheduler

    state = tmp_path / "scheduler_state.json"
    monkeypatch.setattr(scheduler, "STATE_FILE", state)
    assert scheduler.load_state() == {"total_created": 0, "rounds": [], "last_error": None}
    scheduler.save_state({"total_created": 2, "rounds": [], "last_error": None})
    assert json.loads(state.read_text(encoding="utf-8"))["total_created"] == 2
    state.write_text('{"rounds": "not-a-list"}', encoding="utf-8")
    with pytest.raises(RuntimeError, match="结构损坏"):
        scheduler.load_state()
    assert state.read_text(encoding="utf-8") == '{"rounds": "not-a-list"}'


def test_backup_manifest_records_maintenance_lock_and_excludes_lock_file(tmp_path):
    backup = _load_backup_module()
    root = tmp_path / "source"
    project = root / "root" / "iCloud"
    results = project / "results"
    results.mkdir(parents=True)
    (project / "accounts.json").write_text('{"accounts":{}}', encoding="utf-8")
    (results / "creation_guard.json").write_text('{"accounts":{}}', encoding="utf-8")
    (results / "icloud-hme.maintenance.lock").write_text("diagnostic", encoding="utf-8")
    db = sqlite3.connect(results / "mail_bodies.sqlite3")
    try:
        db.execute("CREATE TABLE sample(value)")
        db.execute("INSERT INTO sample VALUES (42)")
        db.commit()
    finally:
        db.close()
    output = backup.run(root, tmp_path / "backups")
    with tarfile.open(output) as archive:
        names = archive.getnames()
        manifest = json.load(archive.extractfile("backup-manifest.json"))
        assert "_snapshot" in manifest
        assert manifest["_snapshot"]["consistency"] == "maintenance-lock"
        assert not any(name.endswith("icloud-hme.maintenance.lock") for name in names)


def test_deploy_script_covers_full_validation_and_optional_workflow():
    script = (ROOT / "deploy" / "install-production.sh").read_text(encoding="utf-8")
    assert "python -m compileall -q ." in script or '"$PYTHON_BIN" -m compileall -q .' in script
    assert '"$PYTHON_BIN" -m pytest -q' in script
    assert "build_source_manifest" in script
    assert "rsync -a --delete" in script
    assert 'if [[ -f "$SOURCE_DIR/.github/workflows/tests.yml" ]]' in script
