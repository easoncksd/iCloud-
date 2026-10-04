"""Strict JSON reads and durable atomic writes for application state.

The files handled here are authoritative state.  A fixed ``.tmp`` sibling is
not sufficient when two processes write the same state file: they can rename
each other's temporary file and leave the next writer with ``ENOENT`` or stale
data.  Temporary names are therefore unique per write, and the containing
directory is fsynced after the replacement on platforms that support it.
"""
import json
import os
import secrets
import threading
import time
from pathlib import Path


def read_object(path):
    path = Path(path)
    try:
        raw = path.read_text(encoding='utf-8')
    except FileNotFoundError:
        return {}
    except OSError:
        raise RuntimeError(f'{path.name} 无法读取，已停止操作以保护原文件') from None
    except UnicodeError:
        raise RuntimeError(f'{path.name} 编码损坏，已停止操作；请从备份恢复') from None
    try:
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise ValueError()
        return result
    except (ValueError, TypeError, UnicodeError):
        raise RuntimeError(f'{path.name} 数据损坏，已停止操作；请从备份恢复') from None


def _temporary_path(path):
    """Return a per-write temporary path beside *path*.

    Keeping the file in the same directory makes ``os.replace`` atomic on the
    same filesystem while the random suffix prevents concurrent writers from
    sharing a temporary path.
    """
    path = Path(path)
    suffix = f'.{os.getpid()}.{threading.get_ident()}.{secrets.token_hex(8)}.tmp'
    return path.with_name(f'.{path.name}{suffix}')


def _fsync_directory(directory):
    """Durably persist a completed rename where the OS exposes directory fsync."""
    if os.name == 'nt':
        return
    fd = None
    try:
        flags = os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0)
        fd = os.open(str(directory), flags)
        os.fsync(fd)
    except OSError:
        # Some filesystems/container mounts do not permit opening directories;
        # the file itself was already fsynced, so preserve the successful write.
        return
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass


def _replace_with_retry(tmp, target):
    """Replace a state file, tolerating transient Windows sharing errors."""
    deadline = time.monotonic() + 2.0
    while True:
        try:
            os.replace(tmp, target)
            return
        except OSError as exc:
            if exc.errno not in (getattr(os, 'EACCES', 13), 13, 32) or time.monotonic() >= deadline:
                raise
            time.sleep(0.01)


def write_object(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = _temporary_path(path)
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        _replace_with_retry(tmp, path)
        _fsync_directory(path.parent)
    except Exception:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass
        raise
