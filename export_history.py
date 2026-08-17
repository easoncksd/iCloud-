#!/usr/bin/env python3
"""Persistent export state for Hide My Email aliases."""
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path


class ExportHistoryStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._records = self._load()

    @staticmethod
    def _key(email):
        return str(email or "").strip().lower()

    def _load(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return {}
            return {
                self._key(email): dict(record)
                for email, record in data.items()
                if self._key(email) and isinstance(record, dict)
            }
        except (OSError, json.JSONDecodeError):
            return {}

    def _save(self):
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        payload = json.dumps(self._records, ensure_ascii=False, indent=2)
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, self.path)

    def get(self, email):
        with self._lock:
            record = self._records.get(self._key(email))
            return dict(record) if record else None

    def status_map(self, emails=None):
        with self._lock:
            if emails is None:
                return {email: dict(record) for email, record in self._records.items()}
            return {
                key: dict(self._records[key])
                for key in (self._key(email) for email in emails)
                if key in self._records
            }

    def claim(self, items):
        """Atomically mark new aliases exported and return (claimed, skipped)."""
        claimed = []
        skipped = []
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            seen = set()
            for raw in items:
                email = self._key(raw.get("email"))
                if not email or email in seen:
                    continue
                seen.add(email)
                if email in self._records:
                    skipped.append(email)
                    continue
                record = {
                    "email": email,
                    "account_id": str(raw.get("account_id") or ""),
                    "exported_at": now,
                    "export_count": 1,
                }
                self._records[email] = record
                claimed.append(dict(record))
            if claimed:
                self._save()
        return claimed, skipped

    def restore(self, emails):
        restored = []
        with self._lock:
            for email in emails:
                key = self._key(email)
                if key and key in self._records:
                    del self._records[key]
                    restored.append(key)
            if restored:
                self._save()
        return restored

    def delete_account(self, account_id):
        """Delete export records that belong to a removed account."""
        account_id = str(account_id or "")
        with self._lock:
            emails = [
                email for email, record in self._records.items()
                if str(record.get("account_id") or "") == account_id
            ]
            for email in emails:
                del self._records[email]
            if emails:
                self._save()
            return len(emails)

    def rebind_accounts(self, account_mapping, alias_accounts=None):
        """Update export ownership after account IDs change."""
        alias_accounts = alias_accounts or {}
        changed = 0
        with self._lock:
            for email, record in self._records.items():
                old_id = str(record.get("account_id") or "")
                new_id = alias_accounts.get(email) or account_mapping.get(old_id)
                if new_id and new_id != old_id:
                    record["account_id"] = new_id
                    changed += 1
            if changed:
                self._save()
        return changed
