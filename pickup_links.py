#!/usr/bin/env python3
"""Opaque bearer links for per-alias mail pickup pages."""
import json
import os
import secrets
import threading
from datetime import datetime, timezone
from pathlib import Path


class PickupLinkStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._links = self._load()
        self._normalize()
        self._reindex()

    def _load(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _save(self):
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        payload = json.dumps(self._links, ensure_ascii=False, indent=2)
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, self.path)

    def _normalize(self):
        """Collapse legacy duplicate records to one active token per alias."""
        normalized = {}
        changed = False
        for old_key, raw in self._links.items():
            if not isinstance(raw, dict):
                changed = True
                continue
            item = dict(raw)
            account_id = str(item.get("account_id", "")).strip()
            alias_email = str(item.get("alias_email", "")).strip().lower()
            token = str(item.get("token", "")).strip()
            if not account_id or not alias_email or not token or not item.get("active"):
                changed = True
                continue
            item["account_id"] = account_id
            item["alias_email"] = alias_email
            key = self._key(account_id, alias_email)
            existing = normalized.get(key)
            # Prefer the record already stored under the canonical key. This
            # preserves the URL exported by the current account mapping.
            if existing is None or (old_key == key and existing[0] != key):
                normalized[key] = (old_key, item)
            changed = changed or old_key != key or existing is not None
        self._links = {key: value[1] for key, value in normalized.items()}
        if changed:
            self._save()

    def _reindex(self):
        self._by_token = {}
        self._by_account = {}
        for item in self._links.values():
            if not item.get("active"):
                continue
            self._by_token[item["token"]] = item
            self._by_account.setdefault(item["account_id"], []).append(item)

    @staticmethod
    def _key(account_id, alias_email):
        return f"{account_id}\0{alias_email.strip().lower()}"

    def ensure(self, account_id, alias_email):
        key = self._key(account_id, alias_email)
        with self._lock:
            item = self._links.get(key)
            if not item or not item.get("active"):
                item = {
                    "token": secrets.token_urlsafe(32),
                    "account_id": account_id,
                    "alias_email": alias_email.strip().lower(),
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "active": True,
                }
                self._links[key] = item
                self._save()
                self._reindex()
            return dict(item)

    def get_by_token(self, token):
        with self._lock:
            item = self._by_token.get(token)
            if item and item.get("active"):
                return dict(item)
        return None

    def revoke(self, account_id, alias_email):
        key = self._key(account_id, alias_email)
        with self._lock:
            removed = False
            for stored_key, item in list(self._links.items()):
                if stored_key == key or (
                    item.get("account_id") == account_id
                    and item.get("alias_email", "").lower() == alias_email.strip().lower()
                ):
                    del self._links[stored_key]
                    removed = True
            if not removed:
                return False
            self._save()
            self._reindex()
            return True

    def list_for_aliases(self, aliases):
        return [self.ensure(a["account_id"], a["email"]) for a in aliases if a.get("account_id") and a.get("email")]

    def list_for_account(self, account_id):
        with self._lock:
            return [dict(item) for item in self._by_account.get(account_id, ())]

    def list_all(self):
        with self._lock:
            return [dict(item) for item in self._by_token.values() if item.get("active")]

    def rebind_stale_accounts(self, valid_account_ids):
        """Keep old pickup tokens working after an account is re-imported."""
        valid_ids = set(valid_account_ids)
        with self._lock:
            current_by_alias = {
                item.get("alias_email", "").lower(): item.get("account_id")
                for item in self._links.values()
                if item.get("active") and item.get("account_id") in valid_ids
            }
            changed = 0
            rebuilt = {}
            for item in self._links.values():
                if not item.get("active") or item.get("account_id") in valid_ids:
                    account_id = item.get("account_id")
                else:
                    account_id = current_by_alias.get(item.get("alias_email", "").lower())
                    if account_id:
                        item["account_id"] = account_id
                        changed += 1
                if account_id in valid_ids:
                    key = self._key(account_id, item.get("alias_email", ""))
                    existing = rebuilt.get(key)
                    if existing is None or item.get("created_at", "") > existing.get("created_at", ""):
                        rebuilt[key] = item
                    if existing is not None:
                        changed += 1
            if len(rebuilt) != len(self._links):
                changed += abs(len(self._links) - len(rebuilt))
            self._links = rebuilt
            if changed:
                self._save()
            self._reindex()
            return changed
