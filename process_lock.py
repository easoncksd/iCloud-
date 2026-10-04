"""Small cross-process file locks used by the service and its operators.

Threading locks only coordinate workers inside one Python process.  The web
service, the legacy scheduler, and maintenance jobs can otherwise all open the
same JSON files at once.  ``ProcessLock`` uses an OS advisory lock while the
process is alive and leaves a tiny diagnostic record in the lock file.  The
record is deliberately not used for stale-lock deletion: the kernel releases
the advisory lock on process exit, and deleting a lock based on a PID can race
with PID reuse.
"""
from __future__ import annotations

import errno
import json
import os
import socket
import time
from pathlib import Path


class LockError(RuntimeError):
    """Base class for lock acquisition and release errors."""


class LockAlreadyHeld(LockError):
    """Another process currently owns the requested lock."""


def _busy_error(exc: OSError) -> bool:
    return exc.errno in (errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK)


class ProcessLock:
    """An advisory exclusive lock held for the lifetime of this object.

    On POSIX the lock is a kernel ``flock`` and the file may safely remain on
    disk after release.  On Windows ``msvcrt`` byte-range locking is used.  A
    small exclusive-create fallback is kept for unusual Python platforms; it
    is intentionally conservative and never removes a lock it did not create.
    """

    def __init__(self, path: os.PathLike | str, timeout: float = 0.0,
                 poll_interval: float = 0.1):
        self.path = Path(path)
        self.timeout = max(0.0, float(timeout))
        self.poll_interval = max(0.01, float(poll_interval))
        self._handle = None
        self._fallback = False

    @property
    def locked(self) -> bool:
        return self._handle is not None

    def _try_lock(self, handle):
        if os.name == "nt":
            import msvcrt
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b" ")
                handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                if _busy_error(exc):
                    raise LockAlreadyHeld(self.path) from None
                raise
            return

        try:
            import fcntl
        except ImportError:
            self._fallback = True
            raise LockAlreadyHeld(self.path) from None
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if _busy_error(exc):
                raise LockAlreadyHeld(self.path) from None
            raise

    def _write_owner(self, handle):
        owner = {
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "started_at": time.time(),
        }
        handle.seek(0)
        handle.truncate()
        handle.write(json.dumps(owner, ensure_ascii=False).encode("utf-8"))
        handle.flush()
        os.fsync(handle.fileno())

    def acquire(self, timeout=None):
        if self.locked:
            return self
        timeout = self.timeout if timeout is None else max(0.0, float(timeout))
        deadline = time.monotonic() + timeout
        self.path.parent.mkdir(parents=True, exist_ok=True)
        while True:
            handle = None
            try:
                if self._fallback:
                    # This branch is only reached on platforms without an
                    # advisory-lock implementation.  Do not remove a stale
                    # path on failure; an operator must inspect it explicitly.
                    handle = self.path.open("x+b")
                else:
                    handle = self.path.open("a+b")
                self._try_lock(handle)
                try:
                    os.chmod(self.path, 0o600)
                except OSError:
                    pass
                self._write_owner(handle)
                self._handle = handle
                return self
            except LockAlreadyHeld:
                if handle is not None:
                    handle.close()
                if time.monotonic() >= deadline:
                    raise LockAlreadyHeld(
                        f"lock already held: {self.path}"
                    ) from None
                time.sleep(self.poll_interval)
            except FileExistsError:
                if handle is not None:
                    handle.close()
                if time.monotonic() >= deadline:
                    raise LockAlreadyHeld(
                        f"lock already held: {self.path}"
                    ) from None
                time.sleep(self.poll_interval)
            except Exception:
                if handle is not None:
                    handle.close()
                raise

    def release(self):
        handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            elif not self._fallback:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
            if self._fallback:
                try:
                    self.path.unlink()
                except FileNotFoundError:
                    pass

    def __enter__(self):
        return self.acquire()

    def __exit__(self, exc_type, exc, tb):
        self.release()
        return False


def service_lock_path(project_root=None):
    root = Path(project_root) if project_root is not None else Path(__file__).resolve().parent
    return root / "results" / "icloud-hme.service.lock"


def maintenance_lock_path(project_root=None):
    root = Path(project_root) if project_root is not None else Path(__file__).resolve().parent
    return root / "results" / "icloud-hme.maintenance.lock"


def service_process_lock(project_root=None, timeout=0.0):
    return ProcessLock(service_lock_path(project_root), timeout=timeout)


def maintenance_snapshot_lock(project_root=None, timeout=0.0):
    return ProcessLock(maintenance_lock_path(project_root), timeout=timeout)
