#!/usr/bin/env python3
"""
iCloud Hide My Email — 隐私邮箱创建工具
==========================================
基于 iCloud 纯协议实现，无需浏览器自动化。
从 Chrome 自动提取登录态，一键创建 @icloud.com 隐私邮箱别名。

用法:
    # 创建一个新的隐私邮箱
    python icloud_hme.py create

    # 批量创建 N 个
    python icloud_hme.py create -n 5

    # 列出所有已有别名
    python icloud_hme.py list

    # 删除指定别名
    python icloud_hme.py delete --email alias@icloud.com

    # 使用手动导出的 cookies 文件
    python icloud_hme.py create --cookies cookies.json

    # 导出 Chrome cookies 到文件（方便服务器复用）
    python icloud_hme.py export-cookies -o cookies.json

依赖 (Windows):
    pip install requests pycryptodome pywin32

依赖 (macOS/Linux 仅支持 --cookies 模式):
    pip install requests
"""

import sys
import os
import json
import re
import time
import sqlite3
import argparse
import hashlib
import base64
import secrets
from datetime import datetime
from typing import Optional, Dict, List, Any
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

import requests

# ============================================================
# 常量
# ============================================================

CLIENT_BUILD_NUMBER = "2206Hotfix11"
REQUEST_TIMEOUT = 15
MAX_RETRIES = 3
RETRY_DELAYS = [1, 2.5, 5]

ICLOUD_COOKIE_DOMAINS = [
    ".icloud.com", ".icloud.com.cn",
    "icloud.com", "icloud.com.cn",
    "setup.icloud.com", "setup.icloud.com.cn",
    "www.icloud.com", "www.icloud.com.cn",
]


# ============================================================
# Cookie 提取 (Chrome)
# ============================================================

def _get_chrome_cookie_path() -> Optional[str]:
    """查找 Chrome Cookie 数据库路径"""
    local_appdata = os.environ.get("LOCALAPPDATA", "")
    candidates = []
    if local_appdata:
        base = os.path.join(local_appdata, "Google", "Chrome", "User Data")
        candidates = [
            os.path.join(base, "Default", "Network", "Cookies"),
            os.path.join(base, "Default", "Cookies"),
        ]
    # macOS
    home = os.path.expanduser("~")
    candidates.append(
        os.path.join(home, "Library", "Application Support", "Google", "Chrome", "Default", "Cookies")
    )
    # Linux
    candidates.append(
        os.path.join(home, ".config", "google-chrome", "Default", "Cookies")
    )
    for p in candidates:
        if os.path.isfile(p):
            return p
    return None


def _get_chrome_key() -> Optional[bytes]:
    """获取 Chrome 加密密钥"""
    local_appdata = os.environ.get("LOCALAPPDATA", "")
    if not local_appdata:
        return None

    state_path = os.path.join(local_appdata, "Google", "Chrome", "User Data", "Local State")
    if not os.path.isfile(state_path):
        return None

    with open(state_path, "r", encoding="utf-8") as f:
        state = json.load(f)

    encrypted_key_b64 = state.get("os_crypt", {}).get("encrypted_key", "")
    if not encrypted_key_b64:
        return None

    encrypted_key = base64.b64decode(encrypted_key_b64)
    if len(encrypted_key) < 6:
        return None
    encrypted_key = encrypted_key[5:]  # 去掉 "DPAPI" 前缀

    # 方式1: pywin32
    try:
        import win32crypt
        return win32crypt.CryptUnprotectData(encrypted_key, None, None, None, 0)[1]
    except ImportError:
        pass

    # 方式2: ctypes 直调 crypt32.dll
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [
            ("cbData", wintypes.DWORD),
            ("pbData", ctypes.POINTER(ctypes.c_char)),
        ]

    crypt32 = ctypes.windll.crypt32
    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(DATA_BLOB), ctypes.c_wchar_p,
        ctypes.POINTER(DATA_BLOB), ctypes.c_void_p,
        ctypes.c_void_p, wintypes.DWORD,
        ctypes.POINTER(DATA_BLOB),
    ]
    crypt32.CryptUnprotectData.restype = wintypes.BOOL

    blob_in = DATA_BLOB(len(encrypted_key), ctypes.c_char_p(encrypted_key))
    blob_out = DATA_BLOB()
    if crypt32.CryptUnprotectData(
        ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
    ):
        result = ctypes.string_at(blob_out.pbData, blob_out.cbData)
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)
        return result
    return None


