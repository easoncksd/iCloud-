import importlib.util
import json
from pathlib import Path
import sqlite3
import tarfile


def test_online_backup_contains_proxy_and_consistent_sqlite(tmp_path):
    spec = importlib.util.spec_from_file_location('backup', Path(__file__).parents[1] / 'ops/backup.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    root = tmp_path / 'source'
    results = root / 'root/iCloud/results'
    results.mkdir(parents=True)
    (root / 'root/iCloud/accounts.json').write_text('{"accounts":{}}')
    (results / 'creation_guard.json').write_text('{}')
    proxy = root / 'root/icloud-proxy'
    proxy.mkdir()
    (proxy / 'config.yaml').write_text('test: secret')
    db = sqlite3.connect(results / 'mail_bodies.sqlite3')
    try:
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('CREATE TABLE sample(value)')
        db.execute('INSERT INTO sample VALUES (42)')
        db.commit()
        output = module.run(root, tmp_path / 'backups')
        with tarfile.open(output) as archive:
            assert 'root/icloud-proxy/config.yaml' in archive.getnames()
            assert not any(name.endswith(('-wal', '-shm')) for name in archive.getnames())
            snapshot = tmp_path / 'restored.sqlite3'
            snapshot.write_bytes(archive.extractfile('root/iCloud/results/mail_bodies.sqlite3').read())
            manifest = json.load(archive.extractfile('backup-manifest.json'))
            assert 'root/icloud-proxy/config.yaml' in manifest
        with sqlite3.connect(snapshot) as check:
            assert check.execute('SELECT value FROM sample').fetchone()[0] == 42
    finally:
        db.close()
