#!/usr/bin/env python3
"""
iCloud HME — 邮件本地缓存
===========================
一次拉取终身存储，增量更新，去重合并。

存储: results/mail_cache.json
"""

import json
import os
import copy
import threading
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

HERE = Path(__file__).resolve().parent
CACHE_FILE = HERE / "results" / "mail_cache.json"
MAX_INBOX_MESSAGES = 1000
MAX_ALIAS_MESSAGES = 100


class MailCache:
    """邮件本地缓存"""

    def __init__(self):
        # Setters persist while holding this lock, so _save() must be re-entrant.
        self._lock = threading.RLock()
        self._data: Dict = {}
        self._load()

    def _load(self):
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        if CACHE_FILE.exists():
            try:
                self._data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
            except Exception:
                self._data = {}

    def _save(self):
        with self._lock:
            tmp = CACHE_FILE.with_suffix(CACHE_FILE.suffix + ".tmp")
            payload = json.dumps(self._data, indent=2, ensure_ascii=False)
            with open(tmp, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, CACHE_FILE)

    def _ensure_account(self, acc_id: str):
        if acc_id not in self._data:
            self._data[acc_id] = {
                "last_checked": None,
                "inbox_emails": [],
                "alias_emails": {},
            }

    def get_inbox(self, acc_id: str) -> List[Dict]:
        with self._lock:
            self._ensure_account(acc_id)
            return list(self._data[acc_id].get("inbox_emails", []))

    def set_inbox(self, acc_id: str, emails: List[Dict]):
        with self._lock:
            self._ensure_account(acc_id)
            existing_ids = {str(e.get("id")) for e in self._data[acc_id]["inbox_emails"]}
            new_emails = []
            for email in emails:
                msg_id = str(email.get("id"))
                if msg_id in existing_ids:
                    continue
                existing_ids.add(msg_id)
                new_emails.append(email)
            if new_emails:
                self._data[acc_id]["inbox_emails"].extend(new_emails)
                if len(self._data[acc_id]["inbox_emails"]) > MAX_INBOX_MESSAGES:
                    self._data[acc_id]["inbox_emails"] = \
                        self._data[acc_id]["inbox_emails"][-MAX_INBOX_MESSAGES:]
            self._data[acc_id]["last_checked"] = datetime.now().isoformat()
            if new_emails:
                self._save()

    def get_alias_mail(self, acc_id: str, alias_email: str) -> List[Dict]:
        with self._lock:
            self._ensure_account(acc_id)
            return list(self._data[acc_id].get("alias_emails", {}).get(alias_email, []))

    def set_alias_mail(self, acc_id: str, alias_email: str, emails: List[Dict]):
        with self._lock:
            self._ensure_account(acc_id)
            if alias_email not in self._data[acc_id]["alias_emails"]:
                self._data[acc_id]["alias_emails"][alias_email] = []
            existing_ids = {str(e.get("id")) for e in self._data[acc_id]["alias_emails"][alias_email]}
            new_emails = []
            for email in emails:
                msg_id = str(email.get("id"))
                if msg_id in existing_ids:
                    continue
                existing_ids.add(msg_id)
                new_emails.append(email)
            if new_emails:
                self._data[acc_id]["alias_emails"][alias_email].extend(new_emails)
                self._data[acc_id]["alias_emails"][alias_email] = \
                    self._data[acc_id]["alias_emails"][alias_email][-MAX_ALIAS_MESSAGES:]
                self._save()

    def set_alias_mail_batch(self, acc_id: str, by_alias: Dict[str, List[Dict]]):
        with self._lock:
            self._ensure_account(acc_id)
            changed = False
            for alias, emails in by_alias.items():
                if alias not in self._data[acc_id]["alias_emails"]:
                    self._data[acc_id]["alias_emails"][alias] = []
                existing_ids = {str(e.get("id")) for e in self._data[acc_id]["alias_emails"][alias]}
                new_emails = []
                for email in emails:
                    msg_id = str(email.get("id"))
                    if msg_id in existing_ids:
                        continue
                    existing_ids.add(msg_id)
                    new_emails.append(email)
                if new_emails:
                    self._data[acc_id]["alias_emails"][alias].extend(new_emails)
                    self._data[acc_id]["alias_emails"][alias] = \
                        self._data[acc_id]["alias_emails"][alias][-MAX_ALIAS_MESSAGES:]
                    changed = True
            if changed:
                self._save()

    def get_all_alias_mail(self, acc_id: str) -> Dict[str, List[Dict]]:
        with self._lock:
            self._ensure_account(acc_id)
            return copy.deepcopy(self._data[acc_id].get("alias_emails", {}))

    def last_checked(self, acc_id: str) -> Optional[str]:
        with self._lock:
            self._ensure_account(acc_id)
            return self._data[acc_id].get("last_checked")

    def cache_age_seconds(self, acc_id: str) -> float:
        lc = self.last_checked(acc_id)
        if not lc:
            return float("inf")
        try:
            dt = datetime.fromisoformat(lc)
            return (datetime.now() - dt).total_seconds()
        except Exception:
            return float("inf")

    def clear_account(self, acc_id: str):
        with self._lock:
            if acc_id in self._data:
                del self._data[acc_id]
                self._save()

    def clear_all(self):
        with self._lock:
            self._data = {}
            self._save()

    def rebind_accounts(self, account_mapping: Dict[str, str]) -> int:
        """Merge cache partitions after an account is re-imported with a new ID."""
        moved = 0
        with self._lock:
            for old_id, new_id in account_mapping.items():
                if not old_id or not new_id or old_id == new_id or old_id not in self._data:
                    continue
                source = self._data.pop(old_id)
                self._ensure_account(new_id)
                target = self._data[new_id]

                existing_ids = {
                    str(message.get("id"))
                    for message in target.get("inbox_emails", [])
                }
                for message in source.get("inbox_emails", []):
                    message_id = str(message.get("id"))
                    if message_id not in existing_ids:
                        target["inbox_emails"].append(message)
                        existing_ids.add(message_id)
                target["inbox_emails"] = target["inbox_emails"][-MAX_INBOX_MESSAGES:]

                for alias, messages in source.get("alias_emails", {}).items():
                    target_messages = target["alias_emails"].setdefault(alias, [])
                    alias_ids = {str(message.get("id")) for message in target_messages}
                    for message in messages:
                        message_id = str(message.get("id"))
                        if message_id not in alias_ids:
                            target_messages.append(message)
                            alias_ids.add(message_id)
                    target["alias_emails"][alias] = target_messages[-MAX_ALIAS_MESSAGES:]

                old_checked = source.get("last_checked") or ""
                new_checked = target.get("last_checked") or ""
                target["last_checked"] = max(old_checked, new_checked) or None
                moved += 1
            if moved:
                self._save()
        return moved

    def get_stats(self, acc_id: str) -> Dict:
        with self._lock:
            self._ensure_account(acc_id)
            acc = self._data[acc_id]
            inbox_count = len(acc.get("inbox_emails", []))
            alias_count = sum(len(v) for v in acc.get("alias_emails", {}).values())
            return {
                "last_checked": acc.get("last_checked"),
                "cache_age_sec": self.cache_age_seconds(acc_id),
                "inbox_cached": inbox_count,
                "alias_cached": alias_count,
                "alias_count": len(acc.get("alias_emails", {})),
            }


_mail_cache: Optional[MailCache] = None


def get_cache() -> MailCache:
    global _mail_cache
    if _mail_cache is None:
        _mail_cache = MailCache()
    return _mail_cache
