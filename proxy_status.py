"""Read-only, credential-free view of the local automatic proxy service."""
import json
from pathlib import Path
from urllib.request import Request, urlopen

PROXY_DIR = Path('/root/icloud-proxy')


def read_status():
    if not (PROXY_DIR / 'config.yaml').exists():
        return {'installed': False, 'running': False, 'enabled': False}
    try:
        import yaml
        from network_proxy import load_config
        config = yaml.safe_load((PROXY_DIR / 'config.yaml').read_text(encoding='utf-8'))
        group_config = next(g for g in config['proxy-groups'] if g['name'] == 'AUTO')
        # Only contact the fixed local controller, never a subscription-supplied URL.
        req = Request('http://127.0.0.1:17891/proxies',
                      headers={'Authorization': 'Bearer ' + config['secret']})
        with urlopen(req, timeout=3) as response:
            proxies = json.load(response)['proxies']
        group = proxies['AUTO']
        labels_path = PROXY_DIR / 'labels.json'
        labels = json.loads(labels_path.read_text(encoding='utf-8')) if labels_path.exists() else {}
        selected = group.get('now')
        def entry(name):
            history = proxies.get(name, {}).get('history') or []
            latest = history[-1] if history else {}
            return {'name': labels.get(name, name), 'selected': name == selected,
                    'delay_ms': latest.get('delay'), 'checked_at': latest.get('time'),
                    'alive': proxies.get(name, {}).get('alive')}
        route = load_config()
        enabled = (route['mode'] == 'proxy' and route['host'] in ('127.0.0.1', 'localhost')
                   and route['port'] == config['mixed-port'])
        return {'installed': True, 'running': True, 'enabled': enabled,
                'automatic': group.get('type') == 'URLTest',
                'current': entry(selected) if selected else None,
                'nodes': [entry(n) for n in group.get('all', [])],
                'interval_seconds': group_config.get('interval'),
                'timeout_ms': group_config.get('timeout'),
                'tolerance_ms': group_config.get('tolerance')}
    except Exception:
        return {'installed': True, 'running': False, 'enabled': False,
                'error': '自动代理状态暂时不可用，请刷新或检查代理服务'}