def extract_chrome_cookies() -> Dict[str, str]:
    """从 Chrome 提取 iCloud 相关 cookie"""
    cookie_path = _get_chrome_cookie_path()
    if not cookie_path:
        raise RuntimeError(
            "找不到 Chrome Cookie 数据库。\n"
            "请确保: 1) 已安装 Chrome  2) 已在 Chrome 登录 icloud.com\n"
            "或使用 --cookies 参数手动提供 cookies.json"
        )

    key = _get_chrome_key()
    if not key:
        raise RuntimeError("无法获取 Chrome 加密密钥 (非 Windows 系统请使用 --cookies)")

    from Crypto.Cipher import AES

    conn = sqlite3.connect(f"file:{cookie_path}?mode=ro", uri=True)
    try:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        placeholders = ",".join("?" * len(ICLOUD_COOKIE_DOMAINS))
        cursor.execute(
            f"SELECT name, encrypted_value FROM cookies WHERE host_key IN ({placeholders})",
            ICLOUD_COOKIE_DOMAINS,
        )
        rows = cursor.fetchall()
    finally:
        conn.close()

    cookies = {}
    for row in rows:
        name = row["name"]
        encrypted = row["encrypted_value"]
        if not encrypted:
            continue
        value = _decrypt_chrome_value(encrypted, key)
        if value:
            cookies[name] = value
    return cookies


def _decrypt_chrome_value(encrypted_value: bytes, key: bytes) -> Optional[str]:
    """解密 Chrome v10/v11 cookie (AES-256-GCM)"""
    from Crypto.Cipher import AES

    if len(encrypted_value) < 3:
        return None
    prefix = encrypted_value[:3]
    if prefix in (b"v10", b"v11"):
        nonce = encrypted_value[3:15]
        ciphertext = encrypted_value[15:-16]
        tag = encrypted_value[-16:]
        if len(ciphertext) < 1:
            return None
        try:
            cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
            return cipher.decrypt_and_verify(ciphertext, tag).decode("utf-8", errors="replace")
        except Exception:
            return None
    return None


# ============================================================
# iCloud HME API 客户端
# ============================================================

