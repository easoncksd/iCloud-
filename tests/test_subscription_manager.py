import json
import os


def test_subscription_is_private_and_not_returned(monkeypatch, tmp_path):
    import subscription_manager as manager
    monkeypatch.setattr(manager, 'SUBSCRIPTION_FILE', tmp_path / 'subscription.url')
    monkeypatch.setattr(manager, 'META_FILE', tmp_path / 'subscription.meta.json')
    result = manager.save('https://provider.example/sub?token=secret-token')
    assert result['configured'] is True
    assert result['host'] == 'provider.example'
    assert 'secret-token' not in json.dumps(result)
    assert (tmp_path / 'subscription.url').read_text(encoding='utf-8').strip().endswith('secret-token')
    if os.name != 'nt':
        assert os.stat(tmp_path / 'subscription.url').st_mode & 0o777 == 0o600


def test_subscription_requires_https(monkeypatch, tmp_path):
    import subscription_manager as manager
    monkeypatch.setattr(manager, 'SUBSCRIPTION_FILE', tmp_path / 'subscription.url')
    monkeypatch.setattr(manager, 'META_FILE', tmp_path / 'subscription.meta.json')
    for url in ('http://provider.example/sub', 'not-a-url', 'https://'):
        try:
            manager.save(url)
        except ValueError:
            pass
        else:
            raise AssertionError('invalid subscription URL was accepted')
