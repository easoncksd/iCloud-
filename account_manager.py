#!/usr/bin/env python3
"""
iCloud HME — 多账号管理器
===========================
管理多组 iCloud 账号及其隐私邮箱别名。

功能:
  - 账号 CRUD (增删改查)
  - Cookie 导入解析 (Header String / JSON)
  - 批量会话校验
  - 别名按账号归属索引
  - 跨账号并发/轮询创建

用法:
    from account_manager import AccountManager

    mgr = AccountManager()
    mgr.add_account("主号", cookie_header_string)
    mgr.create_aliases_batch(["acc_xxx", "acc_yyy"], count_per_account=5)
"""

import json
import os
import random
import time
import uuid
import copy
import threading
import weakref
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Any

from runtime_paths import DATA_ROOT
HERE = DATA_ROOT
ACCOUNTS_FILE = HERE / "accounts.json"
OLD_COOKIES_FILE = HERE / "cookies.json"
RESULTS_DIR = HERE / "results"
LATEST_EMAILS = RESULTS_DIR / "latest_emails.txt"
CREATE_ALIAS_INTERVAL_SECONDS = max(
    0.0, float(os.environ.get("CREATE_ALIAS_INTERVAL_SECONDS", "3"))
)
CREATE_ALIAS_JITTER_SECONDS = max(
    0.0, float(os.environ.get("CREATE_ALIAS_JITTER_SECONDS", "2"))
)

from mail_cache import get_cache, sort_messages_newest  # noqa: E402