class ICloudHME:
    """iCloud Hide My Email 客户端"""

    def __init__(self, cookies: Dict[str, str], host: str = "icloud.com", verbose: bool = True):
        self.cookies = cookies
        self.host = self._normalize_host(host)
        self.verbose = verbose
        self.session = requests.Session()
        self.session.cookies.update(cookies)
        self._setup_url: Optional[str] = None
        self._service_url: Optional[str] = None
        self._account_info: Optional[Dict] = None

    # ---- 内部 ----

    @staticmethod
    def _normalize_host(host: str) -> str:
        h = host.strip().lower()
        try:
            h = urlparse(h if "://" in h else f"https://{h}").hostname or h
        except Exception:
            pass
        return "icloud.com.cn" if (h.endswith(".icloud.com.cn") or h == "icloud.com.cn") else "icloud.com"

    @property
    def setup_url(self) -> str:
        if not self._setup_url:
            suffix = "setup.icloud.com.cn" if self.host == "icloud.com.cn" else "setup.icloud.com"
            self._setup_url = f"https://{suffix}/setup/ws/1"
        return self._setup_url

    @property
    def origin(self) -> str:
        return f"https://www.{self.host}"

    def _log(self, msg: str):
        if self.verbose:
            print(f"  [iCloud] {msg}")

    def _build_url(self, url: str) -> str:
        parsed = urlparse(url)
        params = parse_qs(parsed.query, keep_blank_values=True)
        params["clientBuildNumber"] = [CLIENT_BUILD_NUMBER]
        params["clientMasteringNumber"] = [CLIENT_BUILD_NUMBER]
        return urlunparse(parsed._replace(query=urlencode(params, doseq=True)))

    def _request(self, method: str, url: str, json_data: Any = None,
                 timeout: int = REQUEST_TIMEOUT, max_attempts: int = MAX_RETRIES) -> Any:
        full_url = self._build_url(url)
        headers = {
            "Origin": self.origin,
            "Referer": self.origin + "/",
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "text/plain;charset=UTF-8" if "maildomainws" in urlparse(url).hostname
            else "application/json",
        }

        body = json.dumps(json_data, ensure_ascii=False) if json_data is not None else None
        last_err = None

        for attempt in range(1, max_attempts + 1):
            try:
                resp = self.session.request(method, full_url, headers=headers, data=body, timeout=timeout)
                if not resp.ok:
                    last_err = RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
                    if resp.status_code in (401, 403):
                        raise last_err
                    if attempt < max_attempts:
                        time.sleep(RETRY_DELAYS[min(attempt - 1, len(RETRY_DELAYS) - 1)])
                        continue
                    raise last_err
                text = resp.text
                return resp.json() if text else {}
            except requests.exceptions.Timeout:
                last_err = RuntimeError(f"超时 ({timeout}s)")
                if attempt < max_attempts:
                    time.sleep(RETRY_DELAYS[min(attempt - 1, len(RETRY_DELAYS) - 1)])
                    continue
                raise last_err
            except requests.exceptions.ConnectionError as e:
                last_err = RuntimeError(f"连接失败: {e}")
                if attempt < max_attempts:
                    time.sleep(RETRY_DELAYS[min(attempt - 1, len(RETRY_DELAYS) - 1)])
                    continue
                raise last_err
        raise last_err or RuntimeError("未知错误")

    # ---- 会话 ----

    def validate_session(self) -> Dict:
        """校验 iCloud 会话，获取 Hide My Email 服务端点及账号身份"""
        self._log("校验 iCloud 会话...")
        data = self._request("POST", f"{self.setup_url}/validate", timeout=20)
        premium = data.get("webservices", {}).get("premiummailsettings", {})
        if not premium.get("url"):
            raise RuntimeError(
                "iCloud 会话校验失败 — 可能原因:\n"
                "  1. 未开通 iCloud+ 订阅 (Hide My Email 需要 iCloud+)\n"
                "  2. Cookie 已过期，请在 Chrome 重新登录 icloud.com\n"
                "  3. 网络问题"
            )
        self._service_url = premium["url"].rstrip("/")

        # 提取账号身份信息
        ds_info = data.get("dsInfo", {})
        self._account_info = {
            "dsid": str(ds_info.get("dsid", "")),
            "appleId": str(ds_info.get("appleId", "") or ds_info.get("primaryEmail", "") or ds_info.get("appleIdEmail", "")),
            "primaryEmail": str(ds_info.get("primaryEmail", "") or ds_info.get("appleId", "")),
            "fullName": str(ds_info.get("fullName", "") or ds_info.get("name", "")),
            "isManagedAppleId": bool(ds_info.get("isManagedAppleId", False)),
        }
        if not self._account_info["appleId"]:
            # fallback: 从 cookie 推断
            for name in ("aosappleid", "appleId", "dsid"):
                if name in self.cookies:
                    self._account_info["appleId"] = str(self.cookies[name])
                    break

        self._log(f"会话有效 → {self._account_info.get('appleId', '未知账号')}")
        return data

    def get_account_info(self) -> Optional[Dict]:
        """返回账号身份信息，需先调用 validate_session()。
        返回: {dsid, appleId, primaryEmail, fullName, isManagedAppleId} 或 None"""
        return self._account_info

    def _resolve_service(self):
        if not self._service_url:
            self.validate_session()

    # ---- 别名操作 ----

    def list_aliases(self) -> List[Dict]:
        """列出所有 Hide My Email 别名"""
        self._resolve_service()
        self._log("获取别名列表...")
        response = self._request("GET", f"{self._service_url}/v2/hme/list")
        aliases = self._parse_alias_list(response)
        self._log(f"共 {len(aliases)} 个别名")
        return aliases

    def generate(self) -> str:
        """生成候选别名 (未保留)"""
        self._resolve_service()
        self._log("生成候选别名...")
        response = self._request("POST", f"{self._service_url}/v1/hme/generate", max_attempts=2)
        if not response.get("success"):
            err = response.get("error", {})
            raise RuntimeError(f"生成失败: {err.get('errorMessage', 'unknown')}")
        hme = response.get("result", {}).get("hme", "")
        if isinstance(hme, dict):
            hme = hme.get("hme") or hme.get("email") or ""
        self._log(f"候选: {hme}")
        return hme

    def reserve(self, hme: str, label: Optional[str] = None) -> str:
        """保留/确认候选别名"""
        self._resolve_service()
        if not label:
            label = f"Created {datetime.now().strftime('%Y-%m-%d %H:%M')}"
        self._log(f"保留别名 {hme} ...")
        data = {"hme": hme, "label": label, "note": "Created by icloud_hme tool"}
        response = self._request("POST", f"{self._service_url}/v1/hme/reserve", json_data=data, max_attempts=2)
        if not response.get("success"):
            err = response.get("error", {})
            raise RuntimeError(f"保留失败: {err.get('errorMessage', 'unknown')}")
        result = response.get("result", {}).get("hme", {})
        alias = result.get("hme", hme) if isinstance(result, dict) else hme
        self._log(f"已保留: {alias}")
        return alias

    def create_alias(self, label: Optional[str] = None, max_retries: int = 5) -> Dict:
        """生成 + 保留，一步创建。返回 {'email': ..., 'label': ...}"""
        last_err = ""
        for attempt in range(max_retries):
            if attempt > 0:
                self._service_url = None
                self._setup_url = None
                self._log(f"重试 {attempt+1}/{max_retries} ...")
            try:
                hme = self.generate()
            except Exception as e:
                last_err = f"generate 失败: {e}"
                self._log(last_err)
                if attempt < max_retries - 1:
                    time.sleep(1)
                    continue
                break
            try:
                email = self.reserve(hme, label)
                return {"email": email, "label": label or "", "created_at": datetime.now().isoformat()}
            except Exception as e:
                last_err = str(e)
                self._log(f"reserve 失败: {last_err}")
                lower = last_err.lower()
                if any(marker in lower for marker in (
                    "reached the limit of addresses",
                    "maximum number of addresses",
                    "address limit",
                    "quota exceeded",
                    "too many addresses",
                )):
                    break
                if attempt < max_retries - 1:
                    time.sleep(1)
                    continue
        raise RuntimeError(f"创建别名失败: {last_err}" if last_err else f"创建别名失败，已重试 {max_retries} 次")

    def delete(self, anonymous_id: str) -> bool:
        """删除别名 (必要时先停用再删除)"""
        self._resolve_service()
        self._log(f"删除 {anonymous_id} ...")
        try:
            resp = self._request("POST", f"{self._service_url}/v1/hme/delete",
                                 json_data={"anonymousId": anonymous_id}, max_attempts=2)
            if resp.get("success") is False:
                raise RuntimeError(resp.get("error", {}).get("errorMessage", "delete failed"))
        except Exception:
            self._log("直接删除失败，尝试先停用...")
            self._request("POST", f"{self._service_url}/v1/hme/deactivate",
                         json_data={"anonymousId": anonymous_id}, max_attempts=2)
            resp = self._request("POST", f"{self._service_url}/v1/hme/delete",
                                 json_data={"anonymousId": anonymous_id}, max_attempts=2)
            if resp.get("success") is False:
                raise RuntimeError(resp.get("error", {}).get("errorMessage", "delete failed"))
        self._log("已删除")
        return True

    # ---- 解析 ----

    @staticmethod
    def _parse_alias_list(response: Any) -> List[Dict]:
        aliases_raw = None
        if isinstance(response, dict):
            result = response.get("result", {})
            if isinstance(result, dict):
                hme = result.get("hmeEmails")
                if isinstance(hme, list):
                    aliases_raw = hme

        if not aliases_raw:
            def _find_dict_array(d, depth=0):
                if depth > 4 or d is None:
                    return None
                if isinstance(d, list) and len(d) > 0 and isinstance(d[0], dict):
                    return d
                if isinstance(d, dict):
                    for v in d.values():
                        r = _find_dict_array(v, depth + 1)
                        if r:
                            return r
                return None
            aliases_raw = _find_dict_array(response)

        if not aliases_raw:
            return []

        aliases = []
        for item in aliases_raw:
            if not isinstance(item, dict):
                continue
            email = str(
                item.get("hme") or item.get("email") or item.get("alias")
                or item.get("address") or item.get("metaData", {}).get("hme") or ""
            ).strip().lower()
            if not email or "@" not in email:
                continue
            state = str(item.get("state") or item.get("status") or "").lower()
            aliases.append({
                "email": email,
                "anonymousId": str(item.get("anonymousId") or item.get("id") or ""),
                "label": str(item.get("label") or item.get("metaData", {}).get("label") or ""),
                "active": item.get("active", True) and item.get("isActive", True)
                and state not in ("inactive", "deleted"),
                "createdAt": item.get("createTimestamp") or item.get("createdAt"),
            })
        aliases.sort(key=lambda a: (not a["active"], a["email"]))
        return aliases


