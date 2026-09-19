"""Strict JSON reads and atomic writes for authoritative application state."""
import json
import os
from pathlib import Path


def read_object(path):
    path = Path(path)
    try:
        raw = path.read_text(encoding='utf-8')
    except FileNotFoundError:
        return {}
    except OSError:
        raise RuntimeError(f'{path.name} 无法读取，已停止操作以保护原文件') from None
    try:
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise ValueError()
        return result
    except (ValueError, TypeError):
        raise RuntimeError(f'{path.name} 数据损坏，已停止操作；请从备份恢复') from None


def write_object(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
