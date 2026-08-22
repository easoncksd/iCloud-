#!/usr/bin/env python3
"""
iCloud Mail — IMAP 收件箱检查模块
===================================
通过 Apple 应用专用密码连接 iCloud Mail IMAP，
查询隐私邮箱别名收到的邮件。

用法:
    from icloud_mail import ICloudMail

    mail = ICloudMail("user@icloud.com", "app-specific-password")
    emails = mail.check_inbox(limit=20)
    # 查找某个隐私别名收到的邮件
    alias_mail = mail.find_by_recipient("alias@icloud.com")

前提:
  - 需要在 appleid.apple.com 生成「App 专用密码」
  - iCloud 邮箱已开启 IMAP 访问
"""

import imaplib
import email
import time
from datetime import datetime, timedelta
from email.header import decode_header
from email.utils import parsedate_to_datetime, getaddresses
from typing import Optional, Dict, List

IMAP_SERVER = "imap.mail.me.com"
IMAP_PORT = 993
# Keep a failed IMAP handshake from blocking the Web UI for a long time.
IMAP_TIMEOUT = 10


class ICloudMail:
    """iCloud Mail IMAP 客户端"""

    def __init__(self, apple_id: str, app_password: str, verbose: bool = False):
        self.apple_id = apple_id
        self.app_password = app_password
        self.verbose = verbose
        self._conn: Optional[imaplib.IMAP4_SSL] = None

    def connect(self) -> bool:
        try:
            self._conn = imaplib.IMAP4_SSL(IMAP_SERVER, IMAP_PORT, timeout=IMAP_TIMEOUT)
            self._conn.login(self.apple_id, self.app_password)
            if self.verbose:
                print(f"[IMAP] Connected as {self.apple_id}")
            return True
        except imaplib.IMAP4.error as e:
            msg = str(e)
            if "authentication" in msg.lower() or "login" in msg.lower():
                raise RuntimeError(
                    f"IMAP 登录失败 — 请检查:\n"
                    f"  1. 应用专用密码是否正确\n"
                    f"  2. Apple ID: {self.apple_id}\n"
                    f"  3. 是否已在 appleid.apple.com 生成密码"
                )
            raise RuntimeError(f"IMAP 连接失败: {msg}")
        except Exception as e:
            raise RuntimeError(f"IMAP 连接失败: {e}")

    def disconnect(self):
        if self._conn:
            try:
                self._conn.logout()
            except Exception:
                pass
            self._conn = None

    @property
    def connected(self) -> bool:
        return self._conn is not None and self._conn.state == "SELECTED"

    def _ensure_connected(self):
        if not self._conn:
            self.connect()
        if self._conn.state != "SELECTED":
            self._conn.select("INBOX", readonly=True)

    def check_inbox(self, limit: int = 50, days: int = 7) -> List[Dict]:
        self._ensure_connected()
        return self._search_and_fetch(None, limit, days)

    def check_unread(self, limit: int = 50, days: int = 7) -> List[Dict]:
        self._ensure_connected()
        return self._search_and_fetch("UNSEEN", limit, days)

    def find_by_recipient(self, recipient: str, limit: int = 20, days: int = 30) -> List[Dict]:
        self._ensure_connected()
        needle = (recipient or "").strip().lower()
        if not needle:
            return []

        def matches(message: Dict) -> bool:
            recipients = {
                str(value).strip().lower()
                for value in message.get("recipients") or []
                if value
            }
            if needle in recipients:
                return True
            return needle in str(message.get("to") or "").lower()

        try:
            searched = self._search_and_fetch('TO "%s"' % needle, limit, days)
        except Exception:
            searched = []
        matched = [message for message in searched if matches(message)]
        if matched:
            matched.sort(key=lambda message: str(message.get("date") or ""), reverse=True)
            return matched[:limit]

        all_msgs = self._search_and_fetch(None, max(limit * 5, 80), days)
        matched = [message for message in all_msgs if matches(message)]
        matched.sort(key=lambda message: str(message.get("date") or ""), reverse=True)
        return matched[:limit]

    def recent_uids(self, limit: int = 100, days: int = 30) -> List[bytes]:
        """Return recent message UIDs newest first without fetching message data."""
        self._ensure_connected()
        since = (datetime.now() - timedelta(days=days)).strftime("%d-%b-%Y")
        status, data = self._conn.uid("SEARCH", None, f'(SINCE "{since}")')
        if status != "OK" or not data[0]:
            return []
        uids = data[0].split()
        return list(reversed(uids[-limit:]))

    def fetch_header(self, uid: bytes) -> Optional[Dict]:
        self._ensure_connected()
        return self._fetch_headers_uid(uid)

    def stream_inbox(self, limit: int = 50, days: int = 7):
        self._ensure_connected()
        since = (datetime.now() - timedelta(days=days)).strftime("%d-%b-%Y")
        full = f'(SINCE "{since}")'
        status, data = self._conn.uid("SEARCH", None, full)
        if status != "OK" or not data[0]:
            return
        uids = data[0].split()
        recent = uids[-limit:] if len(uids) > limit else uids
        for uid in reversed(recent):
            try:
                msg = self._fetch_headers_uid(uid)
                if msg:
                    yield msg
            except Exception:
                continue

    def _search_and_fetch(self, criteria: Optional[str], limit: int, days: int) -> List[Dict]:
        since = (datetime.now() - timedelta(days=days)).strftime("%d-%b-%Y")
        full = f'({criteria} SINCE "{since}")' if criteria else f'(SINCE "{since}")'
        status, data = self._conn.uid("SEARCH", None, full)
        if status != "OK" or not data[0]:
            return []
        uids = data[0].split()
        recent = uids[-limit:] if len(uids) > limit else uids
        emails: List[Dict] = []
        for uid in reversed(recent):
            try:
                msg = self._fetch_headers_uid(uid)
                if msg:
                    emails.append(msg)
            except Exception:
                continue
        return emails

    def _fetch_headers(self, msg_id: bytes) -> Optional[Dict]:
        status, data = self._conn.fetch(msg_id, "(BODY.PEEK[HEADER])")
        if status != "OK":
            return None
        return self._parse_header_response(data, msg_id)

    def _fetch_headers_uid(self, uid: bytes) -> Optional[Dict]:
        status, data = self._conn.uid("FETCH", uid, "(BODY.PEEK[HEADER])")
        if status != "OK":
            return None
        return self._parse_header_response(data, uid)

    def _parse_header_response(self, data, msg_id: bytes) -> Optional[Dict]:
        raw = self._extract_body(data)
        if not raw:
            return None
        try:
            msg = email.message_from_bytes(raw + b"\r\n\r\n")
        except Exception:
            return None
        recipient_headers = [
            msg.get(name, "")
            for name in ("To", "Cc", "Delivered-To", "X-Original-To", "Envelope-To", "X-Envelope-To")
            if msg.get(name)
        ]
        recipients = [address.lower() for _, address in getaddresses(recipient_headers) if address]
        return {
            "id": msg_id.decode() if isinstance(msg_id, bytes) else str(msg_id),
            "from": self._decode_header(msg.get("From", "")),
            "to": self._decode_header(msg.get("To", "")),
            "recipients": recipients,
            "subject": self._decode_header(msg.get("Subject", "")),
            "date": self._safe_date(msg.get("Date", "")),
            "body_preview": "",
            "size": len(raw),
        }

    def fetch_body(self, msg_id: bytes) -> Optional[str]:
        self._ensure_connected()
        status, data = self._conn.uid("FETCH", msg_id, "(BODY.PEEK[TEXT])")
        if status != "OK":
            return None
        raw = self._extract_body(data)
        if not raw:
            return None
        try:
            return raw.decode("utf-8", errors="replace")
        except Exception:
            return raw.decode("latin-1", errors="replace")

    def fetch_full(self, msg_id: bytes) -> Optional[Dict]:
        self._ensure_connected()
        return self._fetch_full_message(msg_id)

    def _fetch_full_message(self, msg_id: bytes) -> Optional[Dict]:
        status, data = self._conn.uid("FETCH", msg_id, "(BODY.PEEK[])")
        if status != "OK":
            status, data = self._conn.uid("FETCH", msg_id, "(RFC822)")
            if status != "OK":
                return None
        raw = self._extract_body(data)
        if not raw:
            return None
        try:
            em = email.message_from_bytes(raw)
        except Exception:
            return None

        body = ""
        html_body = ""
        if em.is_multipart():
            for part in em.walk():
                if part.get_content_maintype() == 'multipart':
                    continue
                ctype = part.get_content_type().split(';')[0].strip().lower()
                try:
                    payload = part.get_payload(decode=True)
                    if not payload:
                        continue
                    charset = part.get_content_charset() or "utf-8"
                    text = payload.decode(charset, errors="replace")
                    if ctype == "text/plain" and not body:
                        body = text
                    elif ctype == "text/html" and not html_body:
                        html_body = text
                except Exception:
                    pass
        else:
            try:
                payload = em.get_payload(decode=True)
                if payload:
                    charset = em.get_content_charset() or "utf-8"
                    text = payload.decode(charset, errors="replace")
                    ctype = em.get_content_type().split(';')[0].strip().lower()
                    if ctype == "text/plain":
                        body = text
                    elif ctype == "text/html":
                        html_body = text
                    else:
                        body = text
            except Exception:
                pass

        if not body and html_body:
            body = _strip_html(html_body)

        recipient_headers = [
            em.get(name, "")
            for name in (
                "To", "Cc", "Delivered-To", "X-Original-To",
                "Envelope-To", "X-Envelope-To",
            )
            if em.get(name)
        ]
        recipients = [
            address.lower()
            for _, address in getaddresses(recipient_headers)
            if address
        ]

        return {
            "id": msg_id.decode() if isinstance(msg_id, bytes) else str(msg_id),
            "from": self._decode_header(em.get("From", "")),
            "to": self._decode_header(em.get("To", "")),
            "recipients": recipients,
            "subject": self._decode_header(em.get("Subject", "")),
            "date": self._safe_date(em.get("Date", "")),
            "body": body[:10000],
            "html": html_body[:200000],
            "content_type": em.get_content_type(),
            "size": len(raw),
        }

    @staticmethod
    def _extract_body(data: list) -> Optional[bytes]:
        for item in data:
            if isinstance(item, tuple):
                for sub in item:
                    if isinstance(sub, bytes) and len(sub) > 500:
                        return sub
        for item in data:
            if isinstance(item, bytes) and len(item) > 500:
                return item
        best = None
        for item in data:
            if isinstance(item, bytes):
                if best is None or len(item) > len(best):
                    best = item
            elif isinstance(item, tuple):
                for sub in item:
                    if isinstance(sub, bytes):
                        if best is None or len(sub) > len(best):
                            best = sub
        return best if best and len(best) > 100 else None

    @staticmethod
    def _decode_header(value: str) -> str:
        if not value:
            return ""
        parts = decode_header(value)
        result = []
        for part, charset in parts:
            if isinstance(part, bytes):
                try:
                    result.append(part.decode(charset or "utf-8", errors="replace"))
                except Exception:
                    result.append(part.decode("utf-8", errors="replace"))
            else:
                result.append(str(part))
        return " ".join(result)

    def test_connection(self) -> Dict:
        try:
            self.connect()
            status, data = self._conn.select("INBOX", readonly=True)
            if status != "OK":
                return {"ok": False, "error": "无法选中 INBOX"}
            msg_count = int(data[0]) if data else 0
            self.disconnect()
            return {"ok": True, "email": self.apple_id, "inbox_count": msg_count}
        except Exception as e:
            return {"ok": False, "error": str(e)[:200]}
        finally:
            self.disconnect()

    @staticmethod
    def _safe_date(date_str: str) -> str:
        try:
            return parsedate_to_datetime(date_str).isoformat()
        except Exception:
            return date_str