class AccountManager:
    """多账号管理器"""

    _CREATE_LIMIT_MARKERS = (
        "reached the limit of addresses",
        "maximum number of addresses",
        "address limit",
        "quota exceeded",
        "too many addresses",
        "rate limit",
        "too many requests",
        "429",
        "达到上限",
        "超过上限",
        "创建过多",
        "操作频繁",
    )
    _CREATE_TEMPORARY_LIMIT_MARKERS = (
        "right now",
        "try again later",
        "rate limit",
        "too many requests",
        "429",
        "temporarily",
        "throttle",
        "timeout",
        "timed out",
        "connection",
        "http 421",
        "http 401",
        "http 403",
        "trusttokens",
        "操作频繁",
        "稍后再试",
    )

    def __init__(self):
        self.accounts: Dict[str, Dict] = {}
        # _save() also takes this lock; use RLock for callers that update then persist.
        self._lock = threading.RLock()
        self._mail_clients: Dict[str, Any] = {}
        # Keep lock identity while any worker holds it, without retaining deleted IDs.
        self._mail_sync_locks = weakref.WeakValueDictionary()
        self._mail_epoch_locks = weakref.WeakValueDictionary()
        self._operation_locks = weakref.WeakValueDictionary()
        self._latest_emails_lock = threading.Lock()
        # web_ui installs a checker so duplicate imports cannot replace an
        # account while a durable batch entry is waiting on Apple's cooldown.
        self._creation_activity_checker = None
        self._cache = get_cache()
        self._load()

    def _load(self):
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        if OLD_COOKIES_FILE.exists() and not ACCOUNTS_FILE.exists():
            try:
                self._migrate_old_cookies()
            except Exception:
                pass

        if ACCOUNTS_FILE.exists():
            from durable_json import read_object
            data = read_object(ACCOUNTS_FILE)
            accounts = data.get("accounts")
            if not isinstance(accounts, dict) or any(not isinstance(a, dict) for a in accounts.values()):
                raise RuntimeError("accounts.json 账号结构损坏，已停止操作以保护原文件")
            self.accounts = accounts
            from credential_store import unseal
            for acc_id, account in accounts.items():
                if 'credentials_encrypted' in account:
                    account.update(unseal(acc_id, account.pop('credentials_encrypted'),
                                          ACCOUNTS_FILE.with_name('.credentials.key')))

    def _save(self):
        with self._lock:
            from credential_store import seal
            stored = {}
            for acc_id, account in self.accounts.items():
                item = dict(account)
                credentials = {key: item.pop(key) for key in ('cookies', 'app_password') if key in item}
                if credentials:
                    item['credentials_encrypted'] = seal(acc_id, credentials,
                                                         ACCOUNTS_FILE.with_name('.credentials.key'))
                stored[acc_id] = item
            payload = json.dumps({
                    "accounts": stored,
                    "updated_at": datetime.now().isoformat(),
                }, indent=2, ensure_ascii=False)
            tmp = ACCOUNTS_FILE.with_suffix(ACCOUNTS_FILE.suffix + ".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, ACCOUNTS_FILE)

    def _migrate_old_cookies(self):
        try:
            old = json.loads(OLD_COOKIES_FILE.read_text(encoding="utf-8"))
        except Exception:
            return
        if not isinstance(old, dict) or not old:
            return

        acc_id = self._generate_id()
        self.accounts[acc_id] = {
            "id": acc_id,
            "name": "默认账号",
            "real_email": "",
            "cookies": old,
            "host": "icloud.com",
            "status": "active",
            "alias_total": 0,
            "alias_active": 0,
            "last_validated": None,
            "last_error": None,
            "created_at": datetime.now().isoformat(),
        }
        self._save()
        try:
            OLD_COOKIES_FILE.rename(OLD_COOKIES_FILE.with_suffix(".json.bak"))
        except OSError:
            pass

    def _generate_id(self) -> str:
        return "acc_" + uuid.uuid4().hex[:8]

    @staticmethod
    def parse_cookie_input(raw: str) -> Dict[str, str]:
        raw = raw.strip()
        if not raw:
            raise ValueError("空白输入 — 请粘贴 Cookie Header String 或 JSON")

        if raw.startswith("{") or raw.startswith("["):
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, dict):
                cookies = {k: str(v) for k, v in parsed.items() if v}
                if cookies:
                    return cookies
            if isinstance(parsed, list):
                cookies = {}
                for item in parsed:
                    if not isinstance(item, dict):
                        continue
                    name = str(item.get("name") or item.get("Name") or "").strip()
                    if not name:
                        continue
                    value = item.get("value")
                    if value is None:
                        value = item.get("Value")
                    if value is None or value == "":
                        continue
                    cookies[name] = str(value)
                if cookies:
                    return cookies

        cookies: Dict[str, str] = {}
        for part in raw.split(";"):
            part = part.strip()
            if "=" in part:
                name, value = part.split("=", 1)
                name = name.strip()
                value = value.strip()
                if name:
                    cookies[name] = value

        if not cookies:
            raise ValueError(
                "无法解析 Cookie 输入。\n"
                "请提供 Header String 格式 (name=value; ...) 或 JSON 格式"
            )

        return cookies

    @staticmethod
    def detect_icloud_host(raw: str) -> str:
        text = (raw or "").strip()
        if text.startswith("["):
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, list):
                for item in parsed:
                    if not isinstance(item, dict):
                        continue
                    domain = str(item.get("domain") or item.get("Domain") or "").lower()
                    if "icloud.com.cn" in domain:
                        return "icloud.com.cn"
        if "icloud.com.cn" in text.lower():
            return "icloud.com.cn"
        return "icloud.com"

    @staticmethod
    def _alias_created_at(alias: Dict) -> str:
        value = None
        if isinstance(alias, dict):
            value = alias.get("createdAt") or alias.get("created_at")
        if isinstance(value, (int, float)):
            ts = float(value)
            if ts > 10_000_000_000:
                ts /= 1000.0
            try:
                return datetime.fromtimestamp(ts).astimezone().isoformat()
            except (OSError, OverflowError, ValueError):
                return datetime.now().astimezone().isoformat()
        text = str(value or "").strip()
        return text or datetime.now().astimezone().isoformat()

    def _existing_account_id_locked(self, real_email: str):
        key = (real_email or "").strip().lower()
        if not key:
            return None
        for acc_id, account in self.accounts.items():
            if (account.get("real_email") or "").strip().lower() == key:
                return acc_id
        return None

    def record_known_aliases(self, acc_id: str, aliases: List[Dict]) -> int:
        """Persist Apple-listed aliases into latest_emails.txt without duplicates."""
        rows = []
        seen = set()
        for alias in aliases or []:
            email = str((alias or {}).get("email") or "").strip().lower()
            if not email or "@" not in email or email in seen:
                continue
            seen.add(email)
            rows.append((email, str(acc_id), self._alias_created_at(alias)))
        if not rows:
            return 0

        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        added = 0
        with self._latest_emails_lock:
            existing = set()
            if LATEST_EMAILS.exists():
                for line in LATEST_EMAILS.read_text(encoding="utf-8").splitlines():
                    parts = line.split("\t")
                    if parts and parts[0].strip():
                        existing.add(parts[0].strip().lower())
            new_lines = []
            for email, account_id, created_at in rows:
                if email in existing:
                    continue
                new_lines.append("\t".join([email, account_id, created_at]) + "\n")
                existing.add(email)
                added += 1
            if new_lines:
                with open(str(LATEST_EMAILS), "a", encoding="utf-8") as handle:
                    handle.writelines(new_lines)
                    handle.flush()
                    os.fsync(handle.fileno())
        return added

    def add_account(
        self, name: str, cookie_input: str, host: str = "icloud.com"
    ) -> Dict:
        from icloud_hme import ICloudHME

        cookies = self.parse_cookie_input(cookie_input)
        if host not in ("icloud.com", "icloud.com.cn"):
            host = self.detect_icloud_host(cookie_input)
        acc_id = self._generate_id()
        aliases: List[Dict] = []
        client = None

        account: Dict[str, Any] = {
            "id": acc_id,
            "name": name,
            "real_email": "",
            "icloud_email": "",
            "cookies": cookies,
            "host": host,
            "status": "active",
            "alias_total": 0,
            "alias_active": 0,
            "last_validated": None,
            "last_error": None,
            "created_at": datetime.now().isoformat(),
        }

        try:
            client = ICloudHME(cookies, host=host, verbose=False)
            client.validate_session()
            info = client.get_account_info()
            if info:
                account["real_email"] = (
                    info.get("appleId", "")
                    or info.get("primaryEmail", "")
                )
                account["icloud_email"] = self._derive_icloud_email(info)

            try:
                aliases = client.list_aliases()
                account["alias_total"] = len(aliases)
                account["alias_active"] = sum(
                    1 for a in aliases if a.get("active")
                )
            except Exception:
                aliases = []

            account["last_validated"] = datetime.now().isoformat()
            account["last_error"] = None
        except Exception as e:
            raise ValueError(str(e)[:300]) from e
        finally:
            if client is not None:
                close = getattr(client, "close", None)
                if callable(close):
                    close()

        with self._lock:
            existing_id = self._existing_account_id_locked(account.get("real_email", ""))
        # Do not invoke the application callback while holding _lock: the
        # web-layer checker takes its batch lock and status readers take the
        # locks in the opposite order.
        if existing_id and self._account_creation_in_progress(existing_id):
            raise ValueError("该账号已有创建任务，结束后再重新导入")
        if existing_id:
            # Use the same locked update path as explicit reimport. Cookie
            # verification must never reset unrelated IMAP pause/epoch state.
            updated = self.reimport_account(existing_id, cookie_input, host)
            if name and name != "未命名账号":
                updated = self.update_account(existing_id, name=name)
            return updated
        with self._lock:
            # Re-check identity after the callback in case another import won
            # the race while we were validating the new credentials. The
            # activity check intentionally stays outside this lock to avoid a
            # lock-order inversion with web_ui's batch status readers.
            existing_id = self._existing_account_id_locked(account.get("real_email", ""))
            if existing_id:
                raise ValueError("该账号已被另一个请求导入，请使用重新导入")
            else:
                self.accounts[acc_id] = account
            self._save()
        if aliases:
            self.record_known_aliases(acc_id, aliases)
        return account

    def set_creation_activity_checker(self, checker):
        """Install an optional application-level checker for queued batches."""
        if checker is not None and not callable(checker):
            raise TypeError("creation activity checker must be callable")
        with self._lock:
            self._creation_activity_checker = checker

    def _account_creation_in_progress(self, acc_id):
        checker = self._creation_activity_checker
        if checker is not None:
            try:
                if checker(acc_id):
                    return True
            except Exception:
                # A checker must never make account import unsafe by failing
                # open; the guard below still covers active/queued workers.
                return True
        guard = getattr(self, "_creation_guard", None)
        if guard is not None:
            with guard.lock:
                return acc_id in guard.active or acc_id in guard.queued
        return False

    def reimport_account(
        self, acc_id: str, cookie_input: str, host: str = "icloud.com"
    ) -> Dict:
        from icloud_hme import ICloudHME

        cookies = self.parse_cookie_input(cookie_input)
        if host not in ("icloud.com", "icloud.com.cn"):
            host = self.detect_icloud_host(cookie_input)

        with self._operation_lock(acc_id), self._mail_sync_lock(acc_id):
            account = self.accounts.get(acc_id)
            if not account:
                raise KeyError(f"账号不存在: {acc_id}")
            if self._account_creation_in_progress(acc_id):
                raise ValueError("该账号已有创建任务，结束后再重新导入")

            client = None
            try:
                client = ICloudHME(cookies, host=host, verbose=False)
                client.validate_session()
                info = client.get_account_info() or {}

                new_email = (
                    str(info.get("appleId") or info.get("primaryEmail") or "")
                ).strip()
                old_email = str(account.get("real_email") or "").strip()
                if old_email and new_email and old_email.lower() != new_email.lower():
                    raise ValueError(
                        f"Cookie 属于 {new_email}，与当前账号 {old_email} 不一致"
                    )

                aliases: List[Dict] = []
                try:
                    aliases = client.list_aliases()
                except Exception:
                    aliases = []
            except Exception as e:
                if isinstance(e, ValueError) and str(e).startswith("Cookie 属于"):
                    raise
                raise ValueError(str(e)[:300]) from e
            finally:
                if client is not None:
                    close = getattr(client, "close", None)
                    if callable(close):
                        close()

            self._drop_mail_client(acc_id)
            with self._lock:
                account = self.accounts.get(acc_id)
                if not account:
                    raise KeyError(f"账号不存在: {acc_id}")
                account["cookies"] = cookies
                account["host"] = host
                account["status"] = "active"
                account["last_error"] = None
                account["last_validated"] = datetime.now().isoformat()
                account.update(last_error_kind=None, health_failures=0, health_retry_at=None,
                               credential_generation=uuid.uuid4().hex)
                if new_email:
                    account["real_email"] = new_email
                derived = self._derive_icloud_email(info)
                if derived:
                    account["icloud_email"] = derived
                if aliases:
                    account["alias_total"] = len(aliases)
                    account["alias_active"] = sum(
                        1 for item in aliases if item.get("active")
                    )
                self._save()
            if aliases:
                self.record_known_aliases(acc_id, aliases)
            return dict(account)

    def remove_account(self, acc_id: str) -> bool:
        with self._operation_lock(acc_id), self._mail_sync_lock(acc_id):
            self._drop_mail_client(acc_id)
            with self._lock:
                if acc_id in self.accounts:
                    removed = self.accounts.pop(acc_id)
                    try:
                        self._save()
                    except Exception:
                        self.accounts[acc_id] = removed
                        raise
                    return True
                return False

    def get_account(self, acc_id: str) -> Optional[Dict]:
        with self._lock:
            account = self.accounts.get(acc_id)
            return copy.deepcopy(account) if account is not None else None

    def list_accounts(self) -> List[Dict]:
        with self._lock:
            accounts = [copy.deepcopy(account) for account in self.accounts.values()]
        return sorted(accounts, key=lambda a: (a.get("status") != "active", a.get("created_at", "")))

    def update_account(self, acc_id: str, **kwargs) -> Optional[Dict]:
        with self._lock:
            if acc_id in self.accounts:
                self.accounts[acc_id].update(kwargs)
                self._save()
                return dict(self.accounts[acc_id])
            return None

    @staticmethod
    def _derive_icloud_email(info: Dict) -> str:
        primary = str(info.get("primaryEmail", "") or "").strip()
        apple_id = str(info.get("appleId", "") or "").strip()

        if primary and ("@icloud.com" in primary or "@me.com" in primary or "@mac.com" in primary):
            return primary

        if apple_id and ("@icloud.com" in apple_id or "@me.com" in apple_id or "@mac.com" in apple_id):
            return apple_id

        # A third-party Apple ID does not imply a matching @icloud.com address.
        # Guessing here produces valid-looking credentials that can never log in.
        return ""

    def _operation_lock(self, acc_id: str) -> threading.RLock:
        with self._lock:
            return self._operation_locks.setdefault(acc_id, threading.RLock())

    def validate_account(self, acc_id: str) -> Dict:
        with self._operation_lock(acc_id):
            return self._validate_account_unlocked(acc_id)

    def _validate_account_unlocked(self, acc_id: str) -> Dict:
        from icloud_hme import ICloudHME

        account = self.accounts.get(acc_id)
        if not account:
            raise KeyError(f"账号不存在: {acc_id}")

        aliases: List[Dict] = []
        client = None
        try:
            client = ICloudHME(
                account["cookies"],
                host=account.get("host", "icloud.com"),
                verbose=False,
            )
            client.validate_session()
            info = client.get_account_info()
            if info:
                account["real_email"] = (
                    info.get("appleId", "")
                    or info.get("primaryEmail", "")
                )
                existing = account.get("icloud_email", "")
                is_icloud = existing and any(
                    d in existing for d in ("@icloud.com", "@me.com", "@mac.com")
                )
                if not is_icloud:
                    account["icloud_email"] = self._derive_icloud_email(info)

            aliases = client.list_aliases()
            account["alias_total"] = len(aliases)
            account["alias_active"] = sum(
                1 for a in aliases if a.get("active")
            )
            account["status"] = "active"
            account["last_validated"] = datetime.now().isoformat()
            account["last_error"] = None
            account.update(last_error_kind=None, health_failures=0, health_retry_at=None)
        except Exception as e:
            from create_guard import classify
            kind = classify(e)
            account["status"] = "error"
            account["last_error"] = str(e)[:300]
            failures = int(account.get('health_failures', 0)) + 1
            account.update(last_error_kind=kind, health_failures=failures,
                           health_retry_at=time.time() + (21600 if kind == 'auth' else
                                                         min(3600, 300 * 2 ** min(failures - 1, 4))))
        finally:
            if client is not None:
                close = getattr(client, "close", None)
                if callable(close):
                    close()

        self._save()
        if aliases:
            self.record_known_aliases(acc_id, aliases)
        return account

    def validate_all(self) -> List[Dict]:
        results: List[Dict] = []
        for acc_id in list(self.accounts.keys()):
            try:
                account = self.validate_account(acc_id)
                results.append({
                    "id": acc_id,
                    "ok": account.get("status") == "active",
                    "email": account.get("real_email", ""),
                    "alias_total": account.get("alias_total", 0),
                })
            except Exception as e:
                results.append({
                    "id": acc_id,
                    "ok": False,
                    "error": str(e)[:200],
                })
        return results

    def get_client(self, acc_id: str, verbose: bool = False):
        from icloud_hme import ICloudHME

        account = self.accounts.get(acc_id)
        if not account:
            raise KeyError(f"账号不存在: {acc_id}")
        return ICloudHME(
            account["cookies"],
            host=account.get("host", "icloud.com"),
            verbose=verbose,
        )

    def set_app_password(self, acc_id: str, app_password: str):
        self.set_mail_credentials(acc_id, app_password)

    def set_mail_credentials(self, acc_id, app_password, icloud_email=None, **updates):
        with self._operation_lock(acc_id), self._mail_sync_lock(acc_id):
            self._drop_mail_client(acc_id)
            with self._lock:
                if acc_id not in self.accounts:
                    raise KeyError(f"账号不存在: {acc_id}")
                account = self.accounts[acc_id]
                before = dict(account)
                account.update(app_password=app_password, **updates)
                account['credential_generation'] = uuid.uuid4().hex
                if icloud_email is not None:
                    account['icloud_email'] = icloud_email
                try:
                    self._save()
                except Exception:
                    account.clear()
                    account.update(before)
                    raise

    def _drop_mail_client(self, acc_id: str):
        with self._lock:
            client = self._mail_clients.pop(acc_id, None)
        if client:
            client.disconnect()

    def _mail_sync_lock(self, acc_id: str) -> threading.Lock:
        with self._lock:
            return self._mail_sync_locks.setdefault(acc_id, threading.Lock())

    def _observe_mail_epoch(self, acc_id, epoch):
        if epoch is None:
            return
        with self._mail_epoch_lock(acc_id):
            account = self.get_account(acc_id)
            if not account:
                raise KeyError(acc_id)
            if str(account.get('mail_uidvalidity')) != str(epoch):
                # Clear before acknowledging the new epoch. A crash can cause
                # a rescan, never reuse headers from the previous mailbox.
                self._cache.begin_epoch(acc_id, epoch)
                self.update_account(acc_id, mail_uidvalidity=epoch)

    def _mail_epoch_lock(self, acc_id):
        with self._lock:
            return self._mail_epoch_locks.setdefault(acc_id, threading.RLock())

    def mail_sync_paused(self, acc_id):
        account = self.accounts.get(acc_id) or {}
        return bool(account.get("mail_sync_paused") or account.get("mail_status") == "auth_failed")

    def _require_mail_sync(self, acc_id):
        if self.mail_sync_paused(acc_id):
            raise RuntimeError("收信已暂停，等待延迟复查或点击验证并恢复收信")

    def get_mail_client(self, acc_id: str, verbose: bool = False, allow_paused=False):
        from icloud_mail import ICloudMail

        if not allow_paused:
            self._require_mail_sync(acc_id)
        account = self.accounts.get(acc_id)
        if not account:
            raise KeyError(f"账号不存在: {acc_id}")
        app_pwd = account.get("app_password", "")
        imap_email = account.get("icloud_email", "")
        if not imap_email:
            real = account.get("real_email", "")
            if real and any(d in real for d in ("@icloud.com", "@me.com", "@mac.com")):
                imap_email = real
            else:
                raise ValueError(
                    "未设置 iCloud 邮箱。\n"
                    "Apple ID ({}) 不是 iCloud 地址，\n"
                    "请点击下方按钮输入你的 @icloud.com 邮箱".format(
                        account.get("real_email", "?")
                    )
                )
        if not app_pwd:
            raise ValueError(
                "未设置 App 专用密码。\n"
                "请点击下方按钮，输入 @icloud.com 邮箱和应用密码"
            )
        mail = ICloudMail(imap_email, app_pwd, verbose=verbose)
        generation = account.get('credential_generation')
        def record_failure(error):
            with self._lock:
                current = self.accounts.get(acc_id)
                if (not current or current.get("app_password") != app_pwd or
                        current.get('credential_generation') != generation or
                        (current.get('icloud_email') or current.get('real_email')) != imap_email):
                    return
                self.update_account(acc_id, mail_status="auth_failed",
                    mail_sync_paused=True, mail_last_error=error,
                    mail_last_checked=datetime.now().isoformat(),
                    mail_next_retry_at=time.time() + max(3600, int(os.environ.get("MAIL_AUTH_RECHECK_SECONDS", "21600"))))
        mail.on_auth_failure = record_failure
        mail.on_selected = lambda epoch: self._observe_mail_epoch(acc_id, epoch)
        return mail

    def check_inbox(self, acc_id: str, limit: int = 50, days: int = 7,
                    force: bool = False) -> List[Dict]:
        cached = self._cache.get_inbox(acc_id)
        age = self._cache.cache_age_seconds(acc_id)

        if not force and cached and age < 300:
            return sort_messages_newest(cached)[:limit]

        try:
            mail = self.get_mail_client(acc_id)
            try:
                new_msgs = mail.check_inbox(limit=max(limit, 50), days=days)
            finally:
                mail.disconnect()
        except Exception:
            if force or not cached:
                raise
            new_msgs = []

        self._cache.set_inbox(acc_id, new_msgs)
        return sort_messages_newest(self._cache.get_inbox(acc_id))[:limit]

    def check_alias_mail(self, acc_id: str, alias_email: str,
                         limit: int = 20, days: int = 30,
                         force: bool = False) -> List[Dict]:
        cached = self._cache.get_alias_mail(acc_id, alias_email)
        age = self._cache.cache_age_seconds(acc_id)

        if not force and cached and age < 300:
            return sort_messages_newest(cached)[:limit]

        try:
            mail = self.get_mail_client(acc_id)
            try:
                new_msgs = mail.find_by_recipient(alias_email, limit=limit, days=days)
            finally:
                mail.disconnect()
        except Exception:
            if force or not cached:
                raise
            new_msgs = []

        if new_msgs:
            self._cache.set_alias_mail(acc_id, alias_email, new_msgs)

        return sort_messages_newest(self._cache.get_alias_mail(acc_id, alias_email))[:limit]

    def check_all_aliases_mail(self, acc_id: str, limit_per: int = 5,
                               days: int = 14,
                               force: bool = False) -> Dict[str, List[Dict]]:
        cached = self._cache.get_all_alias_mail(acc_id)
        age = self._cache.cache_age_seconds(acc_id)

        def cached_slice():
            results = {}
            for alias, msgs in (cached or {}).items():
                results[alias] = sort_messages_newest(msgs)[:limit_per]
            return results

        if not force and cached and age < 300:
            return cached_slice()

        try:
            client = self.get_client(acc_id, verbose=False)
            try:
                aliases = client.list_aliases()
            finally:
                close = getattr(client, "close", None)
                if callable(close):
                    close()
        except Exception:
            if cached:
                return cached_slice()
            raise

        alias_set = {a.get("email", "").lower() for a in aliases if a.get("email")}
        if not alias_set:
            return cached_slice()

        try:
            mail = self.get_mail_client(acc_id)
            try:
                all_inbox = mail.check_inbox(limit=100, days=days)
            finally:
                mail.disconnect()
        except Exception:
            if cached:
                return cached_slice()
            raise

        results: Dict[str, List[Dict]] = {}
        for msg in all_inbox:
            for alias in self._match_aliases(msg, alias_set):
                if alias not in results:
                    results[alias] = []
                if len(results[alias]) < limit_per:
                    results[alias].append(msg)

        if results:
            self._cache.set_alias_mail_batch(acc_id, results)

        # A successful scan with no newly matched messages must not erase the
        # cached history shown to users.
        return results or cached_slice()

    def sync_pickup_mail(self, acc_id: str, alias_emails: List[str],
                         scan_limit: int = 100, days: int = 30) -> Dict:
        """Sync with a durable UID cursor independent of the display cache."""
        aliases = {x.strip().lower() for x in alias_emails if x and x.strip()}
        if not aliases:
            return {"messages": {}, "bodies": {}}
        with self._mail_sync_lock(acc_id):
            self._require_mail_sync(acc_id)
            for attempt in range(2):
                try:
                    with self._lock:
                        mail = self._mail_clients.get(acc_id)
                    if not mail:
                        mail = self.get_mail_client(acc_id)
                        with self._lock:
                            self._mail_clients[acc_id] = mail
                    ensure = getattr(mail, '_ensure_connected', None)
                    if callable(ensure):
                        ensure()
                    epoch = getattr(mail, 'uidvalidity', None)
                    self._observe_mail_epoch(acc_id, epoch)
                    # Read snapshots only after epoch invalidation and under the
                    # account lock. Never carry recovered old headers across it.
                    cached = self._cache.get_all_alias_mail(acc_id)
                    inbox = self._cache.get_inbox(acc_id)
                    known = {str(m.get('id')) for rows in cached.values() for m in rows}
                    known.update(str(m.get('id')) for m in inbox)
                    by_alias, bodies, new_headers = {}, {}, []
                    for header in inbox:
                        for alias in self._match_aliases(header, aliases):
                            if str(header.get('id')) not in {str(m.get('id')) for m in cached.get(alias, [])}:
                                by_alias.setdefault(alias, []).append(header)
                    cursor = self._cache.sync_cursor(acc_id, epoch, aliases)
                    highwater, complete = cursor, True
                    candidates = []
                    for uid in mail.recent_uids(limit=None, days=days):
                        uid_text = uid.decode() if isinstance(uid, bytes) else str(uid)
                        number = int(uid_text)
                        highwater = max(highwater, number)
                        if number <= cursor or uid_text in known:
                            continue
                        candidates.append(uid_text)
                    bulk_fetch = getattr(mail, 'fetch_headers', None)
                    for start in range(0, len(candidates), 100):
                        batch = candidates[start:start + 100]
                        headers = (bulk_fetch(batch) if callable(bulk_fetch) else
                                   {uid: mail.fetch_header(uid.encode()) for uid in batch})
                        for uid_text in batch:
                            header = headers.get(uid_text)
                            if not header:
                                # A failed FETCH must not advance past a hole.
                                complete = False
                                continue
                            header = dict(header, _uidvalidity=epoch)
                            new_headers.append(header)
                            for alias in self._match_aliases(header, aliases):
                                by_alias.setdefault(alias, []).append(header)
                    # Header publication must not wait for thousands of body
                    # fetches. Other bodies are warmed gradually or on demand.
                    for messages in list(by_alias.values())[:8]:
                        if not messages:
                            continue
                        message = max(messages, key=lambda m: int(m['id']))
                        msg_id = str(message['id'])
                        full = mail.fetch_full(msg_id.encode())
                        if full is not None and ("body" in full or "html" in full):
                            full.update(message)
                            full['_uidvalidity'] = epoch
                            bodies[msg_id] = full
                    # A cursor becomes durable only after all headers are durable.
                    with self._mail_epoch_lock(acc_id):
                        current_epoch = (self.get_account(acc_id) or {}).get('mail_uidvalidity')
                        if epoch is not None and str(current_epoch) != str(epoch):
                            raise RuntimeError('邮箱世代已变化，重新同步')
                        self._cache.set_inbox(acc_id, new_headers)
                        if by_alias:
                            self._cache.set_alias_mail_batch(acc_id, by_alias)
                        if complete:
                            self._cache.set_sync_cursor(acc_id, epoch, aliases, highwater)
                    return {"messages": by_alias, "bodies": bodies}
                except Exception as exc:
                    from icloud_mail import MailAuthenticationError
                    self._drop_mail_client(acc_id)
                    if isinstance(exc, MailAuthenticationError) or attempt:
                        raise
        return {"messages": {}, "bodies": {}}

    @staticmethod
    def _match_alias(header: Dict, aliases) -> Optional[str]:
        return next(iter(AccountManager._match_aliases(header, aliases)), None)

    @staticmethod
    def _match_aliases(header: Dict, aliases) -> List[str]:
        recipients = {
            str(value).strip().lower()
            for value in header.get("recipients", [])
            if value
        }
        if not recipients:
            from email.utils import getaddresses
            recipients = {
                address.strip().lower()
                for _, address in getaddresses([header.get("to", "")])
                if address
            }
        return sorted(alias for alias in aliases if alias in recipients)

    def fetch_pickup_message(self, acc_id: str, msg_id: str, expected_epoch=None) -> Dict:
        """Fetch one full message through the account's persistent IMAP session."""
        with self._mail_sync_lock(acc_id):
            self._require_mail_sync(acc_id)
            for attempt in range(2):
                with self._lock:
                    mail = self._mail_clients.get(acc_id)
                if not mail:
                    mail = self.get_mail_client(acc_id)
                    with self._lock:
                        self._mail_clients[acc_id] = mail
                try:
                    ensure = getattr(mail, '_ensure_connected', None)
                    if callable(ensure):
                        ensure()
                    epoch = getattr(mail, 'uidvalidity', None)
                    self._observe_mail_epoch(acc_id, epoch)
                    if expected_epoch is not None and str(epoch) != str(expected_epoch):
                        raise RuntimeError('邮箱已重置，请刷新邮件列表')
                    full = mail.fetch_full(str(msg_id).encode()) or {}
                    if full:
                        full['_uidvalidity'] = epoch
                    return full
                except Exception as exc:
                    from icloud_mail import MailAuthenticationError
                    self._drop_mail_client(acc_id)
                    if isinstance(exc, MailAuthenticationError) or attempt:
                        raise
        return {}

    def test_imap_connection(self, acc_id: str, allow_paused=False) -> Dict:
        try:
            mail = self.get_mail_client(acc_id, allow_paused=allow_paused)
            result = mail.test_connection()
            mail.disconnect()
            return result
        except Exception as e:
            return {"ok": False, "error": str(e)[:200]}

    @property
    def creation_guard(self):
        from create_guard import CreationGuard
        with self._lock:
            if not hasattr(self, "_creation_guard"):
                self._creation_guard = CreationGuard(ACCOUNTS_FILE.parent / "results" / "creation_guard.json")
            return self._creation_guard

    def create_aliases_for_account(
        self, acc_id: str, count: int = 1, label: str = "",
        progress_callback=None, should_stop=None, wait=None,
        interval_seconds=None,
    ) -> List[Dict]:
        guard = self.creation_guard
        # Same-account duplicates fail immediately; other accounts wait for a global slot.
        with guard.lock:
            if acc_id in guard.active or acc_id in guard.queued:
                return [guard.result("busy", "该账号已有创建任务")]
            guard.queued.add(acc_id)
        try:
            while not guard.claim(acc_id):
                if callable(should_stop) and should_stop():
                    return []
                if callable(wait):
                    wait(0.5)
                else:
                    time.sleep(0.5)
            with self._operation_lock(acc_id):
                return self._create_aliases_for_account_unlocked(
                    acc_id, count, label, progress_callback, should_stop, wait,
                    interval_seconds,
                )
        finally:
            guard.release(acc_id)

    def _create_aliases_for_account_unlocked(
        self, acc_id: str, count: int = 1, label: str = "",
        progress_callback=None, should_stop=None, wait=None,
        interval_seconds=None,
    ) -> List[Dict]:
        from icloud_hme import ICloudHME

        account = self.accounts.get(acc_id)
        if not account:
            raise KeyError(f"账号不存在: {acc_id}")

        client = ICloudHME(
            account["cookies"],
            host=account.get("host", "icloud.com"),
            verbose=False,
        )

        guard = self.creation_guard
        try:
            create_interval = (
                CREATE_ALIAS_INTERVAL_SECONDS
                if interval_seconds is None
                else max(0.0, min(float(interval_seconds), 30.0))
            )
        except (TypeError, ValueError):
            create_interval = CREATE_ALIAS_INTERVAL_SECONDS
        task_id = getattr(progress_callback, "task_id", None)
        client.before_reserve = lambda email: guard.pending(acc_id, email, task_id)
        results: List[Dict] = []
        for i in range(count):
            if callable(should_stop) and should_stop():
                break
            try:
                alias_label = label or (
                    f"{account.get('name', acc_id)} "
                    f"{datetime.now().strftime('%m%d%H%M')}-{i + 1}"
                )
                blocked = guard.check(acc_id, account)
                if blocked:
                    results.append(dict(blocked, account_id=acc_id))
                    break
                pending = guard.snapshot()["accounts"].get(acc_id, {}).get("pending")
                if pending:
                    pending_task = guard.snapshot()["accounts"][acc_id].get("pending_task")
                    if pending_task and pending_task != task_id:
                        raise RuntimeError("上次创建属于其他任务，请先恢复原任务核对结果")
                    client._creation_mode = True
                    try:
                        aliases = client.list_aliases()
                    finally:
                        client._creation_mode = False
                    match = next((a for a in aliases if a.get("email", "").lower() == pending.lower()), None)
                    if not match:
                        raise RuntimeError("未能确认上次保留结果，请人工核对，禁止重复创建")
                    result = {"email": pending}
                else:
                    guard.attempt(acc_id)
                    result = client.create_alias(label=alias_label, max_retries=1)
                email = result.get("email", "")
                if email:
                    created_at = result.get("created_at") or datetime.now().astimezone().isoformat()
                    completed = {"email": email, "account_id": acc_id,
                                 "ok": True, "created_at": created_at}
                    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
                    with self._latest_emails_lock:
                        # A crash after the local append can leave a pending reserve.
                        # Reconciliation must not append that address a second time.
                        known = set()
                        if pending and LATEST_EMAILS.exists():
                            known = {line.split("\t", 1)[0].lower()
                                     for line in LATEST_EMAILS.read_text(encoding="utf-8").splitlines()}
                        if email.lower() not in known:
                            with open(str(LATEST_EMAILS), "a", encoding="utf-8") as f:
                                f.write(f"{email}\t{acc_id}\t{created_at}\n")
                                f.flush()
                                os.fsync(f.fileno())
                    guard.success(acc_id, email, task_id)
                    results.append(completed)
                    account["alias_total"] = account.get("alias_total", 0) + 1
                    account["alias_active"] = account.get("alias_active", 0) + 1
                    account["create_status"] = "available"
                    account["create_last_error"] = None
                    account["create_limited_at"] = None
                    if progress_callback:
                        try:
                            progress_callback(dict(results[-1]))
                        except Exception:
                            results.append(guard.result("local", "创建已记录，但任务进度保存失败；请检查存储后恢复"))
                            break
                    if i < count - 1 and create_interval > 0:
                        delay = (
                            create_interval
                            + random.uniform(0, CREATE_ALIAS_JITTER_SECONDS)
                        )
                        if callable(wait):
                            wait(delay)
                        else:
                            time.sleep(delay)
                        if callable(should_stop) and should_stop():
                            break
                else:
                    raise RuntimeError("create_alias 返回空邮箱，结果不明确")
            except Exception as e:
                failure = guard.failure(acc_id, e)
                results.append(dict(failure, email=None, account_id=acc_id))
                account["create_last_error"] = failure["error"]
                account["create_status"] = "cooldown" if failure["retryable"] else "limited"
                account["create_limited_at"] = datetime.now().isoformat()
                break

        try:
            self._save()
        finally:
            close = getattr(client, "close", None)
            if callable(close):
                close()
        return results

    def create_aliases_batch(
        self,
        account_ids: List[str],
        count_per_account: int = 1,
        interval_sec: float = 3.0,
        label: str = "",
    ) -> Dict[str, List[Dict]]:
        all_results: Dict[str, List[Dict]] = {}
        for i, acc_id in enumerate(account_ids):
            if acc_id not in self.accounts:
                all_results[acc_id] = [{
                    "email": None, "account_id": acc_id,
                    "ok": False, "error": "账号不存在",
                }]
                continue
            if self.accounts[acc_id].get("status") != "active":
                all_results[acc_id] = [{
                    "email": None, "account_id": acc_id,
                    "ok": False, "error": "账号不可用",
                }]
                continue

            results = self.create_aliases_for_account(
                acc_id, count_per_account, label
            )
            all_results[acc_id] = results

            if i < len(account_ids) - 1 and interval_sec > 0:
                time.sleep(interval_sec)

        return all_results

    def get_aliases_for_account(
        self, acc_id: str, raise_errors: bool = False
    ) -> List[Dict]:
        try:
            with self._operation_lock(acc_id):
                client = self.get_client(acc_id, verbose=False)
                try:
                    return client.list_aliases()
                finally:
                    close = getattr(client, "close", None)
                    if callable(close):
                        close()
        except Exception:
            if raise_errors:
                raise
            return []

    def get_all_aliases_with_status(self, max_workers: int = 5):
        """Fetch accounts concurrently and retain per-account failures."""
        all_aliases: List[Dict] = []
        statuses: Dict[str, Dict] = {}
        accounts = list(self.accounts.items())
        if not accounts:
            return all_aliases, statuses

        workers = max(1, min(int(max_workers or 1), 5, len(accounts)))
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(
                    self.get_aliases_for_account, acc_id, True
                ): (acc_id, account)
                for acc_id, account in accounts
            }
            for future in as_completed(futures):
                acc_id, account = futures[future]
                try:
                    aliases = future.result()
                    statuses[acc_id] = {
                        "ok": True,
                        "count": len(aliases),
                        "name": account.get("name", ""),
                    }
                except Exception as exc:
                    statuses[acc_id] = {
                        "ok": False,
                        "count": 0,
                        "name": account.get("name", ""),
                        "error": str(exc)[:200],
                    }
                    continue
                for alias in aliases:
                    alias = dict(alias)
                    alias["account_id"] = acc_id
                    alias["account_name"] = account.get("name", "")
                    alias["account_email"] = account.get("real_email", "")
                    all_aliases.append(alias)
        return all_aliases, statuses

    def get_all_aliases(self) -> List[Dict]:
        aliases, _statuses = self.get_all_aliases_with_status()
        return aliases

    def get_summary(self) -> Dict:
        total_aliases = sum(
            a.get("alias_total", 0) for a in self.accounts.values()
        )
        total_active = sum(
            a.get("alias_active", 0) for a in self.accounts.values()
        )
        active_accounts = sum(
            1 for a in self.accounts.values() if a.get("status") == "active"
        )
        error_accounts = sum(
            1 for a in self.accounts.values() if a.get("status") == "error"
        )
        return {
            "account_count": len(self.accounts),
            "active_accounts": active_accounts,
            "error_accounts": error_accounts,
            "total_aliases": total_aliases,
            "total_active_aliases": total_active,
        }


if __name__ == "__main__":
    print("AccountManager 自测")
    mgr = AccountManager()
    summary = mgr.get_summary()
    print(f"当前账号数: {summary['account_count']}")

    header = "X_APPLE_WEB_KB=abc123; SESSION_TOKEN=xyz789"
    parsed = mgr.parse_cookie_input(header)
    print(f"Header String → {len(parsed)} 个 cookie")

    json_in = '{"X_APPLE_WEB_KB":"abc123","SESSION_TOKEN":"xyz789"}'
    parsed2 = mgr.parse_cookie_input(json_in)
    print(f"JSON → {len(parsed2)} 个 cookie")

    print("自测完成 ✓")
