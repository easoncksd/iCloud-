#!/usr/bin/env python3
"""Opaque bearer links for per-alias mail pickup pages."""
import json
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

    def _load(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _save(self):
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(self._links, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)

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
            return dict(item)

    def get_by_token(self, token):
        with self._lock:
            for item in self._links.values():
                if item.get("token") == token and item.get("active"):
                    return dict(item)
        return None

    def revoke(self, account_id, alias_email):
        key = self._key(account_id, alias_email)
        with self._lock:
            item = self._links.get(key)
            if not item:
                return False
            item["active"] = False
            item["revoked_at"] = datetime.now(timezone.utc).isoformat()
            self._save()
            return True

    def list_for_aliases(self, aliases):
        return [self.ensure(a["account_id"], a["email"]) for a in aliases if a.get("account_id") and a.get("email")]