def _strip_html(html: str) -> str:
    import re
    html = re.sub(r'<head[^>]*>.*?</head>', '', html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r'<(script|style|noscript)[^>]*>.*?</\1>', '', html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r'<br\s*/?>', '\n', html, flags=re.IGNORECASE)
    html = re.sub(r'</?(div|p|h[1-6]|li|tr|article|section|header|footer|blockquote|pre|table|hr)[^>]*>', '\n', html, flags=re.IGNORECASE)
    html = re.sub(r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', r'\2 (\1)', html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r'<li[^>]*>', '• ', html, flags=re.IGNORECASE)
    html = re.sub(r'<[^>]+>', '', html)
    import html as _html
    text = _html.unescape(html)
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    text = re.sub(r'^\s+', '', text, flags=re.MULTILINE)
    return text.strip()


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 3:
        print("用法: python icloud_mail.py <apple_id> <app_password> [alias_email]")
        sys.exit(1)
    apple_id, app_pwd = sys.argv[1], sys.argv[2]
    alias = sys.argv[3] if len(sys.argv) > 3 else None
    mail = ICloudMail(apple_id, app_pwd, verbose=True)
    result = mail.test_connection()
    if result["ok"]:
        print(f"连接成功! 收件箱: {result['inbox_count']} 封")
    else:
        print(f"连接失败: {result['error']}")
        sys.exit(1)
    if alias:
        msgs = mail.find_by_recipient(alias, limit=10)
        for m in msgs:
            print(f"  [{m['date'][:19]}] {m['from'][:30]} | {m['subject'][:40]}")
    else:
        msgs = mail.check_inbox(limit=10)
        for m in msgs:
            print(f"  [{m['date'][:19]}] {m['from'][:30]} | {m['subject'][:40]}")
    mail.disconnect()
