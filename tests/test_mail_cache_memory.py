"""Cache persistence must stay bounded and preserve the last durable file."""

import json
import tracemalloc

import pytest

import mail_cache


def test_large_cache_save_has_bounded_extra_memory(tmp_path, monkeypatch):
    path = tmp_path / "mail_cache.json"
    monkeypatch.setattr(mail_cache, "CACHE_FILE", path)
    cache = mail_cache.MailCache()
    messages = [{"id": str(n), "subject": "中文邮件标题" * 40}
                for n in range(4000)]
    cache._data = {"account": {"inbox_emails": messages, "alias_emails": {}}}
    tracemalloc.start()
    try:
        cache._save()
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < path.stat().st_size // 2
    assert json.loads(path.read_text(encoding="utf-8")) == cache._data
    assert mail_cache.MailCache().get_inbox("account") == messages


def test_failed_cache_save_preserves_previous_file(tmp_path, monkeypatch):
    path = tmp_path / "mail_cache.json"
    monkeypatch.setattr(mail_cache, "CACHE_FILE", path)
    cache = mail_cache.MailCache()
    cache._save()
    before = path.read_bytes()
    cache._data = {"unserializable": object()}
    with pytest.raises(TypeError):
        cache._save()
    assert path.read_bytes() == before
    assert not path.with_suffix(".json.tmp").exists()
