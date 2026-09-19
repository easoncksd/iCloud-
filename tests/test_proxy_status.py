import io
import json


def test_status_only_exposes_display_fields(tmp_path, monkeypatch):
    import proxy_status
    import network_proxy
    monkeypatch.setattr(proxy_status, 'PROXY_DIR', tmp_path)
    (tmp_path / 'config.yaml').write_text(json.dumps({'secret': 'hidden-controller-secret', 'mixed-port': 17890,
        'proxy-groups': [{'name': 'AUTO', 'interval': 60, 'timeout': 3000, 'tolerance': 100}]}))
    (tmp_path / 'labels.json').write_text(json.dumps({'n1': 'Hong Kong 1'}))
    monkeypatch.setattr(network_proxy, 'load_config', lambda: {'mode': 'proxy', 'host': '127.0.0.1', 'port': 17890})
    payload = {'proxies': {'AUTO': {'type': 'URLTest', 'now': 'n1', 'all': ['n1']},
                          'n1': {'password': 'hidden-node-secret', 'alive': True, 'history': [{'delay': 55, 'time': 'now'}]}}}
    monkeypatch.setattr(proxy_status, 'urlopen', lambda *a, **k: io.BytesIO(json.dumps(payload).encode()))
    status = proxy_status.read_status()
    assert status['enabled'] and status['automatic']
    assert status['current']['name'] == 'Hong Kong 1'
    assert status['current']['delay_ms'] == 55
    assert 'hidden' not in json.dumps(status)


def test_status_unavailable_does_not_expose_controller_error(tmp_path, monkeypatch):
    import proxy_status
    monkeypatch.setattr(proxy_status, 'PROXY_DIR', tmp_path)
    assert not proxy_status.read_status()['installed']
    (tmp_path / 'config.yaml').write_text('invalid')
    assert not proxy_status.read_status()['running']