# ============================================================
# CLI
# ============================================================

def _load_cookies(args) -> Dict[str, str]:
    if args.cookies:
        if not os.path.isfile(args.cookies):
            raise RuntimeError(f"Cookie 文件不存在: {args.cookies}")
        with open(args.cookies, "r", encoding="utf-8") as f:
            return json.load(f)
    print("[*] 从 Chrome 自动提取 iCloud cookies...")
    cookies = extract_chrome_cookies()
    if not cookies:
        raise RuntimeError("未提取到 iCloud cookies。请先在 Chrome 登录 icloud.com，或使用 --cookies 参数")
    print(f"[+] 已提取 {len(cookies)} 个 cookie")
    return cookies


def _make_client(args) -> ICloudHME:
    cookies = _load_cookies(args)
    return ICloudHME(cookies, host=getattr(args, "host", "icloud.com"), verbose=not args.quiet)


def cmd_create(args):
    """创建隐私邮箱"""
    client = _make_client(args)
    count = args.count

    results = []
    for i in range(count):
        if count > 1:
            print(f"\n--- [{i+1}/{count}] ---")
        try:
            result = client.create_alias(label=args.label, max_retries=args.retry)
            results.append(result)
            print(f"  ✅ {result['email']}")
        except Exception as e:
            print(f"  ❌ 创建失败: {e}")
            results.append({"email": None, "error": str(e)})

    # 输出汇总
    successes = [r for r in results if r.get("email")]
    if successes:
        print(f"\n{'='*50}")
        print(f"成功创建 {len(successes)}/{count} 个隐私邮箱:")
        for r in successes:
            print(f"  📧 {r['email']}")
        if args.output:
            with open(args.output, "w", encoding="utf-8") as f:
                json.dump(successes, f, indent=2, ensure_ascii=False)
            print(f"已保存到 {args.output}")


