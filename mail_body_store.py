#!/usr/bin/env python3
"""Bounded persistent cache for parsed IMAP message bodies."""

import json
import os
import sqlite3
import threading
import time
from pathlib import Path


class MailBodyStore:
    def __init__(self, path: Path, max_items: int = 5000,
                 max_bytes: int = 64 * 1024 * 1024):
        self.path = Path(path)
        self.max_items = max(1, int(max_items))
        self.max_bytes = max(1024, int(max_bytes))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(self.path), timeout=10, check_same_thread=False
        )
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=10000")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS message_bodies (
                account_id TEXT NOT NULL,
                message_id TEXT NOT NULL,
                payload TEXT NOT NULL,
                payload_bytes INTEGER NOT NULL,
                updated_ns INTEGER NOT NULL,
                PRIMARY KEY (account_id, message_id)
            )
            """
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_message_bodies_updated "
            "ON message_bodies(updated_ns)"
        )
        self._conn.commit()
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    @staticmethod
    def _key(value):
        return str(value or "")

    def get(self, account_id, message_id):
        with self._lock:
            row = self._conn.execute(
                "SELECT payload FROM message_bodies "
                "WHERE account_id=? AND message_id=?",
                (self._key(account_id), self._key(message_id)),
            ).fetchone()
        if not row:
            return None
        try:
            value = json.loads(row[0])
            return value if isinstance(value, dict) else None
        except (TypeError, ValueError, json.JSONDecodeError):
            return None

    def contains(self, account_id, message_id):
        with self._lock:
            return self._conn.execute(
                "SELECT 1 FROM message_bodies "
                "WHERE account_id=? AND message_id=?",
                (self._key(account_id), self._key(message_id)),
            ).fetchone() is not None

    def put(self, account_id, message_id, message):
        payload = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
        size = len(payload.encode("utf-8", errors="ignore"))
        if size > self.max_bytes:
            return False
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO message_bodies(
                    account_id, message_id, payload, payload_bytes, updated_ns
                ) VALUES(?,?,?,?,?)
                ON CONFLICT(account_id, message_id) DO UPDATE SET
                    payload=excluded.payload,
                    payload_bytes=excluded.payload_bytes,
                    updated_ns=excluded.updated_ns
                """,
                (
                    self._key(account_id), self._key(message_id), payload,
                    size, time.time_ns(),
                ),
            )
            self._prune_locked()
            self._conn.commit()
        return True

    def _prune_locked(self):
        count, total = self._conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(payload_bytes), 0) "
            "FROM message_bodies"
        ).fetchone()
        while count > self.max_items or total > self.max_bytes:
            rows = self._conn.execute(
                "SELECT account_id, message_id, payload_bytes "
                "FROM message_bodies ORDER BY updated_ns ASC LIMIT 100"
            ).fetchall()
            if not rows:
                break
            remove = []
            for account_id, message_id, payload_bytes in rows:
                remove.append((account_id, message_id))
                count -= 1
                total -= int(payload_bytes or 0)
                if count <= self.max_items and total <= self.max_bytes:
                    break
            self._conn.executemany(
                "DELETE FROM message_bodies "
                "WHERE account_id=? AND message_id=?",
                remove,
            )

    def delete_account(self, account_id):
        with self._lock:
            self._conn.execute(
                "DELETE FROM message_bodies WHERE account_id=?",
                (self._key(account_id),),
            )
            self._conn.commit()

    def rebind_accounts(self, account_mapping):
        """Move persisted bodies to replacement account IDs without losing newer rows."""
        moved = 0
        with self._lock:
            for old_id, new_id in account_mapping.items():
                old_key, new_key = self._key(old_id), self._key(new_id)
                if not old_key or not new_key or old_key == new_key:
                    continue
                rows = self._conn.execute(
                    "SELECT message_id, payload, payload_bytes, updated_ns "
                    "FROM message_bodies WHERE account_id=?",
                    (old_key,),
                ).fetchall()
                for message_id, payload, payload_bytes, updated_ns in rows:
                    self._conn.execute(
                        """
                        INSERT INTO message_bodies(
                            account_id, message_id, payload, payload_bytes, updated_ns
                        ) VALUES(?,?,?,?,?)
                        ON CONFLICT(account_id, message_id) DO UPDATE SET
                            payload=excluded.payload,
                            payload_bytes=excluded.payload_bytes,
                            updated_ns=excluded.updated_ns
                        WHERE excluded.updated_ns > message_bodies.updated_ns
                        """,
                        (new_key, message_id, payload, payload_bytes, updated_ns),
                    )
                if rows:
                    self._conn.execute(
                        "DELETE FROM message_bodies WHERE account_id=?", (old_key,)
                    )
                    moved += len(rows)
            if moved:
                self._prune_locked()
                self._conn.commit()
        return moved

    def stats(self):
        with self._lock:
            count, total = self._conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(payload_bytes), 0) "
                "FROM message_bodies"
            ).fetchone()
        return {"count": int(count), "bytes": int(total)}

    def close(self):
        with self._lock:
            self._conn.close()
