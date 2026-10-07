#!/usr/bin/env python3
"""Snapshot application state, SQLite and proxy dependencies without printing secrets."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import sys
import tarfile
import tempfile
import time
import uuid
from contextlib import closing

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
from process_lock import LockAlreadyHeld, maintenance_snapshot_lock


CONFIG_PATHS = [
    'root/.icloud-hme-admin-token', 'root/.icloud-hme-admin-env',
    'root/icloud-proxy',
    'etc/systemd/system/icloud-hme.service',
    'etc/systemd/system/icloud-hme.service.d',
    'etc/systemd/system/icloud-outbound-proxy.service',
    'etc/systemd/system/icloud-hme-healthcheck.service',
    'etc/systemd/system/icloud-hme-healthcheck.timer',
    'etc/systemd/system/icloud-hme-backup.service',
    'etc/systemd/system/icloud-hme-backup.timer',
    'usr/local/sbin/icloud-hme-backup',
    'www/server/panel/vhost/nginx/icloud-pickup.conf',
    'www/server/panel/vhost/nginx/00-icloud-security-zones.conf',
    'www/server/nginx/conf/icloud-security-headers.conf',
    'etc/ssh/sshd_config.d/99-icloud-hardening.conf',
    'etc/fail2ban/jail.d/icloud-hardening.local',
]


def _fsync_directory(directory):
    if os.name == 'nt':
        return
    fd = None
    try:
        fd = os.open(str(directory), os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
        os.fsync(fd)
    except OSError:
        return
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass


def _atomic_write(path, payload, mode=0o600):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(
        f'.{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp'
    )
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        with os.fdopen(fd, 'wb') as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        _fsync_directory(path.parent)
    except Exception:
        try:
            tmp.unlink()
        except (FileNotFoundError, OSError):
            pass
        raise


def copy_file(source, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    data = source.read_bytes()
    if source.suffix == '.json':
        json.loads(data)
    target.write_bytes(data)
    target.chmod(0o700 if source.stat().st_mode & 0o111 else 0o600)


def snapshot_database(source, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + 120
    def progress(*_):
        if time.monotonic() > deadline:
            raise TimeoutError('SQLite backup exceeded time limit')
    src = sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)
    dst = sqlite3.connect(target)
    try:
        src.backup(dst, pages=256, progress=progress, sleep=0.05)
        if dst.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise RuntimeError('SQLite backup integrity check failed')
    finally:
        dst.close()
        src.close()
    target.chmod(0o600)


def make_snapshot(root, stage):
    project = root / 'root/iCloud'
    # Progress first, journal second, ledger last: a restored journal can repair
    # stale progress; an acknowledged completion must already exist in the ledger.
    ordered = ['.credentials.key', 'results/batch_jobs.json', 'results/creation_guard.json',
               'results/latest_emails.txt', 'accounts.json']
    copied = set()
    for rel in ordered:
        source = project / rel
        if source.exists():
            copy_file(source, stage / 'root/iCloud' / rel)
            copied.add(source)
    sources = []
    for folder, dirs, names in os.walk(project):
        dirs[:] = [d for d in dirs if d not in {'.git', '.venv', '__pycache__', '.pytest_cache', 'logs'}
                   and not (Path(folder) / d).is_symlink()]
        sources.extend(Path(folder) / name for name in names)
    for source in sources:
        if source in copied or source.is_symlink() or not source.is_file() or source.name.endswith(('.tmp', '-wal', '-shm', '.lock', '.pyc')):
            continue
        target = stage / source.relative_to(root)
        if source.suffix in ('.sqlite3', '.db'):
            snapshot_database(source, target)
        else:
            copy_file(source, target)
    for rel in CONFIG_PATHS:
        source = root / rel
        if not source.exists():
            continue
        files = source.rglob('*') if source.is_dir() else [source]
        for item in files:
            if item.is_file() and not item.is_symlink() and not item.name.endswith('.tmp'):
                copy_file(item, stage / item.relative_to(root))


def verify_snapshot(stage):
    """Verify a restore without loading web_ui or emitting any credentials."""
    project = Path(stage) / 'root/iCloud'
    account_file = project / 'accounts.json'
    accounts = json.loads(account_file.read_text(encoding='utf-8')).get('accounts', {}) if account_file.exists() else {}
    if not isinstance(accounts, dict):
        raise RuntimeError('Backup account schema invalid')
    from credential_store import unseal
    for acc_id, account in accounts.items():
        if 'credentials_encrypted' in account:
            unseal(acc_id, account['credentials_encrypted'], project / '.credentials.key')
    databases = 0
    for path in project.rglob('*'):
        if path.is_file() and path.suffix in ('.sqlite3', '.db'):
            with closing(sqlite3.connect(path.as_uri() + '?mode=ro&immutable=1', uri=True)) as db:
                if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                    raise RuntimeError('Restored SQLite integrity check failed')
            databases += 1
    return {'accounts': len(accounts), 'databases': databases, 'credentials_verified': True}


def run(root=Path('/'), dest=Path('/var/backups/icloud-hme')):
    root = Path(root)
    dest = Path(dest)
    os.umask(0o077)
    dest.mkdir(parents=True, exist_ok=True, mode=0o700)
    project = root / 'root/iCloud'
    lock = maintenance_snapshot_lock(project)
    try:
        lock.acquire()
    except LockAlreadyHeld:
        raise RuntimeError(
            '应用仍在写入状态；请停止 icloud-hme 或等待另一个备份完成后再试'
        ) from None
    snapshot_started = time.time()
    try:
        with tempfile.TemporaryDirectory(prefix='.snapshot-', dir=dest) as td:
            stage = Path(td)
            make_snapshot(root, stage)
            verification = verify_snapshot(stage)
            manifest = {
                f.relative_to(stage).as_posix(): hashlib.sha256(f.read_bytes()).hexdigest()
                for f in stage.rglob('*') if f.is_file()
            }
            manifest['_snapshot'] = {
                'schema_version': 2,
                'consistency': 'service-lock-offline',
                'restore_verification': verification,
                'lock_path': str(lock.path),
                'started_at': snapshot_started,
                'finished_at': time.time(),
            }
            _atomic_write(
                stage / 'backup-manifest.json',
                json.dumps(manifest, ensure_ascii=False, sort_keys=True).encode('utf-8'),
            )
            name = 'icloud-hme-' + time.strftime('%Y%m%d-%H%M%S') + '.tar.gz'
            tmp = dest / f'.{name}.{os.getpid()}.{uuid.uuid4().hex}.tmp'
            try:
                with open(tmp, 'wb') as output:
                    with tarfile.open(fileobj=output, mode='w:gz') as archive:
                        for item in stage.iterdir():
                            archive.add(item, arcname=item.name)
                    output.flush()
                    os.fsync(output.fileno())
                target = dest / name
                os.replace(tmp, target)
                _fsync_directory(dest)
            finally:
                try:
                    tmp.unlink()
                except (FileNotFoundError, OSError):
                    pass
            digest = hashlib.sha256(target.read_bytes()).hexdigest()
            _atomic_write(
                target.with_suffix(target.suffix + '.sha256'),
                (digest + '  ' + target.name + '\n').encode('utf-8'),
            )
    finally:
        lock.release()
    for old in dest.glob('icloud-hme-*.tar.gz*'):
        if old.is_file() and not old.is_symlink() and old.stat().st_mtime < time.time() - 14 * 86400:
            old.unlink()
    print('Backup completed and SQLite integrity verified:', target.name)
    return target


def verify_archive(path):
    """Restore a backup into a disposable directory and verify all file hashes."""
    with tempfile.TemporaryDirectory(prefix='icloud-restore-check-') as td:
        stage = Path(td)
        with tarfile.open(path, 'r:gz') as archive:
            for member in archive:
                rel = PurePosixPath(member.name)
                if rel.is_absolute() or '..' in rel.parts or ':' in member.name or '\\' in member.name:
                    raise RuntimeError('Unsafe backup member path')
                target = stage.joinpath(*rel.parts)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                elif member.isfile():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with archive.extractfile(member) as src, target.open('wb') as out:
                        shutil.copyfileobj(src, out)
                    target.chmod(0o600)
                else:
                    raise RuntimeError('Backup links and special files are not supported')
        manifest = json.loads((stage / 'backup-manifest.json').read_text())
        actual = {p.relative_to(stage).as_posix() for p in stage.rglob('*') if p.is_file()}
        expected = {name for name in manifest if name != '_snapshot'}
        if actual != expected | {'backup-manifest.json'}:
            raise RuntimeError('Backup manifest membership mismatch')
        for name in expected:
            digest = hashlib.sha256()
            with (stage / name).open('rb') as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                    digest.update(chunk)
            if digest.hexdigest() != manifest[name]:
                raise RuntimeError('Backup manifest hash mismatch')
        return verify_snapshot(stage)


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--verify-archive':
        print(json.dumps(verify_archive(sys.argv[2]), sort_keys=True))
    elif len(sys.argv) == 2 and sys.argv[1] == '--verified-backup':
        target = run()
        print(json.dumps(verify_archive(target), sort_keys=True))
    elif len(sys.argv) == 1:
        run()
    else:
        raise SystemExit('usage: backup.py [--verified-backup | --verify-archive PATH]')