def cmd_list(args):
    """列出所有别名"""
    client = _make_client(args)
    aliases = client.list_aliases()
    if not aliases:
        print("没有找到任何别名")
        return

    print(f"\n共 {len(aliases)} 个 Hide My Email 别名:\n")
    print(f"{'状态':<6} {'邮箱':<40} {'标签':<30} {'创建时间'}")
    print("-" * 100)
    for a in aliases:
        status = "✅" if a["active"] else "❌"
        created = str(a.get("createdAt") or "")[:10]
        print(f"{status:<6} {a['email']:<40} {a['label'][:28]:<30} {created}")
    print(f"\n活跃: {sum(1 for a in aliases if a['active'])}  停用: {sum(1 for a in aliases if not a['active'])}")


def cmd_delete(args):
    """删除别名"""
    client = _make_client(args)
    aliases = client.list_aliases()

    target = None
    if args.email:
        target = next((a for a in aliases if a["email"] == args.email.lower()), None)
        if not target:
            print(f"未找到别名: {args.email}")
            sys.exit(1)
    elif args.id:
        target = next((a for a in aliases if a["anonymousId"] == args.id), None)
        if not target:
            print(f"未找到 anonymousId: {args.id}")
            sys.exit(1)
    else:
        print("请指定 --email 或 --id")
        sys.exit(1)

    if not args.force:
        confirm = input(f"确认删除 {target['email']}? [y/N] ")
        if confirm.lower() != "y":
            print("已取消")
            return

    client.delete(target["anonymousId"])
    print(f"已删除: {target['email']}")


def cmd_export_cookies(args):
    """导出 Chrome cookies 到文件"""
    cookies = extract_chrome_cookies()
    if not cookies:
        print("未提取到 iCloud cookies")
        sys.exit(1)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(cookies, f, indent=2, ensure_ascii=False)
    print(f"已导出 {len(cookies)} 个 cookie → {args.output}")


def main():
    parser = argparse.ArgumentParser(
        description="iCloud Hide My Email — 隐私邮箱创建工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python icloud_hme.py create                # 创建一个隐私邮箱
  python icloud_hme.py create -n 5 -o out.json  # 批量创建5个
  python icloud_hme.py list                  # 列出所有别名
  python icloud_hme.py delete --email a@icloud.com
  python icloud_hme.py export-cookies -o cookies.json
        """,
    )

    sub = parser.add_subparsers(dest="command")

    # ---- create ----
    p_create = sub.add_parser("create", help="创建隐私邮箱")
    p_create.add_argument("-n", "--count", type=int, default=1, help="创建数量 (默认 1)")
    p_create.add_argument("--label", type=str, help="别名标签")
    p_create.add_argument("--retry", type=int, default=5, help="失败重试次数 (默认 5)")
    p_create.add_argument("-o", "--output", type=str, help="结果输出 JSON 文件")
    _add_common_args(p_create)

    # ---- list ----
    p_list = sub.add_parser("list", help="列出所有别名")
    _add_common_args(p_list)

    # ---- delete ----
    p_del = sub.add_parser("delete", help="删除别名")
    p_del.add_argument("--email", type=str, help="别名邮箱")
    p_del.add_argument("--id", type=str, help="别名的 anonymousId")
    p_del.add_argument("--force", "-f", action="store_true", help="跳过确认")
    _add_common_args(p_del)

    # ---- export-cookies ----
    p_exp = sub.add_parser("export-cookies", help="导出 Chrome cookies")
    p_exp.add_argument("-o", "--output", default="icloud_cookies.json", help="输出路径")
    _add_common_args(p_exp)

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    try:
        if args.command == "create":
            cmd_create(args)
        elif args.command == "list":
            cmd_list(args)
        elif args.command == "delete":
            cmd_delete(args)
        elif args.command == "export-cookies":
            cmd_export_cookies(args)
    except RuntimeError as e:
        print(f"\n[!] 错误: {e}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\n[!] 已中断")
        sys.exit(1)


def _add_common_args(parser):
    parser.add_argument("--cookies", type=str, help="手动指定 cookies.json 路径")
    parser.add_argument("--host", type=str, default="icloud.com",
                       choices=["icloud.com", "icloud.com.cn"], help="iCloud 区域")
    parser.add_argument("--quiet", "-q", action="store_true", help="减少输出")


if __name__ == "__main__":
    main()
