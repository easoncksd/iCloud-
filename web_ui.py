#!/usr/bin/env python3
"""iCloud HME Web UI — 多账号聚合管理平台 — Flask single-page app."""
import sys, os, json, time, queue, secrets, threading, re, hashlib
from collections import OrderedDict, deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed, wait
from datetime import datetime, timedelta
from html import escape as _html_escape
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path: sys.path.insert(0, str(HERE))

from flask import Flask, Response, request, jsonify, render_template_string, redirect
from icloud_hme import ICloudHME, extract_chrome_cookies
from account_manager import AccountManager
from export_history import ExportHistoryStore
from mail_body_store import MailBodyStore
from pickup_links import PickupLinkStore

# ---- config ----
RESULTS_DIR = HERE / "results"
LOGS_DIR = HERE / "logs"
RESULTS_DIR.mkdir(parents=True, exist_ok=True)
LOGS_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)
ADMIN_ACCESS_TOKEN = os.environ.get("ADMIN_ACCESS_TOKEN", "").strip()
_ADMIN_COOKIE_NAME = "__Host-icloud_admin"
_ADMIN_COOKIE_VALUE = (
    hashlib.sha256(("icloud-admin-cookie:" + ADMIN_ACCESS_TOKEN).encode()).hexdigest()
    if ADMIN_ACCESS_TOKEN else ""
)



def _is_wildcard_host(host: str) -> bool:
    return (host or "").strip().lower() in {"0.0.0.0", "::", "[::]", "*"}


def _public_bind_blocked(host: str, token: str = "") -> bool:
    return _is_wildcard_host(host) and not (token or "").strip()


@app.before_request
def _require_admin_access():
    if request.path.startswith("/pickup/") or request.path == "/healthz":
        return None
    if not ADMIN_ACCESS_TOKEN:
        return None

    cookie = request.cookies.get(_ADMIN_COOKIE_NAME, "")
    if cookie and secrets.compare_digest(cookie, _ADMIN_COOKIE_VALUE):
        return None

    supplied = request.args.get("access", "")
    if supplied and secrets.compare_digest(supplied, ADMIN_ACCESS_TOKEN):
        response = redirect(request.path or "/", code=302)
        response.set_cookie(
            _ADMIN_COOKIE_NAME,
            _ADMIN_COOKIE_VALUE,
            max_age=30 * 24 * 60 * 60,
            secure=True,
            httponly=True,
            samesite="Strict",
            path="/",
        )
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    return Response("Not Found", status=404, mimetype="text/plain")


@app.route("/healthz")
def healthz():
    return jsonify({"ok": True}), 200, {"Cache-Control": "no-store"}

_BJ_TZ = ZoneInfo("Asia/Shanghai")
_RUNTIME_LOG_FILE = LOGS_DIR / "runtime.jsonl"


def _load_runtime_logs(path=_RUNTIME_LOG_FILE):
    try:
        lines = path.read_text(encoding="utf-8").splitlines()[-1000:]
    except OSError:
        return []
    entries = []
    for line in lines:
        try:
            entry = json.loads(line)
            if isinstance(entry, dict) and isinstance(entry.get("seq"), int):
                entries.append(entry)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
    return entries


_loaded_log_entries = _load_runtime_logs()
_log_condition = threading.Condition()
_log_entries = deque(_loaded_log_entries, maxlen=1000)
_log_seq = max((entry["seq"] for entry in _loaded_log_entries), default=0)
_today_key = datetime.now(_BJ_TZ).strftime("%Y%m%d")
_global_state = {"running":False,"creating":False,"round_status":"","total_created":0,"today_created":0,"current_round_created":0,"next_trigger":None,"last_error":None,"cookies_ok":False,"alias_count":0,"alias_active":0}
_lock = threading.Lock()
_scheduler_thread = None
_scheduler_lock = threading.Lock()
_scheduler_stop_event = threading.Event()
_shutdown_event = threading.Event()
_account_mgr = AccountManager()
_pickup_store = PickupLinkStore(RESULTS_DIR / "pickup_links.json")
_export_store = ExportHistoryStore(RESULTS_DIR / "export_history.json")
_pickup_store.rebind_stale_accounts(_account_mgr.accounts.keys())
PICKUP_BASE_URL = os.environ.get("PICKUP_BASE_URL", "").rstrip("/")
_pickup_refresh_lock = threading.Lock()
_pickup_refreshing_accounts = set()
_pickup_last_account_refresh = {}
_pickup_refresh_errors = {}
_pickup_error_log_state = {}
_removed_account_ids = set()
_pickup_executor = ThreadPoolExecutor(max_workers=16, thread_name_prefix="pickup")
_pickup_pending = 0
_PICKUP_MAX_PENDING = 256
_PICKUP_SYNC_INTERVAL_SECONDS = max(
    1.0, float(os.environ.get("PICKUP_SYNC_INTERVAL_SECONDS", "15"))
)
_PICKUP_BODY_MAX_ITEMS = 1000
_PICKUP_BODY_MAX_BYTES = 64 * 1024 * 1024
_pickup_body_cache = OrderedDict()
_pickup_body_cache_bytes = 0
_pickup_body_refreshing = set()
_pickup_body_store = MailBodyStore(
    RESULTS_DIR / "mail_bodies.sqlite3",
    max_items=5000,
    max_bytes=_PICKUP_BODY_MAX_BYTES,
)


def _migrate_stale_account_data():
    """Rebind local records after the same Apple account is imported again."""
    valid_ids = set(_account_mgr.accounts)
    alias_accounts = {
        str(item.get("alias_email") or "").strip().lower(): item.get("account_id")
        for item in _pickup_store.list_all()
        if item.get("account_id") in valid_ids and item.get("alias_email")
    }
    latest_file = RESULTS_DIR / "latest_emails.txt"
    old_targets = {}
    rewritten = 0
    if latest_file.exists():
        output = []
        for raw_line in latest_file.read_text(encoding="utf-8").splitlines():
            parts = raw_line.split("\t")
            if not parts or not parts[0].strip():
                output.append(raw_line)
                continue
            email = parts[0].strip().lower()
            old_id = parts[1].strip() if len(parts) > 1 else ""
            new_id = alias_accounts.get(email)
            if old_id and old_id not in valid_ids and new_id in valid_ids:
                while len(parts) < 2:
                    parts.append("")
                parts[1] = new_id
                old_targets.setdefault(old_id, set()).add(new_id)
                rewritten += 1
            output.append("\t".join(parts))
        if rewritten:
            tmp = latest_file.with_suffix(latest_file.suffix + ".tmp")
            payload = "\n".join(output) + "\n"
            with open(tmp, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, latest_file)

    account_mapping = {
        old_id: next(iter(targets))
        for old_id, targets in old_targets.items()
        if len(targets) == 1
    }
    return {
        "latest_emails": rewritten,
        "mail_cache_accounts": _account_mgr._cache.rebind_accounts(account_mapping),
        "mail_bodies": _pickup_body_store.rebind_accounts(account_mapping),
        "export_history": _export_store.rebind_accounts(
            account_mapping, alias_accounts
        ),
    }


_DATA_MIGRATION_STATS = _migrate_stale_account_data()
_batch_lock = threading.RLock()
_BATCH_STATE_FILE = RESULTS_DIR / "batch_jobs.json"
_BATCH_JOB_HISTORY = 20
_BATCH_RETRY_DELAY_SECONDS = max(
    1.0, float(os.environ.get("BATCH_RETRY_DELAY_SECONDS", "60"))
)
_BATCH_MAX_ACCOUNT_WORKERS = max(
    1, min(20, int(os.environ.get("BATCH_MAX_ACCOUNT_WORKERS", "10")))
)
_BATCH_CREATE_HEARTBEAT_SECONDS = max(
    5.0, float(os.environ.get("BATCH_CREATE_HEARTBEAT_SECONDS", "15"))
)

_TEMPORARY_CREATE_LIMIT_MARKERS = (
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
)


def _load_batch_state(path=_BATCH_STATE_FILE):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        raw_jobs = data.get("jobs", {})
        jobs = OrderedDict(
            (str(job_id), job)
            for job_id, job in raw_jobs.items()
            if isinstance(job, dict)
        )
        while len(jobs) > _BATCH_JOB_HISTORY:
            jobs.popitem(last=False)
        active_id = str(data.get("active_id") or "") or None
        if active_id not in jobs or jobs.get(active_id, {}).get("status") not in (
            "queued", "running"
        ):
            active_id = next(
                (
                    job_id
                    for job_id, job in reversed(jobs.items())
                    if job.get("status") in ("queued", "running")
                ),
                None,
            )
        return jobs, active_id
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return OrderedDict(), None


_batch_jobs, _batch_active_id = _load_batch_state()
_manual_create_lock = threading.RLock()
_manual_creating_accounts = set()
_SCHEDULER_FLAG_FILE = RESULTS_DIR / "scheduler_enabled.json"


def _load_scheduler_enabled(path=None):
    target = Path(path or _SCHEDULER_FLAG_FILE)
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
        return bool(isinstance(data, dict) and data.get("enabled"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return False


def _save_scheduler_enabled(enabled: bool, path=None):
    target = Path(path or _SCHEDULER_FLAG_FILE)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"enabled": bool(enabled)}, ensure_ascii=False)
    tmp = target.with_suffix(target.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, target)



def _save_batch_state_locked(path=None):
    target = Path(path or _BATCH_STATE_FILE)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        {"active_id": _batch_active_id, "jobs": _batch_jobs},
        ensure_ascii=False,
        indent=2,
    )
    tmp = target.with_suffix(target.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, target)


class _BatchInterrupted(Exception):
    pass


def _format_retry_delay(seconds):
    seconds = max(1, int(round(seconds)))
    if seconds < 60:
        return f"{seconds} 秒"
    minutes = max(1, int(round(seconds / 60)))
    return f"{minutes} 分钟"


def _is_temporary_create_limit(error):
    lower = str(error or "").lower()
    return any(marker in lower for marker in _TEMPORARY_CREATE_LIMIT_MARKERS)

_RATE_LIMIT_KW = ["limit","exceeded","maximum","quota","429","too many","try again","unavailable","上限","超过","过多","频繁","rate limit","throttle","blocked"]

def _is_limit_error(err: str) -> bool: return any(kw in err.lower() for kw in _RATE_LIMIT_KW)

_time_offset = 0.0
def _sync_time():
    global _time_offset
    for url in ["https://www.baidu.com","https://www.cloudflare.com","https://www.microsoft.com"]:
        try:
            import requests as _r
            resp = _r.head(url, timeout=5)
            date_str = resp.headers.get("Date","")
            if date_str:
                from email.utils import parsedate_to_datetime
                net_time = parsedate_to_datetime(date_str)
                _time_offset = (net_time - datetime.now(net_time.tzinfo)).total_seconds()
                return _time_offset
        except: continue
    return 0.0

def _now() -> datetime: return datetime.now(_BJ_TZ) + timedelta(seconds=_time_offset)

def _emit_log(level, msg):
    global _log_seq
    with _log_condition:
        _log_seq += 1
        entry = {
            "seq": _log_seq,
            "time": _now().strftime("%Y-%m-%d %H:%M:%S"),
            "level": level,
            "msg": str(msg)[:1000],
        }
        _log_entries.append(entry)
        try:
            with open(_RUNTIME_LOG_FILE, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
            if _RUNTIME_LOG_FILE.stat().st_size > 2 * 1024 * 1024:
                _RUNTIME_LOG_FILE.write_text(
                    "".join(
                        json.dumps(item, ensure_ascii=False) + "\n"
                        for item in _log_entries
                    ),
                    encoding="utf-8",
                )
        except OSError:
            pass
        _log_condition.notify_all()

def _update_state(**kw):
    global _today_key
    with _lock:
        today = _now().strftime("%Y%m%d")
        if today != _today_key: _global_state["today_created"] = 0; _today_key = today
        _global_state.update(kw)

def _increment_state(**kw):
    global _today_key
    with _lock:
        today = _now().strftime("%Y%m%d")
        if today != _today_key: _global_state["today_created"] = 0; _today_key = today
        for k, delta in kw.items(): _global_state[k] = _global_state.get(k,0) + delta

def _scheduler_loop():
    """Beijing 7:00-20:00, every 60-90 minutes, create 3-5 aliases per active account."""
    import random as _random
    _update_state(running=True, round_status="等待触发窗口")
    _emit_log("info", "调度器已启动 (BJ 7-20h, 间隔 60-90min, 每轮 3-5 个)")
    def _bj_hour() -> int:
        return _now().hour
    while not _scheduler_stop_event.is_set() and not _shutdown_event.is_set():
        h = _bj_hour()
        if h < 7 or h >= 20:
            _update_state(round_status=f"非窗口时段 (BJ {h}:00)，等待...")
            _scheduler_stop_event.wait(1800)
            continue
        active_accounts = [a for a in _account_mgr.list_accounts() if a.get("status") == "active"]
        if not active_accounts:
            _update_state(creating=False, round_status="无活跃账号，跳过")
            _scheduler_stop_event.wait(1800)
            continue
        round_total = 0
        for i, account in enumerate(active_accounts):
            if _scheduler_stop_event.is_set() or _shutdown_event.is_set():
                break
            acc_id = account["id"]
            acc_name = account.get("name", acc_id)
            with _manual_create_lock:
                if _account_create_in_progress(acc_id):
                    claimed = False
                else:
                    _manual_creating_accounts.add(acc_id)
                    claimed = True
            if not claimed:
                _emit_log("info", f"[{acc_name}] 已有创建任务，本轮跳过")
                continue
            target_count = _random.randint(3, 5)
            _emit_log("info", f"[{acc_name}] 本轮目标 {target_count} 个")
            _update_state(creating=True, round_status=f"{acc_name} 自动创建中")
            try:
                results = _account_mgr.create_aliases_for_account(
                    acc_id, target_count, "", progress_callback=_ensure_pickup_for_created
                )
            except Exception as e:
                _emit_log("warn", f"[{acc_name}] 失败: {str(e)[:80]}")
                results = []
            finally:
                with _manual_create_lock:
                    _manual_creating_accounts.discard(acc_id)
            created = [r for r in results if r.get("ok")]
            errors = [r for r in results if not r.get("ok")]
            if created:
                round_total += len(created)
                _increment_state(today_created=len(created), total_created=len(created))
                _emit_log("success", f"[{acc_name}] 创建完成: {len(created)} 个")
            if errors:
                _emit_log("warn", f"[{acc_name}] 失败: {str(errors[0].get('error', ''))[:80]}")
            if i < len(active_accounts) - 1:
                _scheduler_stop_event.wait(_random.uniform(120, 300))
        _update_state(creating=False, current_round_created=round_total, round_status=f"本轮创建 {round_total} 个")
        interval_sec = _random.randint(3600, 5400)
        target = _now() + timedelta(seconds=interval_sec)
        _update_state(next_trigger=target.timestamp())
        _emit_log("info", f"下轮 {target.strftime('%H:%M')} (间隔 {interval_sec // 60}min)")
        _scheduler_stop_event.wait(interval_sec)
    _update_state(running=False, next_trigger=None, round_status="已停止")
    _emit_log("info", "调度器已停止")


def _health_loop():
    _error_reported = set()
    while not _shutdown_event.is_set():
        if _shutdown_event.wait(300):
            break
        for account in _account_mgr.list_accounts():
            acc_id = account["id"]
            if _account_create_in_progress(acc_id):
                continue
            try:
                result = _account_mgr.validate_account(acc_id)
                if result.get("status") == "active":
                    _error_reported.discard(acc_id)
                    continue
                error_text = result.get("last_error") or "error"
                if acc_id not in _error_reported:
                    _emit_log("warn", f"健康检查失败 [{account.get('name','?')}]: {str(error_text)[:100]}")
                    _error_reported.add(acc_id)
            except Exception as e:
                if acc_id not in _error_reported:
                    _emit_log("warn", f"健康检查失败 [{account.get('name','?')}]: {str(e)[:100]}")
                    _error_reported.add(acc_id)

# ----- HTML -----
UI_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>iCloud mail</title>
<link rel="icon" type="image/svg+xml" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Crect width='24' height='24' rx='6' fill='%23f4f5f7'/%3E%3Cpath fill='%231f8b4c' d='M19.2 10.15C18.55 7.05 15.85 4.75 12.55 4.75c-2.52 0-4.72 1.38-5.9 3.42C4.35 8.42 2.5 10.55 2.5 13.15c0 2.82 2.28 5.1 5.1 5.1h10.95c2.42 0 4.4-1.98 4.4-4.4 0-2.28-1.74-4.16-3.95-4.35z'/%3E%3Cpath fill='%23ffffff' d='M6.85 12.2h10.3c.58 0 1.05.47 1.05 1.05v4.85c0 .58-.47 1.05-1.05 1.05H6.85c-.58 0-1.05-.47-1.05-1.05v-4.85c0-.58.47-1.05 1.05-1.05z'/%3E%3Cpath fill='%231f8b4c' d='M5.8 12.2 12 16.25l6.2-4.05v1.12L12 17.48 5.8 13.32V12.2z'/%3E%3C/svg%3E">
<style>:root{
  --bg:#f4f5f7;--panel:#fff;--ink:#1a1d21;--muted:#6b7280;--line:#e5e7eb;
  --green:#1f8b4c;--green-soft:#e8f6ee;--red:#c2413b;--red-soft:#fdecec;
  --shadow:0 1px 2px rgba(16,24,40,.06);
  --sans:ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;
  --mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
}
*{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%;background:var(--bg);color:var(--ink);font-family:var(--sans);font-size:14px;letter-spacing:0}
body{display:flex;min-height:100vh}
button,input,select,textarea{font:inherit}
a{color:var(--green)}
.mono{font-family:var(--mono)}
.app{display:flex;width:100%;min-height:100vh}
.sidebar{width:240px;background:#eef1f4;border-right:1px solid var(--line);padding:18px 14px;display:flex;flex-direction:column;gap:6px;flex-shrink:0}
.nav-ico{width:16px;height:16px;flex-shrink:0}
.side-overview{margin-top:16px;padding:12px;border:1px solid var(--line);border-radius:8px;background:#fff}
.side-kicker{font-size:12px;color:var(--muted);font-weight:600;margin-bottom:4px}
.side-stat{display:flex;justify-content:space-between;gap:8px;align-items:baseline;padding:7px 0;border-bottom:1px solid var(--line);font-size:13px;color:var(--muted)}
.side-stat:last-of-type{border-bottom:0}
.side-stat b{color:var(--ink);font-weight:700}
.side-task{display:flex;align-items:center;gap:8px;margin-top:10px;font-size:12px;color:var(--muted);line-height:1.4}
@keyframes pulse-dot{0%,100%{opacity:1}50%{opacity:.35}}
.status-dot.busy{animation:pulse-dot 1.2s ease-in-out infinite}

.logo{display:flex;align-items:center;gap:10px;margin:0 8px 22px;color:var(--ink);font-weight:700;font-size:15px}
.logo .icon{width:28px;height:28px;color:var(--green);flex-shrink:0;display:block}
.nav-item{display:flex;align-items:center;gap:10px;padding:10px 12px;border-radius:8px;color:var(--muted);cursor:pointer;user-select:none;border:0;background:transparent;width:100%;text-align:left;transition:background .16s,color .16s}
.nav-item:hover{background:#e4e7eb;color:var(--ink)}
.nav-item.active{background:#fff;color:var(--ink);font-weight:600;box-shadow:var(--shadow);outline:1px solid var(--line)}
.sidebar-foot{margin-top:auto;padding:14px 12px 12px;border-top:1px solid var(--line);color:var(--muted);font-size:12px;display:flex;flex-direction:column;gap:12px}
.social-links{display:flex;align-items:center;gap:10px}
.social-link{width:36px;height:36px;display:inline-flex;align-items:center;justify-content:center;flex-shrink:0;border-radius:8px;overflow:hidden;line-height:0;text-decoration:none;color:inherit;transition:transform .16s ease,box-shadow .16s ease}
.social-link svg{width:36px;height:36px;display:block}
.social-link:hover{transform:translateY(-1px);box-shadow:0 4px 10px rgba(16,24,40,.12)}
.social-link:focus-visible{outline:2px solid var(--green);outline-offset:2px}
.main{flex:1;min-width:0;display:flex;flex-direction:column}
.topbar{display:flex;align-items:center;justify-content:space-between;gap:16px;padding:18px 28px 12px;border-bottom:1px solid var(--line);background:var(--bg);position:sticky;top:0;z-index:5}
.topbar h1{font-size:22px;font-weight:700;letter-spacing:0}
.topbar-actions{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.lang-switch{display:flex;background:#fff;border:1px solid var(--line);border-radius:8px;overflow:hidden;flex-shrink:0}
.lang-switch button{border:0;background:transparent;padding:6px 10px;color:var(--muted);cursor:pointer;min-width:38px;line-height:1;transition:background .16s ease,color .16s ease}
.lang-switch button:hover{color:var(--ink)}
.lang-switch button.active{background:var(--green-soft);color:var(--green);font-weight:600}
.task-pill{display:flex;align-items:center;gap:8px;background:#fff;border:1px solid var(--line);border-radius:999px;padding:6px 12px;font-size:12px;color:var(--muted)}
.task-pill b{color:var(--ink);font-weight:600}
.status-dot{width:8px;height:8px;border-radius:50%;background:#d1d5db;display:inline-block}
.status-dot.online{background:var(--green)}
.status-dot.offline{background:#d1d5db}
.page{padding:20px 28px 40px;flex:1;min-width:0}
.hero-empty{background:#fff;border:1px solid var(--line);border-radius:8px;padding:48px 28px;text-align:center;box-shadow:var(--shadow)}
.hero-empty h2{font-size:20px;margin-bottom:8px}
.hero-empty p{color:var(--muted);margin-bottom:18px}
.toolbar,.filter-bar{display:flex;flex-wrap:wrap;gap:12px;align-items:center;margin:0 0 12px}
.pager{display:flex;flex-wrap:wrap;gap:8px;align-items:center}
.filter-bar .pager{margin-left:auto}
.pager-bottom{padding:10px 16px;border-top:1px solid var(--line);justify-content:flex-end}
.segmented{display:flex;background:#fff;border:1px solid var(--line);border-radius:8px;overflow:hidden}
.segmented button{border:0;background:transparent;padding:8px 12px;color:var(--muted);cursor:pointer;transition:background .16s ease,color .16s ease}
.segmented button.active{background:var(--green-soft);color:var(--green);font-weight:600}
.panel{background:#fff;border:1px solid var(--line);border-radius:8px;box-shadow:var(--shadow);min-width:0}
.panel-header{display:flex;justify-content:space-between;align-items:center;padding:14px 16px;border-bottom:1px solid var(--line);font-weight:600;flex-wrap:wrap;gap:10px;position:relative;z-index:6}
.panel-header>div{display:flex;flex-wrap:wrap;gap:8px;align-items:center}
.panel-body{padding:0}#aliasTableContainer{overflow-x:auto}
.inbox-tools{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.alias-search{position:relative;flex:1 1 260px;min-width:220px;max-width:380px}
.alias-search input{width:100%;padding-right:28px}
.alias-search-clear{position:absolute;right:4px;top:50%;transform:translateY(-50%);border:0;background:transparent;color:var(--muted);cursor:pointer;width:22px;height:22px;line-height:20px;padding:0;font-size:16px}
.alias-search-clear:hover{color:var(--ink)}
.alias-suggest{position:absolute;left:0;right:0;top:calc(100% + 4px);background:#fff;border:1px solid var(--line);border-radius:8px;box-shadow:0 10px 24px rgba(16,24,40,.12);max-height:260px;overflow:auto;z-index:12}
.alias-suggest button{display:block;width:100%;text-align:left;border:0;background:transparent;padding:8px 10px;cursor:pointer;font-family:var(--mono);font-size:12px}
.alias-suggest button.active,.alias-suggest button:hover{background:var(--green-soft)}
.alias-suggest .hint{padding:8px 10px}
.email-table{width:100%;min-width:1000px;border-collapse:collapse}
.pickup-cell{white-space:nowrap}
.email-table th,.email-table td{padding:10px 12px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
.email-table th{font-size:12px;color:var(--muted);font-weight:600;background:#fafafa;position:sticky;top:0}
.email-table td{transition:background .16s ease}
.email-table tr:hover td{background:#f8faf9}
.btn{border:1px solid var(--line);background:#fff;color:var(--ink);border-radius:8px;padding:8px 12px;cursor:pointer;display:inline-flex;align-items:center;justify-content:center;gap:6px;transition:background .16s ease,color .16s ease,border-color .16s ease,transform .16s ease,box-shadow .16s ease}
.btn:hover{background:#f8faf9}
.btn:active{transform:scale(.98)}
.btn-primary{background:var(--green);border-color:var(--green);color:#fff}
.btn-danger{background:var(--red);border-color:var(--red);color:#fff}
.btn-outline{background:#fff}
.btn-sm{padding:6px 10px;font-size:13px}
.btn-xs{padding:4px 8px;font-size:12px}
.btn:disabled{opacity:.55;cursor:not-allowed;transform:none}
.copy-btn{border:0;background:transparent;color:var(--green);cursor:pointer;font-size:12px}
input[type=text],input[type=number],input[type=password],select,textarea{border:1px solid var(--line);border-radius:8px;padding:8px 10px;background:#fff;min-width:0}
textarea{width:100%;min-height:140px}
.work-strip{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin-bottom:16px}
.work-stat{background:#fff;border:1px solid var(--line);border-radius:8px;padding:12px 14px;box-shadow:var(--shadow);min-width:0;transition:border-color .16s ease,box-shadow .16s ease,transform .16s ease}
.work-stat span{display:block;color:var(--muted);font-size:12px;margin-bottom:4px}
.work-stat b{display:block;font-size:18px;font-weight:700;letter-spacing:0;word-break:break-word;line-height:1.3}
.work-stat.is-live b{color:var(--green)}
.account-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(340px,1fr));gap:12px}
.acc-card{background:#fff;border:1px solid var(--line);border-radius:8px;padding:16px;box-shadow:var(--shadow);transition:border-color .16s ease,box-shadow .16s ease,transform .16s ease}
.acc-card.is-busy{border-color:#b7dcc5}
.acc-top{display:flex;justify-content:space-between;gap:12px;align-items:flex-start}
.acc-title{font-weight:700;margin-bottom:4px}
.acc-email{color:var(--muted);font-size:12px;font-family:var(--mono)}
.acc-usage,.acc-job{margin:12px 0 8px}
.acc-usage .progress-bar,.acc-job .progress-bar{height:8px;margin-top:6px}
.acc-stats{display:flex;gap:16px;margin:8px 0 12px;color:var(--muted);font-size:13px}
.acc-actions{display:flex;gap:8px;flex-wrap:wrap}
.status-badge{font-size:12px;padding:3px 8px;border-radius:999px}
.status-badge.ok{background:var(--green-soft);color:var(--green)}
.status-badge.err{background:var(--red-soft);color:var(--red)}
.empty{padding:36px 16px;text-align:center;color:var(--muted)}
.empty .icon{width:28px;height:28px;border:2px solid var(--line);border-radius:8px;margin:0 auto 10px}
.empty-title{color:var(--ink);font-weight:700;margin:0 0 8px}
.empty p{margin:0 auto 14px;max-width:380px;line-height:1.6}
.empty-steps{list-style:decimal;padding-left:22px;margin:0 auto 16px;max-width:320px;text-align:left;line-height:1.8}
.empty .btn{margin-top:4px}
.chk-group{display:flex;flex-direction:column;gap:8px}
.chk-item{display:flex;gap:8px;align-items:center}
.progress-card{padding-top:14px}
.progress-head{display:flex;justify-content:space-between;gap:12px;align-items:baseline;font-size:13px}
.progress-head span{color:var(--muted);white-space:nowrap}
.progress-meta{display:flex;justify-content:space-between;gap:12px;margin-top:8px;color:var(--muted);font-size:12px}
.progress-bar{height:12px;background:#eef1f4;border-radius:99px;overflow:hidden;margin-top:8px;display:flex}
.progress-bar .fill{height:100%;background:var(--green);width:0;flex:0 0 auto;transition:width .32s ease}
.progress-bar .fill.err{background:var(--red)}
.progress-bar.is-run .fill.ok{background-color:var(--green);background-image:linear-gradient(90deg,rgba(255,255,255,0),rgba(255,255,255,.34),rgba(255,255,255,0));background-size:42px 100%;animation:progress-sheen 1.1s linear infinite}
@keyframes progress-sheen{to{background-position:42px 0}}
.progress-item{padding:12px 0 4px;border-bottom:1px solid var(--line)}
.progress-item:last-child{border-bottom:0}
.progress-item .progress-bar{height:8px;margin-top:6px}
.progress-note{margin-top:6px;font-size:12px;line-height:1.5}
.log-feed{font-family:var(--mono);font-size:12px;max-height:420px;overflow:auto;padding:12px}
.log-line{padding:4px 0;border-bottom:1px solid var(--line)}
.log-time{color:var(--muted);margin-right:8px}
.modal-overlay{position:fixed;inset:0;background:rgba(16,24,40,.28);display:flex;align-items:center;justify-content:center;padding:24px;z-index:20;animation:overlay-in .16s ease}
.modal-box{background:#fff;width:min(520px,100%);border-radius:8px;border:1px solid var(--line);box-shadow:0 16px 40px rgba(16,24,40,.18);padding:22px;overflow:auto;animation:modal-in .16s ease}
.modal-box h3{font-size:18px;margin-bottom:8px}
.modal-box p{color:var(--muted);margin-bottom:14px;line-height:1.6}
.modal-actions{display:flex;justify-content:flex-end;gap:8px;margin-top:16px}
.modal-msg{margin-top:10px;font-size:13px}
.copy-toast{position:fixed;left:50%;bottom:24px;transform:translateX(-50%) translateY(12px);background:var(--ink);color:#fff;padding:10px 14px;border-radius:8px;opacity:0;pointer-events:none;transition:opacity .16s ease,transform .16s ease}
.copy-toast.show{opacity:1;transform:translateX(-50%) translateY(0)}
.hint{font-size:12px;color:var(--muted)}
.settings-section{padding:16px;border-bottom:1px solid var(--line)}
.settings-section h3{font-size:15px;margin-bottom:6px}
.settings-section p{color:var(--muted);margin-bottom:10px}

.is-enter{animation:view-in .16s ease both}
@keyframes view-in{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:none}}
@keyframes overlay-in{from{opacity:0}to{opacity:1}}
@keyframes modal-in{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:none}}
@media (hover:hover) and (pointer:fine){
  .acc-card:hover{border-color:#d5d9de;box-shadow:0 6px 16px rgba(16,24,40,.08);transform:translateY(-1px)}
  .work-stat:hover{border-color:#d5d9de;box-shadow:0 4px 12px rgba(16,24,40,.07);transform:translateY(-1px)}
  .btn-primary:hover{filter:brightness(1.04)}
  .nav-item{transition:background .16s ease,color .16s ease,transform .16s ease}
  .social-link:hover{transform:translateY(-1px)}
}
@media (prefers-reduced-motion:reduce){
  .is-enter,.modal-overlay,.modal-box{animation:none}
  .btn,.acc-card,.work-stat,.nav-item,.copy-toast,.progress-bar .fill,.email-table td,.segmented button,.lang-switch button,.social-link{transition:none}
  .btn:active,.acc-card:hover,.work-stat:hover,.social-link:hover{transform:none}
  .progress-bar.is-run .fill.ok,.status-dot.busy{animation:none}
}
@media(max-width:768px){
  html,body{width:100%;max-width:100%;overflow-x:hidden}
  body,.app{flex-direction:column}
  .sidebar{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));width:100%;max-width:100%;overflow-x:hidden;padding:10px}
  .sidebar .logo,.sidebar-foot{grid-column:1/-1}
  .side-overview{display:none}
  .sidebar .logo{margin:4px}
  .nav-item{min-width:0;justify-content:center;padding:8px 4px;font-size:12px;white-space:nowrap}
  .main,.panel,.panel-header,.panel-body,.inbox-tools,.page{width:100%;min-width:0;max-width:100%}
  .topbar,.page{padding:12px}
  .inbox-tools{width:100%;align-items:stretch}
  .inbox-tools select,.inbox-tools input[type=text],.inbox-tools .alias-search{flex:1 1 220px;width:auto!important;min-width:0;max-width:none}
  .inbox-tools input[type=number]{flex:0 0 64px}
  .inbox-tools .btn{flex:1 1 calc(33.333% - 8px);padding-left:8px;padding-right:8px}
  .inbox-tools #btnInboxSettings{flex-basis:calc(33.333% - 8px)}
  .inbox-tools #cacheStatus{flex-basis:100%;word-break:break-word}
  .filter-bar .segmented{margin-left:0;max-width:100%;overflow-x:auto}
  .filter-bar .pager{margin-left:0;width:100%}
  .work-strip{grid-template-columns:repeat(2,minmax(0,1fr));gap:8px}
  .work-stat{padding:10px 12px}
  .work-stat b{font-size:16px}
  .account-grid{grid-template-columns:1fr}
}

.modal-box label.hint{display:block;margin:8px 0 6px}
.modal-box input[type=text],.modal-box input[type=password],.modal-box select,.modal-box textarea{width:100%}
</style>
</head>
<body>
<div class="app">
<aside class="sidebar">
  <div class="logo">
    <svg class="icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      <path fill="currentColor" d="M19.2 10.15C18.55 7.05 15.85 4.75 12.55 4.75c-2.52 0-4.72 1.38-5.9 3.42C4.35 8.42 2.5 10.55 2.5 13.15c0 2.82 2.28 5.1 5.1 5.1h10.95c2.42 0 4.4-1.98 4.4-4.4 0-2.28-1.74-4.16-3.95-4.35z"/>
      <path fill="#ffffff" d="M6.85 12.2h10.3c.58 0 1.05.47 1.05 1.05v4.85c0 .58-.47 1.05-1.05 1.05H6.85c-.58 0-1.05-.47-1.05-1.05v-4.85c0-.58.47-1.05 1.05-1.05z"/>
      <path fill="currentColor" d="M5.8 12.2 12 16.25l6.2-4.05v1.12L12 17.48 5.8 13.32V12.2z"/>
    </svg>
    iCloud mail
  </div>
  <a class="nav-item" data-tab="accounts"><svg class="nav-ico" viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" d="M12 12a4 4 0 1 0-4-4 4 4 0 0 0 4 4zm0 2c-3.4 0-8 1.7-8 5v1h16v-1c0-3.3-4.6-5-8-5z"/></svg><span data-i18n="nav.accounts">账号</span></a>
  <a class="nav-item active" data-tab="emails"><svg class="nav-ico" viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" d="M20 4H4a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2V6a2 2 0 0 0-2-2zm0 4-8 5L4 8V6l8 5 8-5z"/></svg><span data-i18n="nav.emails">邮箱</span></a>
  <a class="nav-item" data-tab="inbox"><svg class="nav-ico" viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" d="M19 3H5a2 2 0 0 0-2 2v3h6l1 2h4l1-2h6V5a2 2 0 0 0-2-2zm3 7h-6.4l-1 2H9.4l-1-2H2v9a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2z"/></svg><span data-i18n="nav.inbox">收件箱</span></a>
  <a class="nav-item" data-tab="settings"><svg class="nav-ico" viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" d="M19.4 13a7.7 7.7 0 0 0 .1-1 7.7 7.7 0 0 0-.1-1l2.1-1.6-2-3.5-2.5 1a7 7 0 0 0-1.7-1L14.8 2h-4l-.5 2.9A7 7 0 0 0 8.6 6L6.1 5l-2 3.5L6.2 10a7.7 7.7 0 0 0-.1 1 7.7 7.7 0 0 0 .1 1L4.1 13.6l2 3.5 2.5-1a7 7 0 0 0 1.7 1l.5 2.9h4l.5-2.9a7 7 0 0 0 1.7-1l2.5 1 2-3.5zM12 15.5A3.5 3.5 0 1 1 15.5 12 3.5 3.5 0 0 1 12 15.5z"/></svg><span data-i18n="nav.settings">设置</span></a>
  <a class="nav-item" data-tab="batch" style="display:none"><span data-i18n="nav.batch">任务</span></a>
  <a class="nav-item" data-tab="logs" style="display:none"><span data-i18n="nav.logs">日志</span></a>
  <div class="side-overview" id="sideOverview">
    <div class="side-kicker" data-i18n="side.overview">概况</div>
    <div class="side-stat"><span data-i18n="nav.accounts">账号</span><b id="sideStatAccounts">0</b></div>
    <div class="side-stat"><span data-i18n="nav.emails">邮箱</span><b id="sideStatEmails">0</b></div>
    <div class="side-stat"><span data-i18n="side.ready">可收信</span><b id="sideStatReady">0</b></div>
    <div class="side-task"><span class="status-dot" id="sideTaskDot"></span><span id="sideTaskText">任务空闲</span></div>
  </div>
  <div class="sidebar-foot">
    <div class="social-links" role="group" data-i18n-aria="social.group" aria-label="社交媒体">
      <a class="social-link" href="https://x.com/fangao798" target="_blank" rel="noopener noreferrer" data-i18n-title="social.x" title="X" aria-label="X"><svg viewBox="0 0 24 24" aria-hidden="true" focusable="false"><rect width="24" height="24" rx="6" fill="#000"/><path fill="#fff" d="M16.36 6.2h1.84l-3.99 4.56L19.1 17.8h-3.5l-2.74-3.58-3.13 3.58H7.9l4.07-4.65-4.29-5.6h3.59l2.47 3.27L16.36 6.2zm-.51 10.48h1.02L8.64 7.32H7.55l8.3 9.36z"/></svg></a>
      <a class="social-link" href="https://t.co/fd6OPHgvKm" target="_blank" rel="noopener noreferrer" data-i18n-title="social.telegram" title="Telegram" aria-label="Telegram"><svg viewBox="0 0 24 24" aria-hidden="true" focusable="false"><circle cx="12" cy="12" r="12" fill="#2AABEE"/><path fill="#fff" d="M17.06 7.22c.1 0 .32.02.46.14.16.12.2.3.17.33 0 .03 0 .21-.02.47-.18 1.9-.96 6.5-1.36 8.63-.17.9-.5 1.2-.82 1.23-.7.07-1.23-.46-1.9-.9-1.06-.7-1.65-1.13-2.68-1.8-1.18-.78-.42-1.21.26-1.91.18-.18 3.25-2.98 3.31-3.23 0-.03.01-.15-.06-.21-.07-.07-.17-.04-.25-.02-.1.02-1.79 1.14-5.06 3.34-.48.33-.91.49-1.3.48-.43 0-1.25-.24-1.87-.44-.75-.24-1.35-.37-1.3-.79.03-.21.33-.43.9-.66 3.5-1.53 5.83-2.53 7-3.02 3.33-1.38 4.02-1.62 4.48-1.63z"/></svg></a>
    </div>
    <div data-i18n="side.foot">先加账号，再创建邮箱，然后看信或导出。</div>
  </div>
</aside>
<main class="main">
  <div class="topbar">
    <h1 id="tabTitle">邮箱</h1>
    <div class="topbar-actions">
      <div class="lang-switch" role="group" data-i18n-aria="lang.group" aria-label="Language">
        <button type="button" data-lang="zh" class="active" onclick="setLang('zh')" title="中文">中</button>
        <button type="button" data-lang="en" onclick="setLang('en')" title="English">EN</button>
      </div>
      <div class="task-pill" id="taskPill"><span class="status-dot" id="schedDot"></span><span id="schedLabel">任务空闲</span></div>
      <button class="btn btn-outline btn-sm" onclick="refreshAll()" data-i18n="action.refresh">刷新</button>
      <button class="btn btn-primary btn-sm" id="topPrimaryBtn" onclick="handlePrimaryAction()">添加账号</button>
    </div>
  </div>
  <div class="page">
    <div id="emptyState" class="hero-empty" style="display:none">
      <h2 id="emptyTitle" data-i18n="empty.title">先添加账号</h2>
      <p id="emptyText" data-i18n="empty.text">导入 Cookie 后，就可以创建隐私邮箱。</p>
      <button class="btn btn-primary" id="emptyActionBtn" onclick="handlePrimaryAction()" data-i18n="action.add_account">添加账号</button>
    </div>
    <div id="view-accounts" style="display:none">
      <div class="work-strip" id="accStrip">
        <div class="work-stat"><span data-i18n="nav.accounts">账号</span><b id="accStripAccounts">0</b></div>
        <div class="work-stat"><span data-i18n="nav.emails">邮箱</span><b id="accStripEmails">0</b></div>
        <div class="work-stat"><span data-i18n="stat.today">今日新建</span><b id="accStripToday">0</b></div>
        <div class="work-stat"><span data-i18n="stat.task">当前任务</span><b id="accStripTask">空闲</b></div>
      </div>
      <div class="toolbar">
        <button class="btn btn-primary" onclick="showAddAccountModal()" data-i18n="action.add_account">添加账号</button>
      </div>
      <div class="account-grid" id="accCards"></div>
    </div>
    <div id="view-emails">
      <div class="panel">
        <div class="panel-header">
          <span data-i18n="emails.title">邮箱列表</span>
          <div>
            <span class="hint" id="emailCount">0</span>
            <button class="btn btn-outline btn-sm" onclick="refreshEmails().then(renderAliasTable)" data-i18n="action.refresh">刷新</button>
            <button class="btn btn-outline btn-sm" id="btnAliasSync" onclick="refreshAliases()" title="从云端同步标签和状态" data-i18n="emails.sync" data-i18n-title="emails.sync_title">云端同步</button>
            <button class="btn btn-outline btn-sm" onclick="copyAll()" data-i18n="emails.copy_all">复制全部</button>
            <button class="btn btn-outline btn-sm" onclick="exportCSV()">CSV</button>
            <button class="btn btn-primary btn-sm" onclick="exportSelectedPickupTxt()" data-i18n="emails.export_txt">导出已选 TXT</button>
            <button class="btn btn-primary btn-sm" onclick="showCreateDrawer()" data-i18n="action.create_alias">创建邮箱</button>
          </div>
        </div>
        <div class="filter-bar">
          <span class="hint" data-i18n="filter.account">筛选账号:</span>
          <select id="aliasFilter" onchange="aliasPage=1;renderAliasTable()"><option value="all" data-i18n="filter.all_accounts">全部账号</option></select>
          <div class="segmented" aria-label="导出状态筛选" data-i18n-aria="filter.export">
            <button type="button" class="active" data-export-filter="unexported" onclick="setExportFilter('unexported')" id="exportCountUnexported">未导出 0</button>
            <button type="button" data-export-filter="exported" onclick="setExportFilter('exported')" id="exportCountExported">已导出 0</button>
            <button type="button" data-export-filter="all" onclick="setExportFilter('all')" id="exportCountAll">全部 0</button>
          </div>
          <div class="pager" id="aliasPager">
            <span class="hint" data-i18n="pager.per_page">每页</span>
            <div class="segmented" aria-label="每页数量" data-i18n-aria="pager.per_page_aria">
              <button type="button" data-page-size="20" onclick="setAliasPageSize(20)">20</button>
              <button type="button" data-page-size="50" class="active" onclick="setAliasPageSize(50)">50</button>
            </div>
            <span class="hint" id="aliasPageInfo"></span>
            <button class="btn btn-outline btn-sm" id="btnAliasPrev" onclick="setAliasPage(aliasPage-1)" data-i18n="pager.prev">上一页</button>
            <button class="btn btn-outline btn-sm" id="btnAliasNext" onclick="setAliasPage(aliasPage+1)" data-i18n="pager.next">下一页</button>
          </div>
        </div>
        <div class="panel-body">
          <div id="aliasTableContainer" class="empty"><div class="icon"></div><span data-i18n="emails.empty">还没有邮箱。请先添加账号，再点击「创建邮箱」。</span></div>
        </div>
      </div>
    </div>
    <div id="view-inbox" style="display:none">
      <div class="panel">
        <div class="panel-header">
          <span data-i18n="nav.inbox">收件箱</span>
          <div class="inbox-tools">
            <select id="inboxAccount" onchange="onInboxAccountChange()"></select>
            <input type="number" id="inboxLimit" value="20" min="1" max="100" title="邮件数量" data-i18n-title="inbox.limit_title">
            <div class="alias-search" id="aliasSearchWrap">
              <input type="text" id="aliasSearchInput" placeholder="搜索隐私邮箱查件..." title="搜索已有隐私邮箱，查询这个地址的邮件" data-i18n-placeholder="inbox.search_ph" data-i18n-title="inbox.search_title" autocomplete="off" spellcheck="false">
              <button type="button" class="alias-search-clear" id="btnAliasSearchClear" hidden onclick="clearAliasSearch()" title="清除查询，查看全部收件" data-i18n-title="inbox.clear_title">&times;</button>
              <div class="alias-suggest" id="aliasSuggest" hidden role="listbox"></div>
            </div>
            <button class="btn btn-outline btn-sm" id="btnInboxRefresh" onclick="refreshInbox()" data-i18n="action.refresh">刷新</button>
            <button class="btn btn-outline btn-sm" id="btnInboxForce" onclick="refreshInbox(true)" title="跳过缓存重新拉取" data-i18n="inbox.force" data-i18n-title="inbox.force_title">强制刷新</button>
            <button class="btn btn-outline btn-sm" id="btnInboxSearch" onclick="searchAliasMail()" title="查询指定邮箱的收件" data-i18n="inbox.search" data-i18n-title="inbox.search_btn_title">查件</button>
            <button class="btn btn-outline btn-sm" id="btnInboxAll" onclick="checkAliasMail()" title="检查所有隐私邮箱的收件" data-i18n="inbox.all" data-i18n-title="inbox.all_title">全部</button>
            <button class="btn btn-outline btn-sm" id="btnInboxSettings" onclick="openInboxSettings()" title="设置收信密码" data-i18n="inbox.set_pwd" data-i18n-title="inbox.set_pwd">设置收信密码</button>
            <span class="hint" id="cacheStatus"></span>
          </div>
        </div>
        <div class="panel-body">
          <div id="inboxMsgs" class="empty"><div class="icon"></div><div class="empty-title" data-i18n="inbox.setup_title">收件前先完成设置</div><ol class="empty-steps"><li data-i18n="inbox.step1">在「账号」里添加 Apple 账号</li><li data-i18n="inbox.step2">为账号设置收信密码</li><li data-i18n="inbox.step3">选择账号后即可看信</li></ol><button class="btn btn-primary" onclick="openInboxSettings()" data-i18n="inbox.start_setup">开始设置</button></div>
        </div>
      </div>
    </div>
    <div id="view-settings" style="display:none">
      <div class="panel">
        <div class="settings-section">
          <h3 data-i18n="settings.auto">自动创建</h3>
          <p data-i18n="settings.auto_desc">北京时间 7:00 到 20:00，每隔 60 到 90 分钟给每个有效账号创建 3 到 5 个邮箱。正在批量创建的账号会自动跳过。开启后会记住，服务重启仍会继续。</p>
          <div class="toolbar">
            <span class="status-dot" id="schedDotSettings"></span>
            <span id="schedLabelSettings">已停止</span>
            <button class="btn btn-sm" id="btnSched" onclick="toggleScheduler()">启动自动创建</button>
          </div>
        </div>
        <div class="settings-section">
          <h3 data-i18n="settings.create">创建邮箱</h3>
          <p data-i18n="settings.create_desc">不同主账号最多 10 个并行；同一账号仍逐个创建，触发 Apple 临时限制时每次等待 1 分钟后自动续建。</p>
          <div id="batchAccCount" class="hint">0 个可用账号</div>
          <div class="chk-group" id="batchChkGroup"></div>
          <div class="toolbar">
            <label data-i18n="settings.per_account">每账号数量</label>
            <input type="number" id="batchCount" value="5" min="1" max="750">
            <label data-i18n="settings.label">标签</label>
            <input type="text" id="batchLabel" placeholder="可选" data-i18n-placeholder="settings.label_ph">
            <button class="btn btn-primary" id="btnBatchExec" onclick="execBatchCreate()" data-i18n="settings.start">开始创建</button>
          </div>
          <div id="batchProgress"></div>
        </div>
        <div class="settings-section">
          <h3 data-i18n="settings.logs">任务与日志</h3>
          <p data-i18n="settings.logs_desc">创建、同步和限制解除都会出现在这里。</p>
          <button class="btn btn-outline btn-sm" onclick="clearLogs()" data-i18n="settings.clear_logs">清屏</button>
          <div class="log-feed" id="logFeed"></div>
        </div>
      </div>
    </div>
    <div id="view-docs" style="display:none"></div>
    <div id="docsContent" style="display:none"></div>
  </div>
</main>
</div>
<div class="copy-toast" id="toast"></div>
<script>var LANG_KEY='icloud-mail-lang';
var I18N={zh:{'nav.accounts':'账号','nav.emails':'邮箱','nav.inbox':'收件箱','nav.settings':'设置','nav.batch':'任务','nav.logs':'日志','side.overview':'概况','side.ready':'可收信','side.foot':'先加账号，再创建邮箱，然后看信或导出。','social.group':'社交媒体','social.x':'X','social.telegram':'Telegram','action.refresh':'刷新','action.add_account':'添加账号','action.create_alias':'创建邮箱','empty.title':'先添加账号','empty.text':'导入 Cookie 后，就可以创建隐私邮箱。','stat.today':'今日新建','stat.task':'当前任务','stat.idle':'空闲','emails.title':'邮箱列表','emails.sync':'云端同步','emails.sync_title':'从云端同步标签和状态','emails.copy_all':'复制全部','emails.export_txt':'导出已选 TXT','filter.account':'筛选账号:','filter.all_accounts':'全部账号','filter.export':'导出状态筛选','pager.per_page':'每页','pager.per_page_aria':'每页数量','pager.prev':'上一页','pager.next':'下一页','emails.empty':'还没有邮箱。请先添加账号，再点击「创建邮箱」。','inbox.limit_title':'邮件数量','inbox.search_ph':'搜索隐私邮箱查件...','inbox.search_title':'搜索已有隐私邮箱，查询这个地址的邮件','inbox.clear_title':'清除查询，查看全部收件','inbox.force':'强制刷新','inbox.force_title':'跳过缓存重新拉取','inbox.search':'查件','inbox.search_btn_title':'查询指定邮箱的收件','inbox.all':'全部','inbox.all_title':'检查所有隐私邮箱的收件','inbox.set_pwd':'设置收信密码','inbox.setup_title':'收件前先完成设置','inbox.step1':'在「账号」里添加 Apple 账号','inbox.step2':'为账号设置收信密码','inbox.step3':'选择账号后即可看信','inbox.start_setup':'开始设置','settings.auto':'自动创建','settings.auto_desc':'北京时间 7:00 到 20:00，每隔 60 到 90 分钟给每个有效账号创建 3 到 5 个邮箱。正在批量创建的账号会自动跳过。开启后会记住，服务重启仍会继续。','settings.create':'创建邮箱','settings.create_desc':'不同主账号最多 10 个并行；同一账号仍逐个创建，触发 Apple 临时限制时每次等待 1 分钟后自动续建。','settings.per_account':'每账号数量','settings.label':'标签','settings.label_ph':'可选','settings.start':'开始创建','settings.logs':'任务与日志','settings.logs_desc':'创建、同步和限制解除都会出现在这里。','settings.clear_logs':'清屏','settings.logs_empty':'还没有新日志。创建、同步和限制解除会显示在这里。','settings.logs_cleared':'已清屏，新日志会继续显示。','task.creating':'正在创建邮箱','task.wait_round':'等待下一轮','task.idle':'任务空闲','task.creating_short':'创建中','task.wait_short':'等待下轮','task.stopped':'已停止','action.stop_auto':'停止自动创建','action.start_auto':'启动自动创建','task.wait_limit':'等待限制解除','status.login_ok':'登录有效','status.login_expired':'登录已过期，请重新导入 Cookie','status.mail_ready':'可以收信','status.mail_blocked':'还不能收信','name.unnamed':'未命名','usage.capacity':'邮箱容量','action.check_login':'检查登录','action.set_mail':'设置收信','action.delete':'删除','filter.all_accounts_n':'全部账号 ({n})','pickup.gen_fail':'取件链接生成失败: {err}','copy.empty':'没有可复制的内容','copy.fail':'复制失败，请手动复制','pickup.missing':'取件链接尚未生成','pickup.copied':'取件链接已复制','export.need_select':'请先勾选未导出的邮箱','export.fail':'导出失败: {err}','error.unknown':'未知错误','export.already':'所选邮箱均已导出，未重复生成文件','export.done':'已导出 {n} 条','export.restore_confirm':'确认将 {email} 恢复为未导出？','export.restore_fail':'恢复失败: {err}','export.restored':'已恢复为未导出','export.unexported_n':'未导出 {n}','export.exported_n':'已导出 {n}','export.all_n':'全部 {n}','emails.empty_filter':'当前筛选下没有邮箱','pickup.generating':'正在生成取件链接...','table.select_page':'全选本页','table.email':'邮箱地址','table.lookup':'查件','table.pickup':'取件链接','table.account':'所属账号','table.label':'标签','table.created':'创建时间','table.export':'导出状态','table.status':'邮箱状态','status.active':'可用','status.inactive':'停用','export.exported':'已导出','export.restore_title':'恢复后可再次导出','export.restore':'恢复','export.unexported':'未导出','lookup.this_alias':'只看这个隐私邮箱的邮件','pickup.copy':'复制链接','pickup.failed':'生成失败','batch.wait_apple':'等待 Apple 限制解除','batch.queued':'等待中','batch.running':'创建中','batch.done':'已完成','batch.partial':'部分成功','batch.limited':'Apple 已限制','batch.failed':'失败','batch.available_n':'{n} 个可用账号','batch.none':'没有可用账号，请先添加','batch.retry_note':'上次触发限制，本次会再试一次','batch.retry_soon':'即将继续','batch.retry_sec':'约 {n} 秒后继续','batch.retry_min':'约 {n} 分钟后继续','batch.accounts_done':'{done}/{total} 个账号完成','batch.ok_fail':'{created} 成功 / {errors} 失败','batch.creating':'正在创建...','batch.progress_fail':'获取进度失败: {err}','batch.complete_n':'创建完成: {n} 个成功','batch.none_created':'本次没有创建成功，请查看账号错误','batch.need_account':'请勾选至少一个账号','batch.starting':'正在启动...','batch.start_fail':'创建任务启动失败','inbox.need_password':'这个账号还不能收信','inbox.need_password_text':'请先设置收信密码（Apple 的 App 专用密码），然后就可以查看邮件。','inbox.go_set_password':'去设置密码','inbox.choose_account':'请先选择账号','inbox.choose_account_text':'添加账号后，还要设置收信密码，才能在这里看信。','inbox.refetch':'正在重新拉取邮件...','inbox.connect_fail':'连接失败','inbox.title_n':'收件箱 ({n} 封)','inbox.title_loading':'收件箱 ({n} 封, 加载中...)','inbox.title_cut':'收件箱 ({n} 封, 连接中断)','inbox.fetching':'正在拉取邮件...','alias.no_match':'没有匹配的隐私邮箱','alias.none':'还没有隐私邮箱，请先创建','alias.shown_n':'已显示前 {n} 个，继续输入可缩小范围','alias.need_input':'请输入隐私邮箱地址','alias.querying':'正在查询 {alias} ...','alias.only':'仅显示 {alias}（{n} 封）','alias.empty':'{alias} 暂无收件','alias.checking':'正在检查各邮箱的收件...','alias.query_fail':'查询失败','alias.all_empty':'所有隐私邮箱暂无收件','mail.no_subject':'(无主题)','alias.summary':'共 {aliases} 个邮箱收到 {n} 封邮件','alias.count_n':'{alias} ({n} 封)','inbox.empty':'收件箱为空','mail.loading':'加载中...','mail.nobody':'(无法获取邮件正文)','mail.fetch_fail':'(获取失败: {err})','mail.unknown':'未知','mail.nobody2':'(无正文内容)','cache.ago':'缓存 {age} 前','cache.n':' | {n} 封已缓存 {txt}','pwd.change_title':'修改收信密码','pwd.set_title':'设置收信密码','pwd.account':'账号:','pwd.help_before':'在','pwd.help_after':'→ 登录与安全 → App 专用密码 生成。','pwd.reenter':' (重新输入以更新)','action.cancel':'取消','pwd.save':'保存并测试','pwd.need_email':'请输入 iCloud 邮箱','pwd.need_pwd':'请输入密码','pwd.testing':'测试中...','pwd.ok':'连接成功，收件箱 {n} 封','create.busy':'该账号正在创建，请稍候','create.ok':'成功创建 {n} 个','create.fail':'创建失败','status.login_ok_email':'登录有效: {email}','status.checking_login':'正在检查登录...','status.login_busy':'这个账号正在创建邮箱，登录还有效，创建结束后再检查','account.delete_confirm':'确认删除该账号？','account.deleted':'已删除','auto.stopped':'自动创建已停止','auto.started':'自动创建已启动','copy.one':'已复制: {email}','copy.n':'已复制 {n} 个','add.help_before':'Chrome 安装','add.help_mid':'，登录 icloud.com 后导出 Header String 粘贴即可。','add.help_json':'也支持 JSON：','add.download':'下载扩展','add.region':'区域','add.region_intl':'国际 (icloud.com)','add.region_cn':'中国 (icloud.com.cn)','add.name_ph':'账号名称，例如：主号','add.cookie_ph':'粘贴 Cookie，支持 Header String 或 JSON','add.unnamed':'未命名账号','add.need_cookie':'请粘贴 Cookie','add.checking':'正在检查登录...','add.ok':'已添加 {email}','sync.busy':'云端同步正在进行','sync.running':'同步中...','sync.fail':'云端同步失败: {err}','sync.partial':'同步完成，但有 {n} 个账号失败','sync.ok':'云端同步完成: {n} 个邮箱','inbox.select_account':'选择账号','inbox.ready':'可收信','inbox.no_pwd':'未设密码','api.timeout':'请求超时 ({n}s)','api.network':'网络错误','lang.zh':'中文','lang.en':'English','lang.group':'语言','pwd.icloud_email':'iCloud 邮箱','pwd.app_password':'App 专用密码'},en:{'nav.accounts':'Accounts','nav.emails':'Aliases','nav.inbox':'Inbox','nav.settings':'Settings','nav.batch':'Tasks','nav.logs':'Logs','side.overview':'Overview','side.ready':'Ready','side.foot':'Add an account, create aliases, then read or export mail.','social.group':'Social links','social.x':'X','social.telegram':'Telegram','action.refresh':'Refresh','action.add_account':'Add account','action.create_alias':'Create aliases','empty.title':'Add an account first','empty.text':'Import cookies, then you can create private aliases.','stat.today':'Created today','stat.task':'Current task','stat.idle':'Idle','emails.title':'Alias list','emails.sync':'Sync','emails.sync_title':'Sync labels and status from iCloud','emails.copy_all':'Copy all','emails.export_txt':'Export selected TXT','filter.account':'Account:','filter.all_accounts':'All accounts','filter.export':'Export status','pager.per_page':'Per page','pager.per_page_aria':'Rows per page','pager.prev':'Previous','pager.next':'Next','emails.empty':'No aliases yet. Add an account, then click Create aliases.','inbox.limit_title':'Message count','inbox.search_ph':'Search a private alias','inbox.search_title':'Search an existing alias to view its mail','inbox.clear_title':'Clear search and show full inbox','inbox.force':'Force refresh','inbox.force_title':'Fetch again and skip cache','inbox.search':'Lookup','inbox.search_btn_title':'Look up mail for this alias','inbox.all':'All','inbox.all_title':'Check mail for all aliases','inbox.set_pwd':'Set mail password','inbox.setup_title':'Finish setup before reading mail','inbox.step1':'Add an Apple account under Accounts','inbox.step2':'Set a mail password for the account','inbox.step3':'Choose an account to read mail','inbox.start_setup':'Start setup','settings.auto':'Auto create','settings.auto_desc':'From 07:00 to 20:00 Beijing time, every 60 to 90 minutes each valid account creates 3 to 5 aliases. Accounts already in a batch job are skipped. This stays on after restart.','settings.create':'Create aliases','settings.create_desc':'Up to 10 accounts run in parallel. The same account still creates one by one. If Apple rate-limits, it waits 1 minute and continues.','settings.per_account':'Per account','settings.label':'Label','settings.label_ph':'Optional','settings.start':'Start','settings.logs':'Jobs and logs','settings.logs_desc':'Create, sync, and rate-limit events show up here.','settings.clear_logs':'Clear','settings.logs_empty':'No logs yet. Create, sync, and rate-limit events show up here.','settings.logs_cleared':'Cleared. New logs will show up here.','task.creating':'Creating aliases','task.wait_round':'Waiting for next round','task.idle':'Idle','task.creating_short':'Creating','task.wait_short':'Waiting','task.stopped':'Stopped','action.stop_auto':'Stop auto create','action.start_auto':'Start auto create','task.wait_limit':'Waiting to resume','status.login_ok':'Signed in','status.login_expired':'Sign-in expired. Import cookies again.','status.mail_ready':'Mail ready','status.mail_blocked':'Mail not ready','name.unnamed':'Untitled','usage.capacity':'Alias capacity','action.check_login':'Check sign-in','action.set_mail':'Set mail','action.delete':'Delete','filter.all_accounts_n':'All accounts ({n})','pickup.gen_fail':'Could not create pickup links: {err}','copy.empty':'Nothing to copy','copy.fail':'Copy failed. Copy it manually.','pickup.missing':'Pickup link is not ready','pickup.copied':'Pickup link copied','export.need_select':'Select unexported aliases first','export.fail':'Export failed: {err}','error.unknown':'Unknown error','export.already':'Selected aliases were already exported','export.done':'Exported {n}','export.restore_confirm':'Restore {email} as not exported?','export.restore_fail':'Restore failed: {err}','export.restored':'Restored as not exported','export.unexported_n':'Not exported {n}','export.exported_n':'Exported {n}','export.all_n':'All {n}','emails.empty_filter':'No aliases in this filter','pickup.generating':'Creating pickup links...','table.select_page':'Select this page','table.email':'Alias','table.lookup':'Lookup','table.pickup':'Pickup link','table.account':'Account','table.label':'Label','table.created':'Created','table.export':'Export','table.status':'Status','status.active':'Active','status.inactive':'Inactive','export.exported':'Exported','export.restore_title':'Restore to export again','export.restore':'Restore','export.unexported':'Not exported','lookup.this_alias':'Show mail for this alias only','pickup.copy':'Copy link','pickup.failed':'Failed','batch.wait_apple':'Waiting for Apple limit to lift','batch.queued':'Waiting','batch.running':'Creating','batch.done':'Done','batch.partial':'Partial','batch.limited':'Apple limited','batch.failed':'Failed','batch.available_n':'{n} available accounts','batch.none':'No available accounts. Add one first.','batch.retry_note':'Rate-limited last time. This run will try again.','batch.retry_soon':'Continuing soon','batch.retry_sec':'In about {n}s','batch.retry_min':'In about {n} min','batch.accounts_done':'{done}/{total} accounts done','batch.ok_fail':'{created} created / {errors} failed','batch.creating':'Creating...','batch.progress_fail':'Could not load progress: {err}','batch.complete_n':'Created {n} aliases','batch.none_created':'Nothing was created. Check the account errors.','batch.need_account':'Select at least one account','batch.starting':'Starting...','batch.start_fail':'Could not start the create job','inbox.need_password':'This account cannot read mail yet','inbox.need_password_text':'Set an app-specific password first, then you can read mail.','inbox.go_set_password':'Set password','inbox.choose_account':'Choose an account first','inbox.choose_account_text':'After adding an account, set a mail password to read mail here.','inbox.refetch':'Fetching mail again...','inbox.connect_fail':'Connection failed','inbox.title_n':'Inbox ({n})','inbox.title_loading':'Inbox ({n}, loading...)','inbox.title_cut':'Inbox ({n}, disconnected)','inbox.fetching':'Fetching mail...','alias.no_match':'No matching private alias','alias.none':'No private aliases yet. Create some first.','alias.shown_n':'Showing first {n}. Type more to narrow results.','alias.need_input':'Enter a private alias','alias.querying':'Looking up {alias}...','alias.only':'Showing {alias} ({n})','alias.empty':'{alias} has no mail','alias.checking':'Checking mail for each alias...','alias.query_fail':'Lookup failed','alias.all_empty':'No mail on any private alias','mail.no_subject':'(No subject)','alias.summary':'{aliases} aliases received {n} messages','alias.count_n':'{alias} ({n})','inbox.empty':'Inbox is empty','mail.loading':'Loading...','mail.nobody':'(Could not load message body)','mail.fetch_fail':'(Failed: {err})','mail.unknown':'unknown','mail.nobody2':'(No message body)','cache.ago':'Cached {age} ago','cache.n':' | {n} cached {txt}','pwd.change_title':'Change mail password','pwd.set_title':'Set mail password','pwd.account':'Account:','pwd.help_before':'Create one at','pwd.help_after':'→ Sign-In and Security → App-Specific Passwords.','pwd.reenter':' (enter again to update)','action.cancel':'Cancel','pwd.save':'Save and test','pwd.need_email':'Enter an iCloud email','pwd.need_pwd':'Enter the password','pwd.testing':'Testing...','pwd.ok':'Connected. Inbox has {n} messages.','create.busy':'This account is already creating aliases','create.ok':'Created {n}','create.fail':'Create failed','status.login_ok_email':'Signed in: {email}','status.checking_login':'Checking sign-in...','status.login_busy':'This account is still creating aliases. Sign-in is still valid; check again when it finishes.','account.delete_confirm':'Delete this account?','account.deleted':'Deleted','auto.stopped':'Auto create stopped','auto.started':'Auto create started','copy.one':'Copied: {email}','copy.n':'Copied {n}','add.help_before':'In Chrome, install','add.help_mid':', sign in to icloud.com, export the Header String, then paste it here.','add.help_json':'JSON is also supported:','add.download':'Download extension','add.region':'Region','add.region_intl':'International (icloud.com)','add.region_cn':'China (icloud.com.cn)','add.name_ph':'Account name, e.g. Main','add.cookie_ph':'Paste cookies as Header String or JSON','add.unnamed':'Untitled account','add.need_cookie':'Paste cookies first','add.checking':'Checking sign-in...','add.ok':'Added {email}','sync.busy':'Sync already running','sync.running':'Syncing...','sync.fail':'iCloud sync failed: {err}','sync.partial':'Sync finished, but {n} account(s) failed','sync.ok':'Synced {n} aliases','inbox.select_account':'Choose account','inbox.ready':'Ready','inbox.no_pwd':'No password','api.timeout':'Request timed out ({n}s)','api.network':'Network error','lang.zh':'Chinese','lang.en':'English','lang.group':'Language','pwd.icloud_email':'iCloud email','pwd.app_password':'App-specific password'}};
var lang=(function(){try{var v=localStorage.getItem(LANG_KEY);if(v==='en'||v==='zh')return v;}catch(_){}return 'zh';})();
function t(key,vars){var dict=I18N[lang]||I18N.zh;var s=dict[key];if(s==null)s=(I18N.zh[key]!=null)?I18N.zh[key]:key;if(vars)Object.keys(vars).forEach(function(k){s=String(s).split('{'+k+'}').join(String(vars[k]));});return s;}
function applyStaticI18n(){document.documentElement.lang=lang==='en'?'en':'zh-CN';document.querySelectorAll('[data-i18n]').forEach(function(el){el.textContent=t(el.getAttribute('data-i18n'));});document.querySelectorAll('[data-i18n-placeholder]').forEach(function(el){el.setAttribute('placeholder',t(el.getAttribute('data-i18n-placeholder')));});document.querySelectorAll('[data-i18n-title]').forEach(function(el){el.setAttribute('title',t(el.getAttribute('data-i18n-title')));});document.querySelectorAll('[data-i18n-aria]').forEach(function(el){el.setAttribute('aria-label',t(el.getAttribute('data-i18n-aria')));});document.querySelectorAll('.lang-switch [data-lang]').forEach(function(btn){btn.classList.toggle('active',btn.getAttribute('data-lang')===lang);});}
function setLang(next){if(next!=='zh'&&next!=='en')return;if(next===lang)return;lang=next;try{localStorage.setItem(LANG_KEY,lang);}catch(_){}applyStaticI18n();if(E('tabTitle'))E('tabTitle').textContent=t('nav.'+curTab)||curTab;renderSidebar();updateEmptyState();if(curTab==='accounts')renderDashboard();if(curTab==='emails')renderAliasTable();if(curTab==='settings'){renderBatchPanel();renderLogs();}if(curTab==='inbox'){updateInboxAccountSelect();var aliasInput=E('aliasSearchInput');if(aliasInput&&aliasInput.value.trim())searchAliasMail();else refreshInbox();}}
var E=function(id){return document.getElementById(id)};
var state={running:false,creating:false,round_status:'',total_created:0,today_created:0,current_round_created:0,next_trigger:null};
var accounts=[],emails=[],logs=[],logCursor=0,sseTimer=null;
var curTab='emails',sseConn=null;
var pickupLinksByEmail={};var pickupLinksLoaded=false;var pickupSelected={};var exportFilter='unexported';var aliasPage=1;var aliasPageSize=50;
var batchJob=null;var batchPollTimer=null;var pendingBatchAccountId=null;
var _refreshBusy=false;var _createBusyByAccount={};var _aliasesBusy=false;
var _inboxBusy=false;var _inboxSse=null;var _inboxStreamMsgs=[];
var _inboxRequestSeq=0;var _inboxRenderedAccount='';var _expandedEmail=null;
var pendingAliasQuery=null;var _aliasSuggestIndex=-1;
document.querySelectorAll('.nav-item').forEach(function(el){
  el.addEventListener('click',function(){showTab(this.dataset.tab);});
});
function showView(id,on){var el=E(id);if(!el)return;var shown=el.style.display==='block';if(on){el.style.display='block';if(!shown){el.classList.remove('is-enter');void el.offsetWidth;el.classList.add('is-enter');}}else{el.style.display='none';el.classList.remove('is-enter');}}
function showTab(tab){
  if(tab==='batch'||tab==='logs'||tab==='dashboard'||tab==='docs')tab='settings';
  curTab=tab;
  document.querySelectorAll('.nav-item').forEach(function(n){n.classList.toggle('active',n.dataset.tab===curTab);});
  E('tabTitle').textContent=t('nav.'+curTab)||curTab;
  updateEmptyState();
  if(accounts.length){
    showView('view-accounts',curTab==='accounts');
    showView('view-emails',curTab==='emails');
    showView('view-inbox',curTab==='inbox');
    showView('view-settings',curTab==='settings');
  }
  if(curTab==='emails'){refreshEmails().then(renderAliasTable);}
  if(curTab==='accounts')renderDashboard();
  if(curTab==='settings'){renderBatchPanel();renderLogs();}
  if(curTab==='inbox'){updateInboxAccountSelect();bindAliasSearch();ensureEmailsLoaded();if(pendingAliasQuery)applyPendingAliasQuery();}
}
function handlePrimaryAction(accId){
  if(!accounts.length){showAddAccountModal();return;}
  pendingBatchAccountId=accId||null;
  showTab('settings');
  var box=E('btnBatchExec');if(box)box.scrollIntoView({behavior:'smooth',block:'center'});
}
function showCreateDrawer(){handlePrimaryAction();}
function updateEmptyState(){
  var empty=E('emptyState');
  var noAcc=!accounts.length;
  var showHero=noAcc && curTab!=='inbox';
  showView('emptyState',showHero);
  if(noAcc && curTab==='inbox'){
    showView('view-accounts',false);
    showView('view-emails',false);
    showView('view-inbox',true);
    showView('view-settings',false);
    renderInboxSetupHint();
  }else if(noAcc){
    E('emptyTitle').textContent=t('empty.title');
    E('emptyText').textContent=t('empty.text');
    E('emptyActionBtn').textContent=t('action.add_account');
    showView('view-accounts',false);
    showView('view-emails',false);
    showView('view-inbox',false);
    showView('view-settings',false);
  }else{
    showView('view-accounts',curTab==='accounts');
    showView('view-emails',curTab==='emails');
    showView('view-inbox',curTab==='inbox');
    showView('view-settings',curTab==='settings');
  }
  var btn=E('topPrimaryBtn');
  if(!accounts.length){btn.textContent=t('action.add_account');btn.onclick=showAddAccountModal;}
  else {btn.textContent=t('action.create_alias');btn.onclick=showCreateDrawer;}
}
async function api(path,opts){var timeout=(opts||{}).timeout||60000;if(opts)delete opts.timeout;var ctrl=new AbortController();var t=setTimeout(function(){ctrl.abort()},timeout);try{var r=await fetch(path,Object.assign({signal:ctrl.signal},opts||{}));clearTimeout(t);return r.json();}catch(e){clearTimeout(t);var msg=(e.name==='AbortError')?t('api.timeout',{n:timeout/1000}):(e.message||t('api.network'));return{ok:false,error:msg};}}
async function apiSlow(path,opts){return api(path,Object.assign({timeout:60000},opts||{}));}
async function refreshAll(){if(_refreshBusy)return;_refreshBusy=true;try{var _a=api('/api/accounts'),_s=api('/api/state');var a=await _a,s=await _s;accounts=a.accounts||[];state=s;renderSidebar();renderDashboard();updateEmptyState();if(curTab==='emails'){await refreshEmails();renderAliasTable();}if(curTab==='settings')renderBatchPanel();await loadLogs();updateInboxAccountSelect();}finally{_refreshBusy=false;}}
async function refreshLight(){if(_refreshBusy)return;var s=await api('/api/state');state=s;renderSidebar();}
async function refreshEmails(){var d=await api('/api/emails');emails=d.emails||[];pickupLinksByEmail={};emails.forEach(function(e){if(e.pickup_url)pickupLinksByEmail[String(e.email||'').toLowerCase()]=e.pickup_url;var acc=accounts.find(function(a){return a.id===e.account_id});e.account_name=acc?(acc.name||acc.real_email||''):(e.account_id||'');e.account_email=acc?(acc.real_email||''):'';});pickupLinksLoaded=true;E('emailCount').textContent=emails.length;updateEmailFilter();}

function renderSidebar(){
  var running=state.running;
  var creating=!!(state.creating||(batchJob&&(batchJob.status==='queued'||batchJob.status==='running')));
  ['schedDot','schedDotSettings','sideTaskDot'].forEach(function(id){var el=E(id);if(el)el.className='status-dot '+(creating?'online busy':(running?'online':'offline'));});
  var sm=running?(state.creating?t('task.creating'):t('task.wait_round')):t('task.idle');
  if(batchJob&&(batchJob.status==='queued'||batchJob.status==='running'))sm=t('task.creating');
  if(E('schedLabel'))E('schedLabel').textContent=sm;
  if(E('schedLabelSettings'))E('schedLabelSettings').textContent=running?(state.creating?t('task.creating_short'):t('task.wait_short')):t('task.stopped');
  if(E('sideTaskText'))E('sideTaskText').textContent=sm;
  if(E('sideStatAccounts'))E('sideStatAccounts').textContent=accounts.length;
  var aliasCount=emails.length||state.alias_count||accounts.reduce(function(n,a){return n+(a.alias_total||0);},0);
  if(E('sideStatEmails'))E('sideStatEmails').textContent=aliasCount;
  if(E('sideStatReady'))E('sideStatReady').textContent=accounts.filter(function(a){return a.has_app_password}).length;
  var bs=E('btnSched');
  if(bs){bs.textContent=running?t('action.stop_auto'):t('action.start_auto');bs.className='btn btn-sm '+(running?'btn-danger':'btn-primary');}
  renderAccountStrip();
}
function accountBatchItem(accId){if(!batchJob||!batchJob.accounts)return null;return batchJob.accounts[accId]||null;}
function renderAccountStrip(){if(!E('accStripAccounts'))return;var aliasCount=emails.length||state.alias_count||accounts.reduce(function(n,a){return n+(a.alias_total||0);},0);E('accStripAccounts').textContent=accounts.length;E('accStripEmails').textContent=aliasCount;E('accStripToday').textContent=state.today_created||0;var busy=!!(batchJob&&(batchJob.status==='queued'||batchJob.status==='running'));var task=t('stat.idle');if(busy){var target=batchTargetCount(batchJob)||0;var created=batchJob.total_created||0;task=(jobDisplayStatus(batchJob)==='waiting'?t('task.wait_limit'):t('task.creating_short'))+' '+created+(target?(' / '+target):'');}else if(state.creating){task=t('task.creating');}else if(state.running){task=t('task.wait_round');}E('accStripTask').textContent=task;E('accStripTask').parentElement.classList.toggle('is-live',busy||!!state.creating);}
function renderDashboard(){
  renderAccountStrip();
  var c=E('accCards');
  if(!c)return;
  if(!accounts.length){c.innerHTML='';return;}
  var limit=750;
  c.innerHTML=accounts.map(function(a){
    var stCls=a.status==='active'?'ok':'err';
    var stText=a.status==='active'?t('status.login_ok'):(a.last_error||t('status.login_expired'));
    var mailReady=a.has_app_password?t('status.mail_ready'):t('status.mail_blocked');
    var email=a.real_email||'';
    var used=a.alias_total||0;
    var pct=Math.min(100, used*100/limit);
    var job=accountBatchItem(a.id);
    var jobBusy=job&&(job.status==='queued'||job.status==='running'||job.status==='waiting');
    var jobHtml='';
    if(jobBusy){var accTarget=batchAccountTarget(batchJob,job),accCreated=job.created||0,accErrors=job.errors||0,mode=job.status==='waiting'?'is-wait':'is-run';jobHtml='<div class="acc-job"><div class="progress-head"><strong>'+esc(batchStatusText(job.status))+'</strong><span>'+accCreated+(accTarget?(' / '+accTarget):'')+'</span></div>'+progressBarHtml(accCreated,accErrors,accTarget||Math.max(accCreated+accErrors,1),mode)+'</div>';}
    return '<div class="acc-card'+(jobBusy?' is-busy':'')+'"><div class="acc-top"><div><div class="acc-title">'+esc(a.name||t('name.unnamed'))+'</div><div class="acc-email">'+esc(email)+'</div></div><span class="status-badge '+stCls+'">'+esc(stText.substring(0,42))+'</span></div><div class="acc-usage"><div class="progress-head"><span>'+t('usage.capacity')+'</span><span>'+used+' / '+limit+'</span></div><div class="progress-bar"><div class="fill ok" style="width:'+pct+'%"></div></div></div><div class="acc-stats"><div>'+esc(mailReady)+'</div></div>'+jobHtml+'<div class="acc-actions"><button class="btn btn-primary btn-xs" onclick="handlePrimaryAction(\''+escAttr(a.id)+'\')">'+t('action.create_alias')+'</button><button class="btn btn-outline btn-xs" onclick="validateAccount(\''+escAttr(a.id)+'\')">'+t('action.check_login')+'</button><button class="btn btn-outline btn-xs" onclick="showAppPwdModal(\''+escAttr(a.id)+'\')">'+t('action.set_mail')+'</button><button class="btn btn-outline btn-xs" onclick="removeAccount(\''+escAttr(a.id)+'\')">'+t('action.delete')+'</button></div></div>';
  }).join('');
}
function updateEmailFilter(){var sel=E('aliasFilter');if(!sel)return;var old=sel.value;sel.innerHTML='<option value="all">'+t('filter.all_accounts_n',{n:emails.length})+'</option>';var byAcc={};emails.forEach(function(e){var ak=e.account_id||'?';byAcc[ak]=(byAcc[ak]||0)+1;});Object.keys(byAcc).forEach(function(ak){var acc=accounts.find(function(x){return x.id===ak});var label=acc?(acc.name||acc.real_email||ak):ak;sel.innerHTML+='<option value="'+escAttr(ak)+'">'+esc(label)+' ('+byAcc[ak]+')</option>';});sel.value=old||'all';}
async function loadPickupLinks(){var d=await apiSlow('/api/pickup-links');if(d.error){toast(t('pickup.gen_fail',{err:d.error}),true);return}pickupLinksByEmail={};(d.links||[]).forEach(function(x){pickupLinksByEmail[String(x.email||'').toLowerCase()]=x.url});pickupLinksLoaded=true;}
function setAliasPageSize(size){aliasPageSize=parseInt(size,10)||50;aliasPage=1;renderAliasTable();}
function setAliasPage(page){aliasPage=parseInt(page,10)||1;if(aliasPage<1)aliasPage=1;renderAliasTable();}
function updateAliasPager(total){var pages=Math.max(1,Math.ceil((total||0)/aliasPageSize));if(aliasPage>pages)aliasPage=pages;if(aliasPage<1)aliasPage=1;var start=total?((aliasPage-1)*aliasPageSize+1):0;var end=Math.min(aliasPage*aliasPageSize,total||0);var info=E('aliasPageInfo');if(info)info.textContent=total?(start+'-'+end+' / '+total):'0';var prev=E('btnAliasPrev'),next=E('btnAliasNext');if(prev)prev.disabled=aliasPage<=1||!total;if(next)next.disabled=aliasPage>=pages||!total;document.querySelectorAll('[data-page-size]').forEach(function(btn){btn.classList.toggle('active',String(aliasPageSize)===String(btn.dataset.pageSize));});}
function setExportFilter(value){exportFilter=value;aliasPage=1;document.querySelectorAll('[data-export-filter]').forEach(function(btn){btn.classList.toggle('active',btn.dataset.exportFilter===value)});renderAliasTable();}
function togglePickupSelected(email,checked){var key=String(email||'').toLowerCase();if(checked)pickupSelected[key]=true;else delete pickupSelected[key];}
function toggleAllPickup(){var checks=document.querySelectorAll('#aliasTableContainer input.pickup-check:not(:disabled)');var shouldCheck=Array.from(checks).some(function(c){return !c.checked});checks.forEach(function(c){c.checked=shouldCheck;togglePickupSelected(c.dataset.email,shouldCheck);});}
function copyText(text,okMsg){text=String(text||'');if(!text){toast(t('copy.empty'),true);return}function fallback(){var ta=document.createElement('textarea');ta.value=text;ta.setAttribute('readonly','');ta.style.position='fixed';ta.style.left='-9999px';document.body.appendChild(ta);ta.select();var ok=false;try{ok=document.execCommand('copy')}catch(_){ok=false}document.body.removeChild(ta);if(ok)toast(okMsg);else toast(t('copy.fail'),true);}if(navigator.clipboard&&navigator.clipboard.writeText){navigator.clipboard.writeText(text).then(function(){toast(okMsg)}).catch(fallback);return;}fallback();}
function copyPickup(url){if(!url){toast(t('pickup.missing'),true);return}copyText(url,t('pickup.copied'));}
function visibleAliases(){var accountFilter=E('aliasFilter').value;return emails.filter(function(e){if(accountFilter!=='all'&&e.account_id!==accountFilter)return false;if(exportFilter==='exported')return !!e.exported;if(exportFilter==='unexported')return !e.exported;return true;});}
function formatBeijingTime(value){if(!value)return '--';var raw=String(value).trim();var d=new Date(raw);if(isNaN(d.getTime()))return raw.substring(0,19).replace('T',' ');try{var parts=new Intl.DateTimeFormat('en-GB',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false}).formatToParts(d);var map={};parts.forEach(function(p){map[p.type]=p.value;});var hour=map.hour==='24'?'00':map.hour;return map.year+'-'+map.month+'-'+map.day+' '+hour+':'+map.minute+':'+map.second;}catch(_){return raw.substring(0,19).replace('T',' ');}}
function formatExportTime(value){if(!value)return '--';try{return new Date(value).toLocaleString('zh-CN',{hour12:false})}catch(_){return value}}
async function exportSelectedPickupTxt(){var selected=emails.filter(function(e){return !e.exported&&pickupSelected[String(e.email||'').toLowerCase()]}).map(function(e){return e.email});if(!selected.length){toast(t('export.need_select'),true);return}var d=await apiSlow('/api/pickup-links/export',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({emails:selected})});if(!d.ok){toast(t('export.fail',{err:d.error||t('error.unknown')}),true);return}if(!(d.lines||[]).length){toast(t('export.already'),true);await refreshEmails();renderAliasTable();return}var b=new Blob(['\uFEFF'+d.lines.join('\n')],{type:'text/plain;charset=utf-8'}),a=document.createElement('a');a.href=URL.createObjectURL(b);a.download='icloud_mail_pickup_links_'+new Date().toISOString().slice(0,10)+'.txt';a.click();setTimeout(function(){URL.revokeObjectURL(a.href)},1000);selected.forEach(function(email){delete pickupSelected[String(email).toLowerCase()]});await refreshEmails();renderAliasTable();toast(t('export.done',{n:d.count}));}
async function restoreExportedEmail(email){if(!confirm(t('export.restore_confirm',{email:email})))return;var d=await api('/api/export-history/restore',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({emails:[email]})});if(!d.ok){toast(t('export.restore_fail',{err:d.error||t('error.unknown')}),true);return}await refreshEmails();renderAliasTable();toast(t('export.restored'));}
function renderAliasTable(){updateEmailFilter();var filtered=visibleAliases();var exportedCount=emails.filter(function(e){return e.exported}).length;var unexportedCount=emails.length-exportedCount;E('exportCountUnexported').textContent=t('export.unexported_n',{n:unexportedCount});E('exportCountExported').textContent=t('export.exported_n',{n:exportedCount});E('exportCountAll').textContent=t('export.all_n',{n:emails.length});E('emailCount').textContent=filtered.length+' / '+emails.length;updateAliasPager(filtered.length);var c=E('aliasTableContainer');if(!filtered.length){c.innerHTML='<div class="empty"><div class="icon"></div>'+(emails.length?t('emails.empty_filter'):t('emails.empty'))+'</div>';return;}if(!pickupLinksLoaded){c.innerHTML='<div class="empty">'+t('pickup.generating')+'</div>';loadPickupLinks().then(renderAliasTable);return;}var start=(aliasPage-1)*aliasPageSize;var pageItems=filtered.slice(start,start+aliasPageSize);var pages=Math.max(1,Math.ceil(filtered.length/aliasPageSize));var h='<table class="email-table"><thead><tr><th style="width:42px"><input type="checkbox" title="'+escAttr(t('table.select_page'))+'" onclick="toggleAllPickup()"></th><th>#</th><th>'+t('table.email')+'</th><th>'+t('table.lookup')+'</th><th>'+t('table.pickup')+'</th><th>'+t('table.account')+'</th><th>'+t('table.label')+'</th><th>'+t('table.created')+'</th><th>'+t('table.export')+'</th><th>'+t('table.status')+'</th></tr></thead><tbody>';pageItems.forEach(function(e,i){var key=String(e.email||'').toLowerCase();var url=pickupLinksByEmail[key]||'';var checked=pickupSelected[key]&&!e.exported?' checked':'';var disabled=e.exported?' disabled':'';var accName=e.account_name||e.account_email||e.account_id||'--';var activeHtml=e.hasOwnProperty('active')?(e.active?'<span style="color:var(--green)">'+t('status.active')+'</span>':'<span style="color:var(--red)">'+t('status.inactive')+'</span>'):'<span style="color:var(--muted)">--</span>';var exportHtml=e.exported?'<span style="color:var(--green)">'+t('export.exported')+'</span><div class="hint">'+esc(formatExportTime(e.exported_at))+'</div><button class="copy-btn" onclick="restoreExportedEmail(\''+escAttr(e.email||'')+'\')" title="'+escAttr(t('export.restore_title'))+'">'+t('export.restore')+'</button>':'<span style="color:var(--muted)">'+t('export.unexported')+'</span>';h+='<tr><td><input class="pickup-check" type="checkbox" data-email="'+escAttr(e.email||'')+'"'+checked+disabled+' onchange="togglePickupSelected(this.dataset.email,this.checked)"></td><td class="hint">'+(start+i+1)+'</td><td class="mono">'+esc(e.email||'')+'</td><td class="pickup-cell"><button class="copy-btn" onclick="openAliasInbox(\''+escAttr(e.email||'')+'\',\''+escAttr(e.account_id||'')+'\')" title="'+escAttr(t('lookup.this_alias'))+'">'+t('table.lookup')+'</button></td><td class="pickup-cell">'+(url?'<button class="copy-btn" onclick="copyPickup(\''+escAttr(url)+'\')" title="'+escAttr(url)+'">'+t('pickup.copy')+'</button>':'<span class="hint">'+t('pickup.failed')+'</span>')+'</td><td>'+esc(accName)+'</td><td class="hint">'+esc((e.label||'').substring(0,30))+'</td><td style="white-space:nowrap">'+esc(formatExportTime(e.created_at))+'</td><td>'+exportHtml+'</td><td>'+activeHtml+'</td></tr>';});h+='</tbody></table>';h+='<div class="pager pager-bottom"><span class="hint">'+(start+1)+'-'+(start+pageItems.length)+' / '+filtered.length+'</span><button class="btn btn-outline btn-sm" onclick="setAliasPage(aliasPage-1)"'+(aliasPage<=1?' disabled':'')+'>'+t('pager.prev')+'</button><button class="btn btn-outline btn-sm" onclick="setAliasPage(aliasPage+1)"'+(aliasPage>=pages?' disabled':'')+'>'+t('pager.next')+'</button></div>';c.innerHTML=h;}
function batchStatusText(status){var labels={waiting:t('batch.wait_apple'),queued:t('batch.queued'),running:t('batch.running'),completed:t('batch.done'),partial:t('batch.partial'),limited:t('batch.limited'),failed:t('batch.failed')};return labels[status]||status||'--';}
function renderBatchPanel(){var activeAccs=accounts.filter(function(a){return a.status==='active'});E('batchAccCount').textContent=t('batch.available_n',{n:activeAccs.length});var g=E('batchChkGroup');if(!activeAccs.length){g.innerHTML='<span class="hint">'+t('batch.none')+'</span>';E('btnBatchExec').disabled=true;}else{g.innerHTML=activeAccs.map(function(a){var email=a.real_email||a.name||a.id;var limited=a.create_status==='limited';var note=limited?'<span style="color:var(--red);font-size:12px">'+t('batch.retry_note')+'</span>':'';var busy=batchBusyAccountIds(batchJob);var isBusy=!!busy[a.id];var selected=(!pendingBatchAccountId||pendingBatchAccountId===a.id)&&!isBusy;return'<label class="chk-item"><input type="checkbox" value="'+escAttr(a.id)+'"'+(isBusy?' disabled':'')+(selected?' checked':'')+'><span><strong>'+esc(a.name||email.substring(0,20))+'</strong> '+note+'</span></label>';}).join('');E('btnBatchExec').disabled=activeAccs.every(function(a){return busy[a.id]});}pendingBatchAccountId=null;if(batchJob)renderBatchJob(batchJob);else loadCurrentBatchJob();}
async function loadCurrentBatchJob(){var d=await api('/api/create-batch-current');if(d.ok&&d.job){batchJob=d.job;renderBatchJob(batchJob);if(batchJob.status==='queued'||batchJob.status==='running')scheduleBatchPoll();}}
function jobDisplayStatus(job){var waiting=false,runningAcc=false;Object.keys(job.accounts||{}).forEach(function(id){var st=(job.accounts[id]||{}).status;if(st==='waiting')waiting=true;if(st==='running')runningAcc=true;});if((job.status==='queued'||job.status==='running')&&waiting&&!runningAcc)return 'waiting';return job.status;}
function batchBusyAccountIds(job){var ids={};if(!job||(job.status!=='queued'&&job.status!=='running'))return ids;Object.keys(job.accounts||{}).forEach(function(id){var st=(job.accounts[id]||{}).status;if(st==='queued'||st==='running'||st==='waiting')ids[id]=true;});return ids;}function batchAccountTarget(job,item){return parseInt((item||{}).target,10)||parseInt(job.count_per_account,10)||0;}function batchTargetCount(job){var accs=job.accounts||{};var ids=Object.keys(accs);var target=0;ids.forEach(function(id){target+=batchAccountTarget(job,accs[id]);});return target||((job.total_created||0)+(job.total_errors||0));}
function progressBarHtml(created,errors,target,mode){var createdPct=target?Math.min(100,created*100/target):0;var errorPct=target?Math.min(100-createdPct,errors*100/target):0;if(created&&createdPct<1.2)createdPct=1.2;return '<div class="progress-bar'+(mode?(' '+mode):'')+'"><div class="fill ok" style="width:'+createdPct+'%"></div>'+(errorPct?('<div class="fill err" style="width:'+errorPct+'%"></div>'):'')+'</div>';}
function retryLeftText(retryAt){if(!retryAt)return '';var t=Date.parse(retryAt);if(!t)return '';var sec=Math.max(0,Math.round((t-Date.now())/1000));if(sec<=0)return t('batch.retry_soon');if(sec<60)return t('batch.retry_sec',{n:sec});return t('batch.retry_min',{n:Math.ceil(sec/60)});}
function renderBatchJob(job){var box=E('batchProgress');if(!job){box.innerHTML='';return}var total=job.total_accounts||0,done=job.completed_accounts||0,created=job.total_created||0,errors=job.total_errors||0,target=batchTargetCount(job)||0;var processed=target?Math.min(target,created+errors):created+errors;var pct=target?Math.round(processed*100/target):0;var displayStatus=jobDisplayStatus(job);var statusColor=displayStatus==='completed'?'var(--green)':(displayStatus==='failed'||displayStatus==='limited'||displayStatus==='waiting')?'var(--red)':'var(--ink)';var running=job.status==='queued'||job.status==='running';var barMode=displayStatus==='waiting'?'is-wait':(running?'is-run':'');var h='<div class="progress-card"><div class="progress-head"><strong style="color:'+statusColor+'">'+esc(batchStatusText(displayStatus))+'</strong><span>'+created+' / '+target+' · '+pct+'%</span></div>'+progressBarHtml(created,errors,target,barMode)+'<div class="progress-meta"><span>'+t('batch.accounts_done',{done:done,total:total})+'</span><span>'+t('batch.ok_fail',{created:created,errors:errors})+'</span></div>';Object.keys(job.accounts||{}).forEach(function(id){var item=job.accounts[id],color=item.status==='completed'?'var(--green)':(item.status==='limited'||item.status==='failed'||item.status==='waiting')?'var(--red)':'var(--muted)';var accTarget=batchAccountTarget(job,item),accCreated=item.created||0,accErrors=item.errors||0;var accMode=item.status==='waiting'?'is-wait':((item.status==='running'||item.status==='queued')?'is-run':'');var extra=retryLeftText(item.retry_at);var note=item.error||((item.status==='running'||item.status==='queued')?'正在向 Apple 申请':'');h+='<div class="progress-item"><div class="progress-head"><strong>'+esc(item.name||id)+'</strong><span style="color:'+color+'">'+esc(batchStatusText(item.status))+(accTarget?(' · '+accCreated+' / '+accTarget):(' · '+accCreated))+'</span></div>'+progressBarHtml(accCreated,accErrors,accTarget||Math.max(accCreated+accErrors,1),accMode)+((note||extra)?('<div class="progress-note" style="color:var(--red)">'+esc(note)+(extra?(' · '+esc(extra)):'' )+'</div>'):'')+'</div>';});h+='</div>';box.innerHTML=h;var busy=batchBusyAccountIds(job);var checks=document.querySelectorAll('#batchChkGroup input[type=checkbox]');var canStart=false;checks.forEach(function(box){if(!busy[box.value])canStart=true;});E('btnBatchExec').disabled=!canStart;E('btnBatchExec').textContent=t('settings.start');renderSidebar();if(curTab==='accounts')renderDashboard();}
function scheduleBatchPoll(){if(batchPollTimer)clearTimeout(batchPollTimer);batchPollTimer=setTimeout(pollBatchJob,1200);}
async function pollBatchJob(){if(!batchJob||!batchJob.id)return;var d=await api('/api/create-batch/'+encodeURIComponent(batchJob.id));if(!d.ok){toast(t('batch.progress_fail',{err:d.error||t('error.unknown')}),true);return}batchJob=d.job;renderBatchJob(batchJob);if(batchJob.status==='queued'||batchJob.status==='running'){scheduleBatchPoll();return}await refreshAll();if(batchJob.total_created){toast(t('batch.complete_n',{n:batchJob.total_created}));}else{toast(t('batch.none_created'),true);}}
async function execBatchCreate(){var checks=document.querySelectorAll('#batchChkGroup input:checked');var ids=[];checks.forEach(function(c){ids.push(c.value)});if(!ids.length){toast(t('batch.need_account'),true);return}var count=Math.max(1,Math.min(parseInt(E('batchCount').value)||5,750));E('batchCount').value=count;var label=E('batchLabel').value.trim();var btn=E('btnBatchExec');btn.disabled=true;btn.textContent=t('batch.starting');var d=await api('/api/create-batch',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({account_ids:ids,count_per_account:count,label:label})});if(!d.ok){btn.disabled=false;btn.textContent=t('settings.start');if(d.job_id){batchJob={id:d.job_id,status:'running'};scheduleBatchPoll();}toast(d.error||t('batch.start_fail'),true);return}batchJob=d.job;renderBatchJob(batchJob);scheduleBatchPoll();}
function setInboxBusy(busy){_inboxBusy=busy;['btnInboxSearch','btnInboxAll'].forEach(function(id){var btn=E(id);if(btn)btn.disabled=busy});}
function beginInboxRequest(){if(_inboxSse){_inboxSse.close();_inboxSse=null}_inboxStreamMsgs=[];_inboxRequestSeq+=1;setInboxBusy(true);return _inboxRequestSeq;}
function inboxRequestCurrent(seq,accId){return seq===_inboxRequestSeq&&E('inboxAccount').value===accId;}
function finishInboxRequest(seq){if(seq!==_inboxRequestSeq)return;setInboxBusy(false);}
function inboxSetupHintHtml(){
  var accId=E('inboxAccount')?E('inboxAccount').value:'';
  var acc=accId?accounts.find(function(a){return a.id===accId}):null;
  if(acc&&!acc.has_app_password){
    return '<div class="empty"><div class="icon"></div><div class="empty-title">'+t('inbox.need_password')+'</div><p>'+t('inbox.need_password_text')+'</p><button class="btn btn-primary" onclick="showAppPwdModal(\''+escAttr(accId)+'\')">'+t('inbox.go_set_password')+'</button></div>';
  }
  if(!accounts.length){
    return '<div class="empty"><div class="icon"></div><div class="empty-title">'+t('inbox.setup_title')+'</div><ol class="empty-steps"><li>'+t('inbox.step1')+'</li><li>'+t('inbox.step2')+'</li><li>'+t('inbox.step3')+'</li></ol><button class="btn btn-primary" onclick="showAddAccountModal()">'+t('action.add_account')+'</button></div>';
  }
  return '<div class="empty"><div class="icon"></div><div class="empty-title">'+t('inbox.choose_account')+'</div><p>'+t('inbox.choose_account_text')+'</p><button class="btn btn-primary" onclick="openInboxSettings()">'+t('inbox.set_pwd')+'</button></div>';
}
function renderInboxSetupHint(){var el=E('inboxMsgs');if(el)el.innerHTML=inboxSetupHintHtml();}
function renderInboxSetupHintIfNeeded(){
  var sel=E('inboxAccount');
  var accId=sel?sel.value:'';
  var acc=accId?accounts.find(function(a){return a.id===accId}):null;
  if(!accId||(acc&&!acc.has_app_password))renderInboxSetupHint();
}
function refreshInbox(force){var aliasInput=E('aliasSearchInput');var alias=aliasInput?aliasInput.value.trim():'';if(alias){searchAliasMail(!!force);return;}var accId=E('inboxAccount').value;if(!accId){beginInboxRequest();finishInboxRequest(_inboxRequestSeq);renderInboxSetupHint();return}var acc=accounts.find(function(a){return a.id===accId});if(acc&&!acc.has_app_password){renderInboxSetupHint();return}var seq=beginInboxRequest();var limit=parseInt(E('inboxLimit').value)||20;if(force){E('inboxMsgs').innerHTML='<div class="empty"><div class="icon"></div>'+t('inbox.refetch')+'</div>';api('/api/accounts/'+encodeURIComponent(accId)+'/inbox?limit='+limit+'&force=1',{timeout:120000}).then(function(d){if(!inboxRequestCurrent(seq,accId))return;if(d.error){E('inboxMsgs').innerHTML='<div class="empty"><div class="icon"></div>'+esc(d.error||t('inbox.connect_fail'))+'</div>';finishInboxRequest(seq);return}renderInboxMsgs(d.emails||[],t('inbox.title_n',{n:d.count||0}),accId);updateCacheStatus(d.cached);finishInboxRequest(seq);});return}startInboxStream(accId,seq);}
function startInboxStream(accId,seq){E('inboxMsgs').innerHTML='<div class="empty"><div class="icon"></div>'+t('inbox.fetching')+'</div>';var limit=parseInt(E('inboxLimit').value)||20;var source=new EventSource('/api/accounts/'+encodeURIComponent(accId)+'/inbox-stream?limit='+limit);_inboxSse=source;source.onmessage=function(e){if(!inboxRequestCurrent(seq,accId)||_inboxSse!==source){source.close();return}try{var d=JSON.parse(e.data);if(d.type==='email'){_inboxStreamMsgs.push(d.email);renderInboxMsgs(_inboxStreamMsgs,t('inbox.title_loading',{n:d.count}),accId)}else if(d.type==='done'){source.close();_inboxSse=null;renderInboxMsgs(_inboxStreamMsgs,t('inbox.title_n',{n:d.count}),accId);finishInboxRequest(seq)}else if(d.type==='error'){source.close();_inboxSse=null;E('inboxMsgs').innerHTML='<div class="empty"><div class="icon"></div>'+esc(d.error||t('inbox.connect_fail'))+'</div>';finishInboxRequest(seq)}}catch(_){}};source.onerror=function(){if(!inboxRequestCurrent(seq,accId)||_inboxSse!==source){source.close();return}source.close();_inboxSse=null;if(_inboxStreamMsgs.length){renderInboxMsgs(_inboxStreamMsgs,t('inbox.title_cut',{n:_inboxStreamMsgs.length}),accId)}else{E('inboxMsgs').innerHTML='<div class="empty"><div class="icon"></div>'+t('inbox.connect_fail')+'</div>'}finishInboxRequest(seq);};}
function ensureEmailsLoaded(){if(emails.length)return Promise.resolve();return refreshEmails();}
function inboxAliases(){var accId=E('inboxAccount')?E('inboxAccount').value:'';return emails.filter(function(e){if(!e||!e.email)return false;if(accId&&e.account_id!==accId)return false;return true;});}
function resolveAliasQuery(raw){var q=String(raw||'').trim();if(!q)return '';var lower=q.toLowerCase();var list=inboxAliases();var exact=null;list.forEach(function(e){if(String(e.email||'').toLowerCase()===lower)exact=e;});if(exact)return exact.email;var hits=list.filter(function(e){return String(e.email||'').toLowerCase().indexOf(lower)>=0;});if(hits.length===1)return hits[0].email;return q;}
function updateAliasSearchClear(){var btn=E('btnAliasSearchClear');var input=E('aliasSearchInput');if(btn)btn.hidden=!(input&&input.value.trim());}
function hideAliasSuggest(){var box=E('aliasSuggest');if(box){box.hidden=true;box.innerHTML='';}_aliasSuggestIndex=-1;}
function aliasSuggestItems(){var box=E('aliasSuggest');return box?box.querySelectorAll('button[data-email]'):[];}
function highlightAliasSuggest(){var buttons=aliasSuggestItems();buttons.forEach(function(btn,i){btn.classList.toggle('active',i===_aliasSuggestIndex);});if(_aliasSuggestIndex>=0&&buttons[_aliasSuggestIndex])buttons[_aliasSuggestIndex].scrollIntoView({block:'nearest'});}
function renderAliasSuggest(){var box=E('aliasSuggest');var input=E('aliasSearchInput');if(!box||!input)return;var q=input.value.trim().toLowerCase();var list=inboxAliases();var matches=q?list.filter(function(e){var email=String(e.email||'').toLowerCase();var label=String(e.label||'').toLowerCase();return email.indexOf(q)>=0||label.indexOf(q)>=0;}):list;var total=matches.length;matches=matches.slice(0,12);if(!matches.length){box.hidden=false;box.innerHTML='<div class="hint">'+(emails.length?t('alias.no_match'):t('alias.none'))+'</div>';_aliasSuggestIndex=-1;return;}var extra=total>matches.length?'<div class="hint">'+t('alias.shown_n',{n:matches.length})+'</div>':'';box.hidden=false;box.innerHTML=matches.map(function(e,i){return '<button type="button" role="option" data-email="'+escAttr(e.email)+'" data-account="'+escAttr(e.account_id||'')+'" class="'+(i===_aliasSuggestIndex?'active':'')+'" onmousedown="event.preventDefault();pickAliasSuggest(this.dataset.email,this.dataset.account)">'+esc(e.email)+'</button>';}).join('')+extra;}
function pickAliasSuggest(email,accountId){var input=E('aliasSearchInput');if(input)input.value=email;if(accountId&&E('inboxAccount')&&E('inboxAccount').value!==accountId)E('inboxAccount').value=accountId;updateAliasSearchClear();hideAliasSuggest();searchAliasMail();}
function onInboxAccountChange(){var accId=E('inboxAccount')?E('inboxAccount').value:'';var input=E('aliasSearchInput');var alias=input?input.value.trim().toLowerCase():'';if(alias){var owned=emails.some(function(e){return e.account_id===accId&&String(e.email||'').toLowerCase()===alias;});if(!owned){input.value='';updateAliasSearchClear();}}hideAliasSuggest();refreshInbox();}
function clearAliasSearch(){if(E('aliasSearchInput'))E('aliasSearchInput').value='';hideAliasSuggest();updateAliasSearchClear();refreshInbox();}
function openAliasInbox(email,accountId){pendingAliasQuery={email:email,accountId:accountId||''};showTab('inbox');}
function applyPendingAliasQuery(){var q=pendingAliasQuery;pendingAliasQuery=null;if(!q||!q.email)return;if(q.accountId&&E('inboxAccount'))E('inboxAccount').value=q.accountId;if(E('aliasSearchInput'))E('aliasSearchInput').value=q.email;updateAliasSearchClear();var accId=E('inboxAccount')?E('inboxAccount').value:'';var acc=accId?accounts.find(function(a){return a.id===accId;}):null;if(!accId||(acc&&!acc.has_app_password)){renderInboxSetupHint();return;}searchAliasMail();}
function bindAliasSearch(){var input=E('aliasSearchInput');if(!input||input.dataset.bound==='1')return;input.dataset.bound='1';input.addEventListener('focus',function(){ensureEmailsLoaded().then(function(){_aliasSuggestIndex=-1;renderAliasSuggest();});});input.addEventListener('input',function(){updateAliasSearchClear();_aliasSuggestIndex=-1;renderAliasSuggest();});input.addEventListener('keydown',function(ev){var buttons=aliasSuggestItems();if(ev.key==='ArrowDown'&&buttons.length){ev.preventDefault();if(E('aliasSuggest')&&E('aliasSuggest').hidden)renderAliasSuggest();buttons=aliasSuggestItems();_aliasSuggestIndex=Math.min(buttons.length-1,_aliasSuggestIndex+1);highlightAliasSuggest();}else if(ev.key==='ArrowUp'&&buttons.length){ev.preventDefault();_aliasSuggestIndex=Math.max(0,_aliasSuggestIndex-1);highlightAliasSuggest();}else if(ev.key==='Enter'){ev.preventDefault();if(_aliasSuggestIndex>=0&&buttons[_aliasSuggestIndex]){pickAliasSuggest(buttons[_aliasSuggestIndex].dataset.email,buttons[_aliasSuggestIndex].dataset.account);}else{hideAliasSuggest();searchAliasMail();}}else if(ev.key==='Escape'){hideAliasSuggest();}});input.addEventListener('blur',function(){setTimeout(hideAliasSuggest,120);});document.addEventListener('click',function(ev){var wrap=E('aliasSearchWrap');if(wrap&&!wrap.contains(ev.target))hideAliasSuggest();});}
async function searchAliasMail(force){var input=E('aliasSearchInput');var alias=resolveAliasQuery(input?input.value:'');if(!alias){toast(t('alias.need_input'),true);return}if(input)input.value=alias;updateAliasSearchClear();hideAliasSuggest();var accId=E('inboxAccount')?E('inboxAccount').value:'';if(!accId){var hit=emails.find(function(e){return String(e.email||'').toLowerCase()===alias.toLowerCase();});if(hit){accId=hit.account_id;if(E('inboxAccount'))E('inboxAccount').value=accId;}}if(!accId){toast(t('inbox.choose_account'),true);return}var acc=accounts.find(function(a){return a.id===accId;});if(acc&&!acc.has_app_password){renderInboxSetupHint();return}var seq=beginInboxRequest();E('inboxMsgs').innerHTML='<div class="empty"><div class="icon"></div>'+t('alias.querying',{alias:esc(alias)})+'</div>';var limit=parseInt(E('inboxLimit')&&E('inboxLimit').value,10)||20;var path='/api/accounts/'+encodeURIComponent(accId)+'/mail/'+encodeURIComponent(alias)+'?limit='+limit+(force?'&force=1':'');var d=await api(path,{timeout:120000});if(!inboxRequestCurrent(seq,accId))return;if(d.error){E('inboxMsgs').innerHTML='<div class="empty"><div class="icon"></div>'+esc(d.error)+'</div>'}else{var msgs=d.emails||[];renderInboxMsgs(msgs,msgs.length?t('alias.only',{alias:alias,n:d.count||msgs.length}):t('alias.empty',{alias:alias}),accId)}finishInboxRequest(seq);}
async function checkAliasMail(){if(E('aliasSearchInput'))E('aliasSearchInput').value='';updateAliasSearchClear();hideAliasSuggest();var accId=E('inboxAccount').value;if(!accId){toast(t('inbox.choose_account'),true);return}var seq=beginInboxRequest();E('inboxMsgs').innerHTML='<div class="empty"><div class="icon"></div>'+t('alias.checking')+'</div>';var d=await api('/api/accounts/'+encodeURIComponent(accId)+'/alias-mail',{timeout:120000});if(!inboxRequestCurrent(seq,accId))return;if(d.error){E('inboxMsgs').innerHTML='<div class="empty"><div class="icon"></div>'+esc(d.error||t('alias.query_fail'))+'</div>';finishInboxRequest(seq);return}var byAlias=d.by_alias||{},total=0,aliasKeys=Object.keys(byAlias),h='';if(!aliasKeys.length){E('inboxMsgs').innerHTML='<div class="empty"><div class="icon"></div>'+t('alias.all_empty')+'</div>';finishInboxRequest(seq);return}aliasKeys.forEach(function(alias){var msgs=sortInboxNewest(byAlias[alias]||[]);total+=msgs.length;h+='<div style="padding:8px 14px;border-bottom:1px solid var(--line);font-weight:600">'+esc(t('alias.count_n',{alias:alias,n:msgs.length}))+'</div>';msgs.forEach(function(m){h+='<div style="padding:6px 20px;border-bottom:1px solid var(--line);font-size:12px;display:flex;justify-content:space-between;flex-wrap:wrap;gap:4px"><span><strong>'+esc(m.subject||t('mail.no_subject'))+'</strong></span><span style="color:var(--muted)">'+esc(m.from||'').substring(0,30)+'</span><span class="hint">'+formatBeijingTime(m.date)+'</span></div>';});});E('inboxMsgs').innerHTML='<div class="hint" style="padding:8px 14px;border-bottom:1px solid var(--line)">'+t('alias.summary',{aliases:aliasKeys.length,n:total})+'</div>'+h;finishInboxRequest(seq);}
function inboxDateValue(m){var raw=String((m&&m.date)||'');var t=Date.parse(raw);return isNaN(t)?0:t;}
function sortInboxNewest(msgs){return (msgs||[]).slice().sort(function(a,b){return inboxDateValue(b)-inboxDateValue(a);});}
function renderInboxMsgs(msgs,title,accountId){msgs=sortInboxNewest(msgs);_inboxRenderedAccount=accountId||E('inboxAccount').value;if(!msgs.length){E('inboxMsgs').innerHTML='<div class="empty"><div class="icon"></div>'+esc(title||t('inbox.empty'))+'</div>';return}var h='<div class="hint" style="padding:8px 16px;border-bottom:1px solid var(--line)">'+esc(title)+'</div>';msgs.forEach(function(m,i){var mid=m.id||'m'+i;h+='<div class="email-item" style="border-bottom:1px solid var(--line);cursor:pointer" onclick="toggleEmail(\''+escAttr(mid)+'\',\''+escAttr(m.id||'')+'\',\''+escAttr(_inboxRenderedAccount)+'\')"><div style="padding:12px 16px;display:flex;justify-content:space-between;align-items:flex-start;gap:12px"><div style="flex:1;min-width:0"><div style="font-weight:600;margin-bottom:4px">'+esc(m.subject||t('mail.no_subject'))+'</div><div style="font-size:12px;color:var(--muted)">'+esc(m.from||'')+'</div><div class="hint" style="margin-top:2px">To: '+esc((m.to||'').substring(0,50))+'</div></div><div class="hint" style="white-space:nowrap">'+formatBeijingTime(m.date)+'</div></div><div id="'+escAttr(mid)+'_body" style="display:none;padding:0 16px 16px;line-height:1.7;color:var(--muted);white-space:pre-wrap;word-break:break-word;max-height:400px;overflow-y:auto;border-top:1px solid var(--line)"></div></div>'});E('inboxMsgs').innerHTML=h;}
async function toggleEmail(domId,msgId,accountId){var bodyEl=E(domId+'_body');if(!bodyEl)return;if(_expandedEmail&&_expandedEmail!==domId){var prev=E(_expandedEmail+'_body');if(prev)prev.style.display='none'}if(bodyEl.style.display==='block'){bodyEl.style.display='none';_expandedEmail=null;return}bodyEl.style.display='block';_expandedEmail=domId;if(bodyEl.textContent.trim()&&bodyEl.textContent!==t('mail.loading'))return;bodyEl.textContent=t('mail.loading');if(!msgId||!accountId){bodyEl.textContent=t('mail.nobody');return}var d=await api('/api/accounts/'+encodeURIComponent(accountId)+'/message/'+encodeURIComponent(msgId),{timeout:120000});if(!d.ok||!d.message){bodyEl.textContent=t('mail.fetch_fail',{err:d.error||t('mail.unknown')});return}bodyEl.textContent=d.message.body||t('mail.nobody2');}
function updateCacheStatus(cached){if(!cached)return;var age=cached.cache_age_sec||0;var txt=age<300?t('cache.ago',{age:age<60?Math.round(age)+'s':Math.round(age/60)+'m'}):'';E('cacheStatus').textContent=cached.inbox_cached?t('cache.n',{n:cached.inbox_cached,txt:txt}):'';}
function openInboxSettings(){
  var sel=E('inboxAccount');
  var accId=sel?sel.value:'';
  if(!accounts.length){showAddAccountModal();return;}
  if(!accId){
    var need=accounts.find(function(a){return !a.has_app_password});
    if(!need){toast(t('inbox.choose_account'),true);return;}
    sel.value=need.id;
    showAppPwdModal(need.id);
    return;
  }
  showAppPwdModal(accId);
}
function showAppPwdModal(accId){var acc=accounts.find(function(a){return a.id===accId});var name=acc?(acc.name||acc.real_email||accId):accId;var icloudEmail='';if(acc&&acc.icloud_email&&(acc.icloud_email.indexOf('@icloud.com')>=0||acc.icloud_email.indexOf('@me.com')>=0||acc.icloud_email.indexOf('@mac.com')>=0)){icloudEmail=acc.icloud_email;}else if(acc&&acc.real_email&&(acc.real_email.indexOf('@icloud.com')>=0||acc.real_email.indexOf('@me.com')>=0)){icloudEmail=acc.real_email;}var hasPwd=acc&&acc.has_app_password;var h='<div class="modal-overlay" id="appPwdModal" onclick="if(event.target===this)closeAppPwdModal()"><div class="modal-box"><h3>'+(hasPwd?t('pwd.change_title'):t('pwd.set_title'))+'</h3><p>'+t('pwd.account')+' <b>'+esc(name)+'</b><br>'+t('pwd.help_before')+' <a href="https://account.apple.com/" target="_blank" rel="noopener noreferrer">account.apple.com</a> '+t('pwd.help_after')+'</p><label class="hint">'+t('pwd.icloud_email')+'</label><input type="text" id="icloudEmailInput" value="'+escAttr(icloudEmail)+'" placeholder="xxx@icloud.com"><label class="hint">'+t('pwd.app_password')+(hasPwd?t('pwd.reenter'):'')+'</label><input type="password" id="appPwdInput" placeholder="xxxx-xxxx-xxxx-xxxx"><div class="modal-actions"><button class="btn btn-outline" onclick="closeAppPwdModal()">'+t('action.cancel')+'</button><button class="btn btn-primary" id="btnSetPwd" onclick="setAppPassword(\''+escAttr(accId)+'\')">'+t('pwd.save')+'</button></div><div class="modal-msg" id="appPwdMsg"></div></div></div>';document.body.insertAdjacentHTML('beforeend',h);}
function closeAppPwdModal(){var m=E('appPwdModal');if(m)m.remove()}
async function setAppPassword(accId){var pwd=E('appPwdInput').value.trim();var email=E('icloudEmailInput').value.trim();if(!email){E('appPwdMsg').innerHTML='<span style="color:var(--red)">'+t('pwd.need_email')+'</span>';return}if(!pwd){E('appPwdMsg').innerHTML='<span style="color:var(--red)">'+t('pwd.need_pwd')+'</span>';return}var btn=E('btnSetPwd');btn.disabled=true;btn.textContent=t('pwd.testing');var d=await api('/api/accounts/'+encodeURIComponent(accId)+'/app-password',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({app_password:pwd,icloud_email:email})});btn.disabled=false;btn.textContent=t('pwd.save');if(d.ok){E('appPwdMsg').innerHTML='<span style="color:var(--green)">'+t('pwd.ok',{n:d.inbox_count})+'</span>';var acc=accounts.find(function(a){return a.id===accId});if(acc){acc.has_app_password=true;acc.icloud_email=email;}setTimeout(closeAppPwdModal,1500);updateInboxAccountSelect();renderDashboard();if(curTab==='inbox')refreshInbox();}else{E('appPwdMsg').innerHTML='<span style="color:var(--red)">'+esc(d.error||t('inbox.connect_fail'))+'</span>';}}
function setCreateBusy(accId,busy){if(busy)_createBusyByAccount[accId]=true;else delete _createBusyByAccount[accId];document.querySelectorAll('.acc-actions button').forEach(function(btn){var action=btn.getAttribute('onclick')||'';if(action.indexOf("createForAccount('"+accId+"'")>=0)btn.disabled=busy;});}
async function createForAccount(accId,count){if(_createBusyByAccount[accId]){toast(t('create.busy'),true);return}setCreateBusy(accId,true);try{var d=await api('/api/accounts/'+encodeURIComponent(accId)+'/create',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({count:count})});if(d.ok)toast(t('create.ok',{n:d.created}));else toast(d.error||t('create.fail'),true);}finally{setCreateBusy(accId,false);await refreshAll();}}
async function validateAccount(accId){toast(t('status.checking_login'));var d=await api('/api/accounts/'+encodeURIComponent(accId)+'/validate',{method:'POST'});if(d.ok)toast(t('status.login_ok_email',{email:d.real_email}));else toast(d.error||t('status.login_expired'),true);refreshAll();}
async function removeAccount(accId){if(!confirm(t('account.delete_confirm')))return;var d=await api('/api/accounts/'+encodeURIComponent(accId)+'/remove',{method:'POST'});if(d.ok)toast(t('account.deleted'));refreshAll();}
async function toggleScheduler(){var act=state.running?'stop':'start';var d=await api('/api/scheduler/'+act,{method:'POST'});if(d.ok)toast(state.running?t('auto.stopped'):t('auto.started'));refreshAll();}
function copyOne(email){copyText(email,t('copy.one',{email:email}));}
function copyAll(){var filtered=visibleAliases();if(!filtered.length){toast(t('emails.empty_filter'),true);return}copyText(filtered.map(function(e){return e.email}).join('\n'),t('copy.n',{n:filtered.length}));}
function csvCell(v){v=String(v==null?'':v);if(/^[=+\-@]/.test(v))v="'"+v;return '"'+v.replace(/"/g,'""')+'"';}
function exportCSV(){var filtered=visibleAliases();if(!filtered.length){toast(t('emails.empty_filter'),true);return}var csv='email,account,label,active\n'+filtered.map(function(e){return [e.email,e.account_name||e.account_id||'',e.label||'',e.hasOwnProperty('active')?(e.active?'yes':'no'):''].map(csvCell).join(',');}).join('\n');var b=new Blob(['\uFEFF'+csv],{type:'text/csv'}),a=document.createElement('a'),u=URL.createObjectURL(b);a.href=u;a.download='icloud_mail_aliases.csv';a.click();setTimeout(function(){URL.revokeObjectURL(u)},1000);toast(t('export.done',{n:filtered.length}));}
function appendLog(entry){if(!entry||(entry.seq||0)<=logCursor)return false;logCursor=entry.seq;logs.push(entry);if(logs.length>500)logs=logs.slice(-500);return true;}
async function loadLogs(){var d=await api('/api/logs?after='+logCursor);if(!d||d.ok===false)return;(d.logs||[]).forEach(function(entry){appendLog(entry);});if(curTab==='settings')renderLogs();}
function clearLogs(){logs=[];var f=E('logFeed');if(f)f.innerHTML='<div class="log-line hint">'+t('settings.logs_cleared')+'</div>';}
function toast(msg,isErr){var t=E('toast');t.textContent=msg;t.style.background=isErr?'var(--red)':'var(--ink)';t.classList.add('show');setTimeout(function(){t.classList.remove('show')},2200);}
function connectSSE(){if(sseTimer){clearTimeout(sseTimer);sseTimer=null}if(sseConn){sseConn.close();sseConn=null}sseConn=new EventSource('/api/log-stream?after='+logCursor);sseConn.onmessage=function(e){try{var entry=JSON.parse(e.data);if(!appendLog(entry))return;if(curTab==='settings')renderLogs();if(entry.msg&&entry.msg.indexOf('创建')>=0)refreshLight();}catch(_){}};sseConn.onerror=function(){if(!sseConn||sseConn.readyState!==EventSource.CLOSED)return;sseConn=null;sseTimer=setTimeout(connectSSE,3000)};}
function renderLogs(){var f=E('logFeed');if(!f)return;if(!logs.length){f.innerHTML='<div class="log-line hint">'+t(logCursor?'settings.logs_cleared':'settings.logs_empty')+'</div>';return;}f.innerHTML=logs.map(function(l){return'<div class="log-line '+l.level+'"><span class="log-time">'+esc(l.time)+'</span>'+esc(l.msg)+'</div>';}).join('\n');f.scrollTop=f.scrollHeight;}
function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;')}
function escAttr(s){return String(s).replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/'/g,'&#39;')}
function showAddAccountModal(){var h='<div class="modal-overlay" id="addAccModal" onclick="if(event.target===this)closeAddAccModal()"><div class="modal-box"><h3>'+t('action.add_account')+'</h3><p>'+t('add.help_before')+' <a href="https://chromewebstore.google.com/detail/cookie-editor/hlkenndednhfkekhgcdicdfddnkalmdm" target="_blank" rel="noopener noreferrer">Cookie Editor</a>'+t('add.help_mid')+'<br>'+t('add.help_json')+'<code>{"name1":"value1"}</code> <a href="https://chromewebstore.google.com/detail/cookie-editor/hlkenndednhfkekhgcdicdfddnkalmdm" target="_blank" rel="noopener noreferrer">'+t('add.download')+'</a></p><label class="hint">'+t('add.region')+'</label><select id="accHostInput"><option value="icloud.com" selected>'+t('add.region_intl')+'</option><option value="icloud.com.cn">'+t('add.region_cn')+'</option></select><input type="text" id="accNameInput" placeholder="'+escAttr(t('add.name_ph'))+'"><textarea id="cookieInput" placeholder="'+escAttr(t('add.cookie_ph'))+'"></textarea><div class="modal-actions"><button class="btn btn-outline" onclick="closeAddAccModal()">'+t('action.cancel')+'</button><button class="btn btn-primary" id="btnAddAccount" onclick="addAccount()">'+t('action.add_account')+'</button></div><div class="modal-msg" id="addAccMsg"></div></div></div>';document.body.insertAdjacentHTML('beforeend',h);}
function closeAddAccModal(){var m=E('addAccModal');if(m)m.remove()}
async function addAccount(){var name=E('accNameInput').value.trim()||t('add.unnamed');var cookies=E('cookieInput').value.trim();if(!cookies){E('addAccMsg').innerHTML='<span style="color:var(--red)">'+t('add.need_cookie')+'</span>';return}var btn=E('btnAddAccount');btn.disabled=true;btn.textContent=t('add.checking');var d=await api('/api/accounts/add',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:name,cookie_input:cookies,host:(E('accHostInput')&&E('accHostInput').value)||'icloud.com'})});btn.disabled=false;btn.textContent=t('action.add_account');if(d.ok){E('addAccMsg').innerHTML='<span style="color:var(--green)">'+t('add.ok',{email:esc(d.real_email||'')})+'</span>';setTimeout(closeAddAccModal,1200);refreshAll();}else{E('addAccMsg').innerHTML='<span style="color:var(--red)">'+esc(d.error||t('status.login_expired'))+'</span>';}}
async function refreshAliases(){if(_aliasesBusy){toast(t('sync.busy'),true);return}_aliasesBusy=true;var btn=E('btnAliasSync');if(btn){btn.disabled=true;btn.textContent=t('sync.running')}try{var d=await api('/api/aliases',{timeout:120000});if(d.error&&d.ok===false){toast(t('sync.fail',{err:d.error}),true);return}var apiAliases=d.aliases||[],apiMap={};apiAliases.forEach(function(a){apiMap[String(a.email||'').toLowerCase()]=a;});await refreshEmails();emails.forEach(function(e){var apiData=apiMap[String(e.email||'').toLowerCase()];if(apiData){e.label=apiData.label||'';e.active=apiData.active;e.anonymousId=apiData.anonymousId;e.created_at=apiData.createdAt||e.created_at;e.account_name=apiData.account_name||e.account_name;e.account_email=apiData.account_email||e.account_email;}});renderAliasTable();var failed=Object.keys(d.failures||{});if(failed.length){toast(t('sync.partial',{n:failed.length}),true)}else{toast(t('sync.ok',{n:apiAliases.length}))}}finally{_aliasesBusy=false;if(btn){btn.disabled=false;btn.textContent=t('emails.sync')}}}
function updateInboxAccountSelect(){var sel=E('inboxAccount');if(!sel)return;var old=sel.value;sel.innerHTML='<option value="">'+t('inbox.select_account')+'</option>';accounts.forEach(function(a){var hasPwd=a.has_app_password?t('inbox.ready'):t('inbox.no_pwd');var imapEmail=a.icloud_email||a.real_email||'';sel.innerHTML+='<option value="'+escAttr(a.id)+'">'+esc((a.name||a.real_email||a.id).substring(0,20))+' | '+esc(imapEmail.substring(0,25))+' '+hasPwd+'</option>';});sel.value=old||'';renderInboxSetupHintIfNeeded();}
function renderDocs(){var el=E('docsContent');if(el)el.innerHTML='';}
bindAliasSearch();
applyStaticI18n();
refreshAll().then(connectSSE);setInterval(refreshLight,10000);setInterval(refreshAll,30000);
</script>
</body>
</html>
"""
# ----- Flask Routes -----

@app.route("/")
@app.route("/index.html")
def index(): return render_template_string(UI_HTML)

@app.route("/api/state")
def api_state():
    summary = _account_mgr.get_summary()
    with _lock:
        state = dict(_global_state); state.update(summary)
        state["cookies_ok"] = summary["active_accounts"] > 0
        state["alias_count"] = summary["total_aliases"]
        state["alias_active"] = summary["total_active_aliases"]
    return jsonify(state)

@app.route("/api/accounts")
def api_accounts():
    accounts = _account_mgr.list_accounts()
    safe = []
    for a in accounts:
        ac = {k:v for k,v in a.items() if k not in ("cookies", "app_password")}
        ac["has_cookies"] = bool(a.get("cookies"))
        ac["has_app_password"] = bool(a.get("app_password"))
        safe.append(ac)
    return jsonify({"accounts":safe,"count":len(safe)})

@app.route("/api/accounts/add", methods=["POST"])
def api_add_account():
    data = request.get_json() or {}
    name = data.get("name","未命名账号")
    cookie_input = data.get("cookie_input","")
    if not cookie_input:
        return jsonify({"ok":False,"error":"请提供 cookie_input"}), 400
    requested_host = str(data.get("host") or "").strip().lower()
    if requested_host in ("icloud.com", "icloud.com.cn"):
        host = requested_host
    else:
        host = AccountManager.detect_icloud_host(cookie_input)
    try:
        account = _account_mgr.add_account(name, cookie_input, host=host)
        _emit_log("info",f"添加账号: {account.get('name','')} ({account.get('real_email','?')})")
        ok = account.get("status") == "active"
        payload = {"ok":ok,"id":account["id"],"name":account["name"],"real_email":account.get("real_email",""),"alias_total":account.get("alias_total",0),"alias_active":account.get("alias_active",0),"status":account.get("status","")}
        if not ok:
            payload["error"] = account.get("last_error") or "账号校验失败"
        return jsonify(payload), 200 if ok else 400
    except ValueError as e:
        return jsonify({"ok":False,"error":str(e)}), 400
    except Exception as e:
        return jsonify({"ok":False,"error":str(e)}), 500


_ACTIVE_BATCH_ACCOUNT_STATUSES = ("queued", "running", "waiting")
_FINISHED_BATCH_ACCOUNT_STATUSES = ("completed", "partial", "failed", "limited")
_batch_runner_jobs = set()


def _batch_account_target(job, acc_id=None, entry=None):
    item = entry if entry is not None else ((job.get("accounts") or {}).get(acc_id) or {})
    try:
        target = int(item.get("target") or 0)
    except (TypeError, ValueError):
        target = 0
    if target > 0:
        return target
    try:
        return max(0, int(job.get("count_per_account") or 0))
    except (TypeError, ValueError):
        return 0


def _new_batch_account_entry(acc_id, count):
    return {
        "account_id": acc_id,
        "name": (_account_mgr.get_account(acc_id) or {}).get("name") or acc_id,
        "status": "queued",
        "created": 0,
        "errors": 0,
        "limited": False,
        "error": "",
        "retry_count": 0,
        "retry_delay_seconds": 0,
        "retry_at": None,
        "finished_at": None,
        "target": int(count),
    }


def _pending_batch_account_ids(job, inflight_ids=None):
    inflight_ids = inflight_ids or set()
    pending = []
    for acc_id in job.get("account_ids") or []:
        if acc_id in inflight_ids:
            continue
        entry = (job.get("accounts") or {}).get(acc_id) or {}
        if entry.get("finished_at") and entry.get("status") in _FINISHED_BATCH_ACCOUNT_STATUSES:
            continue
        pending.append(acc_id)
    return pending


def _batch_uses_account(acc_id):
    with _batch_lock:
        if not _batch_active_id:
            return False
        job = _batch_jobs.get(_batch_active_id) or {}
        if job.get("status") not in ("queued", "running"):
            return False
        item = (job.get("accounts") or {}).get(acc_id) or {}
        return item.get("status") in _ACTIVE_BATCH_ACCOUNT_STATUSES



def _account_create_in_progress(acc_id) -> bool:
    acc_id = str(acc_id or "")
    if not acc_id:
        return False
    with _manual_create_lock:
        if acc_id in _manual_creating_accounts:
            return True
    return _batch_uses_account(acc_id)


def _remove_latest_emails_for_account(acc_id):
    latest_file = RESULTS_DIR / "latest_emails.txt"
    if not latest_file.exists():
        return 0
    with _account_mgr._latest_emails_lock:
        lines = latest_file.read_text(encoding="utf-8").splitlines()
        kept = []
        removed = 0
        for line in lines:
            parts = line.split("\t")
            if len(parts) > 1 and parts[1].strip() == acc_id:
                removed += 1
            else:
                kept.append(line)
        if removed:
            tmp = latest_file.with_suffix(latest_file.suffix + ".tmp")
            payload = "\n".join(kept)
            if payload:
                payload += "\n"
            with open(tmp, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, latest_file)
    return removed


def _purge_account_data(acc_id):
    global _pickup_body_cache_bytes
    with _pickup_refresh_lock:
        _removed_account_ids.add(acc_id)
    stats = {
        "pickup_links": _pickup_store.revoke_account(acc_id),
        "latest_emails": _remove_latest_emails_for_account(acc_id),
        "export_history": _export_store.delete_account(acc_id),
    }
    _account_mgr._cache.clear_account(acc_id)
    _pickup_body_store.delete_account(acc_id)
    with _pickup_refresh_lock:
        _pickup_refreshing_accounts.discard(acc_id)
        _pickup_last_account_refresh.pop(acc_id, None)
        _pickup_refresh_errors.pop(acc_id, None)
        _pickup_error_log_state.pop(acc_id, None)
    with _pickup_refresh_lock:
        for key in list(_pickup_body_cache):
            if key[0] == acc_id:
                cached = _pickup_body_cache.pop(key, None)
                if cached:
                    _pickup_body_cache_bytes -= cached[0]
        for key in list(_pickup_body_refreshing):
            if key[0] == acc_id:
                _pickup_body_refreshing.discard(key)
    return stats

@app.route("/api/accounts/<acc_id>/remove", methods=["POST"])
def api_remove_account(acc_id):
    if not _account_mgr.get_account(acc_id):
        return jsonify({"ok":False,"error":"账号不存在"}), 404
    with _manual_create_lock:
        manual_busy = acc_id in _manual_creating_accounts
    if manual_busy or _batch_uses_account(acc_id):
        return jsonify({"ok":False,"error":"账号正在创建邮箱，暂时不能删除"}), 409
    with _pickup_refresh_lock:
        _removed_account_ids.add(acc_id)
    ok = _account_mgr.remove_account(acc_id)
    if not ok:
        with _pickup_refresh_lock:
            _removed_account_ids.discard(acc_id)
        return jsonify({"ok":False,"error":"账号不存在"}), 404
    cleanup = _purge_account_data(acc_id)
    _emit_log("info", f"删除账号并清理本地数据: {acc_id}")
    return jsonify({"ok":True,"cleanup":cleanup})

@app.route("/api/accounts/<acc_id>/validate", methods=["POST"])
def api_validate_account(acc_id):
    try:
        if _account_create_in_progress(acc_id):
            return jsonify({
                "ok": False,
                "busy": True,
                "error": "这个账号正在创建邮箱，登录还有效，创建结束后再检查",
            }), 409
        account = _account_mgr.validate_account(acc_id)
        ok = account.get("status") == "active"
        payload = {"ok":ok,"real_email":account.get("real_email",""),"alias_total":account.get("alias_total",0)}
        if not ok:
            payload["error"] = account.get("last_error") or "账号校验失败"
        return jsonify(payload), 200 if ok else 400
    except Exception as e: return jsonify({"ok":False,"error":str(e)})

def _ensure_pickup_for_created(result):
    if not result or not result.get("ok"):
        return None
    account_id = str(result.get("account_id") or "")
    email = str(result.get("email") or "").strip().lower()
    if not account_id or not email:
        return None
    return _pickup_store.ensure(account_id, email)


@app.route("/api/accounts/<acc_id>/create", methods=["POST"])
def api_create_for_account(acc_id):
    data = request.get_json() or {}
    try:
        count = int(data.get("count", 1))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "创建数量无效"}), 400
    if count < 1 or count > 750:
        return jsonify({"ok": False, "error": "创建数量必须在 1 到 750 之间"}), 400
    if not _account_mgr.get_account(acc_id):
        return jsonify({"ok": False, "error": "账号不存在"}), 404
    with _manual_create_lock:
        if acc_id in _manual_creating_accounts or _batch_uses_account(acc_id):
            return jsonify({"ok": False, "error": "该账号已有创建任务正在运行"}), 409
        _manual_creating_accounts.add(acc_id)
    label = data.get("label","")
    _update_state(creating=True)
    _emit_log("info",f"手动创建: 账号 {acc_id} x{count}")
    try:
        results = _account_mgr.create_aliases_for_account(
            acc_id, count, label, progress_callback=_ensure_pickup_for_created
        )
        created = [r["email"] for r in results if r.get("ok")]
        errors = [r["error"] for r in results if not r.get("ok")]
        _increment_state(today_created=len(created), total_created=len(created))
        if created: _emit_log("success",f"创建完成: {len(created)} 个")
        status = 200 if created else 400
        return jsonify({"ok":len(created)>0,"emails":created,"created":len(created),"errors":len(errors),"error":errors[0] if errors else None}), status
    except Exception as e:
        return jsonify({"ok":False,"error":str(e)}), 500
    finally:
        with _manual_create_lock:
            _manual_creating_accounts.discard(acc_id)
        _update_state(creating=False)

def _batch_job_snapshot(job_id):
    with _batch_lock:
        job = _batch_jobs.get(job_id)
        return json.loads(json.dumps(job, ensure_ascii=False)) if job else None


def _create_account_with_cooldown(job, acc_id, count, label, name):
    """Create the remaining aliases, pausing after Apple's temporary throttle."""
    successful = []
    with _batch_lock:
        already_created = int(job["accounts"][acc_id].get("created", 0) or 0)
    progress_created = 0

    def record_progress(_result):
        nonlocal progress_created
        _ensure_pickup_for_created(_result)
        progress_created += 1
        with _batch_lock:
            entry = job["accounts"][acc_id]
            entry["created"] = already_created + progress_created
            job["total_created"] = sum(
                int(account_entry.get("created", 0) or 0)
                for account_entry in job["accounts"].values()
            )
            job["updated_at"] = datetime.now(_BJ_TZ).isoformat()
            _save_batch_state_locked()

    while already_created + len(successful) < count:
        remaining = count - already_created - len(successful)
        _emit_log("info", f"[{name}] 继续创建剩余 {remaining} 个")
        stop_heartbeat = threading.Event()
        started_at = time.monotonic()

        def _heartbeat():
            while not stop_heartbeat.wait(_BATCH_CREATE_HEARTBEAT_SECONDS):
                waited = max(1, int(time.monotonic() - started_at))
                _emit_log(
                    "info",
                    f"[{name}] 仍在向 Apple 申请，已等待 {waited} 秒，剩余 {remaining} 个",
                )
                with _batch_lock:
                    entry = job["accounts"][acc_id]
                    entry["error"] = f"正在向 Apple 申请，已等待 {waited} 秒"
                    job["updated_at"] = datetime.now(_BJ_TZ).isoformat()
                    _save_batch_state_locked()

        threading.Thread(
            target=_heartbeat, daemon=True, name=f"create-hb-{acc_id}"
        ).start()
        try:
            results = _account_mgr.create_aliases_for_account(
                acc_id, remaining, label, progress_callback=record_progress
            )
        finally:
            stop_heartbeat.set()
        successful.extend(result for result in results if result.get("ok"))
        errors = [result for result in results if not result.get("ok")]
        if not errors:
            if already_created + len(successful) >= count:
                return successful
            return successful + [{
                "ok": False,
                "limited": False,
                "error": "创建接口未返回完整结果",
            }]

        first_error = str(errors[0].get("error") or "")
        retryable = any(result.get("retryable") for result in errors)
        if not retryable and not _is_temporary_create_limit(first_error):
            return successful + errors

        remaining_after_limit = count - already_created - len(successful)

        with _batch_lock:
            entry = job["accounts"][acc_id]
            previous_retries = int(entry.get("retry_count", 0) or 0)
            retry_delay = _BATCH_RETRY_DELAY_SECONDS
            retry_at = datetime.now(_BJ_TZ) + timedelta(seconds=retry_delay)
            retry_at_text = retry_at.strftime("%Y-%m-%d %H:%M:%S")
            retry_delay_text = _format_retry_delay(retry_delay)
            entry["status"] = "waiting"
            entry["created"] = already_created + len(successful)
            entry["retry_count"] = previous_retries + 1
            entry["retry_delay_seconds"] = retry_delay
            entry["retry_at"] = retry_at.isoformat()
            entry["error"] = (
                f"Apple 临时限制，等待 {retry_delay_text}，"
                f"{retry_at_text} 自动继续"
            )
            job["updated_at"] = datetime.now(_BJ_TZ).isoformat()
            _save_batch_state_locked()
        _emit_log(
            "warn",
            f"[{name}] Apple 临时限制，等待 {retry_delay_text} 后继续剩余 {remaining_after_limit} 个",
        )

        if _shutdown_event.wait(retry_delay):
            raise _BatchInterrupted()
        with _batch_lock:
            entry = job["accounts"][acc_id]
            entry["status"] = "running"
            entry["retry_at"] = None
            entry["error"] = ""
            job["updated_at"] = datetime.now(_BJ_TZ).isoformat()
            _save_batch_state_locked()

    return successful


def _run_batch_account(job, acc_id, count, label):
    if _shutdown_event.is_set():
        raise _BatchInterrupted()
    account = _account_mgr.get_account(acc_id)
    name = (account or {}).get("name") or acc_id
    with _batch_lock:
        entry = job["accounts"][acc_id]
        previous_created = int(entry.get("created", 0) or 0)
        entry["status"] = "running"
        entry["started_at"] = entry.get("started_at") or datetime.now(_BJ_TZ).isoformat()
        _save_batch_state_locked()
    _emit_log("info", f"[{name}] 开始创建，目标 {count} 个，已完成 {previous_created} 个")
    try:
        if not account:
            results = [{"ok": False, "error": "账号不存在", "limited": False}]
        elif account.get("status") != "active":
            results = [{"ok": False, "error": "账号不可用", "limited": False}]
        else:
            results = _create_account_with_cooldown(job, acc_id, count, label, name)
    except _BatchInterrupted:
        raise
    except Exception as exc:
        results = [{"ok": False, "error": str(exc)[:200], "limited": False}]

    created = previous_created + sum(1 for result in results if result.get("ok"))
    errors = [result for result in results if not result.get("ok")]
    limited = any(result.get("limited") for result in errors)
    first_error = str(errors[0].get("error") or "")[:200] if errors else ""
    if limited:
        status = "limited"
    elif created and errors:
        status = "partial"
    elif created:
        status = "completed"
    else:
        status = "failed"
    with _batch_lock:
        entry.update({
            "status": status,
            "created": created,
            "errors": len(errors),
            "limited": limited,
            "error": first_error,
            "retry_at": None,
            "finished_at": datetime.now(_BJ_TZ).isoformat(),
        })
        job["completed_accounts"] = sum(
            1 for account_entry in job["accounts"].values()
            if account_entry.get("finished_at")
        )
        job["total_created"] = sum(
            int(account_entry.get("created", 0) or 0)
            for account_entry in job["accounts"].values()
        )
        job["total_errors"] = sum(
            int(account_entry.get("errors", 0) or 0)
            for account_entry in job["accounts"].values()
        )
        job["updated_at"] = datetime.now(_BJ_TZ).isoformat()
        completed_accounts = job["completed_accounts"]
        _save_batch_state_locked()
    level = "warn" if errors else "success"
    detail = f" / {first_error}" if first_error else ""
    _emit_log(level, f"[{name}] {created} 成功 / {len(errors)} 失败{detail}")
    return completed_accounts


def _run_batch_job(job_id):
    global _batch_active_id
    with _batch_lock:
        if job_id in _batch_runner_jobs:
            return
        _batch_runner_jobs.add(job_id)
        job = _batch_jobs[job_id]
        job["status"] = "running"
        job["started_at"] = job.get("started_at") or datetime.now(_BJ_TZ).isoformat()
        _save_batch_state_locked()
    total_accounts = len(job["account_ids"])
    count = job.get("count_per_account")
    workers = min(_BATCH_MAX_ACCOUNT_WORKERS, max(1, total_accounts))
    _update_state(
        creating=True,
        round_status=f"批量创建 {job.get('completed_accounts', 0)}/{total_accounts} 个账号",
    )
    _emit_log(
        "info", f"批量任务启动: {total_accounts} 个账号 x{count}，并行账号数 {workers}"
    )

    try:
        with ThreadPoolExecutor(
            max_workers=_BATCH_MAX_ACCOUNT_WORKERS, thread_name_prefix="batch-account"
        ) as executor:
            futures = {}
            while True:
                with _batch_lock:
                    if _shutdown_event.is_set():
                        raise _BatchInterrupted()
                    job = _batch_jobs[job_id]
                    label = job.get("label") or ""
                    pending = _pending_batch_account_ids(job, set(futures.values()))
                    for acc_id in pending:
                        acc_count = _batch_account_target(job, acc_id)
                        futures[executor.submit(_run_batch_account, job, acc_id, acc_count, label)] = acc_id
                    total_accounts = len(job.get("account_ids") or [])
                if not futures:
                    break
                done, _pending = wait(futures, timeout=0.4, return_when=FIRST_COMPLETED)
                if not done:
                    continue
                for future in done:
                    futures.pop(future)
                    completed_accounts = future.result()
                    _update_state(
                        round_status=f"批量创建 {completed_accounts}/{total_accounts} 个账号"
                    )

        with _batch_lock:
            job["status"] = "completed" if job["total_created"] else "failed"
            job["finished_at"] = datetime.now(_BJ_TZ).isoformat()
            total_created = job["total_created"]
            total_errors = job["total_errors"]
            _save_batch_state_locked()
        _increment_state(today_created=total_created, total_created=total_created)
        _emit_log(
            "success" if total_created else "warn",
            f"批量任务完成: {total_created} 成功 / {total_errors} 失败",
        )
    except _BatchInterrupted:
        with _batch_lock:
            job["status"] = "queued"
            job["updated_at"] = datetime.now(_BJ_TZ).isoformat()
            _save_batch_state_locked()
    except Exception as exc:
        with _batch_lock:
            job["status"] = "failed"
            job["error"] = str(exc)[:300]
            job["finished_at"] = datetime.now(_BJ_TZ).isoformat()
            _save_batch_state_locked()
        _emit_log("error", f"批量任务异常: {str(exc)[:200]}")
    finally:
        with _batch_lock:
            _batch_runner_jobs.discard(job_id)
            if _batch_active_id == job_id and job.get("status") not in ("queued", "running"):
                _batch_active_id = None
            _save_batch_state_locked()
        final_text = "批量任务等待恢复" if job.get("status") == "queued" else "批量任务已完成"
        _update_state(creating=False, round_status=final_text)


def _resume_batch_job_if_needed():
    with _batch_lock:
        job_id = _batch_active_id
        job = _batch_jobs.get(job_id) if job_id else None
        if not job or job.get("status") not in ("queued", "running"):
            return False
        job["status"] = "queued"
        job["updated_at"] = datetime.now(_BJ_TZ).isoformat()
        for entry in job.get("accounts", {}).values():
            if not entry.get("finished_at") and entry.get("status") in ("running", "waiting"):
                entry["status"] = "queued"
                entry["retry_at"] = None
        _save_batch_state_locked()
    threading.Thread(target=_run_batch_job, args=(job_id,), daemon=True).start()
    _emit_log("info", "已恢复未完成的批量创建任务")
    return True


@app.route("/api/create-batch", methods=["POST"])
def api_create_batch():
    global _batch_active_id
    data = request.get_json(silent=True) or {}
    raw_ids = data.get("account_ids") or []
    if not isinstance(raw_ids, list):
        return jsonify({"ok": False, "error": "账号列表格式错误"}), 400
    account_ids = list(dict.fromkeys(str(item) for item in raw_ids if item))
    if not account_ids:
        return jsonify({"ok": False, "error": "请选择至少一个账号"}), 400
    if len(account_ids) > 100:
        return jsonify({"ok": False, "error": "单次最多选择 100 个主账号"}), 400
    try:
        count = int(data.get("count_per_account", 5))
        interval = max(0.0, min(float(data.get("interval", 3.0)), 30.0))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "创建数量或间隔无效"}), 400
    if count < 1 or count > 750:
        return jsonify({"ok": False, "error": "每账号创建数量必须在 1 到 750 之间"}), 400
    label = str(data.get("label") or "")[:100]

    with _manual_create_lock:
        busy_ids = [acc_id for acc_id in account_ids if acc_id in _manual_creating_accounts]
        if busy_ids:
            return jsonify({
                "ok": False,
                "error": "所选账号中已有手动创建任务正在运行",
                "account_ids": busy_ids,
            }), 409
        with _batch_lock:
            active = _batch_jobs.get(_batch_active_id) if _batch_active_id else None
            start_runner = True
            if active and active.get("status") in ("queued", "running"):
                overlap = []
                new_ids = []
                for acc_id in account_ids:
                    item = (active.get("accounts") or {}).get(acc_id) or {}
                    if item.get("status") in _ACTIVE_BATCH_ACCOUNT_STATUSES:
                        overlap.append(acc_id)
                    else:
                        new_ids.append(acc_id)
                if not new_ids:
                    return jsonify({
                        "ok": False,
                        "error": "所选账号已在创建中",
                        "account_ids": overlap,
                        "job_id": _batch_active_id,
                    }), 409
                now = datetime.now(_BJ_TZ).isoformat()
                for acc_id in new_ids:
                    active["accounts"][acc_id] = _new_batch_account_entry(acc_id, count)
                    if acc_id not in active["account_ids"]:
                        active["account_ids"].append(acc_id)
                active["total_accounts"] = len(active["account_ids"])
                active["completed_accounts"] = sum(
                    1 for entry in active["accounts"].values() if entry.get("finished_at")
                )
                if label:
                    active["label"] = label
                active["updated_at"] = now
                job_id = _batch_active_id
                start_runner = job_id not in _batch_runner_jobs
                _save_batch_state_locked()
            else:
                job_id = secrets.token_urlsafe(12)
                now = datetime.now(_BJ_TZ).isoformat()
                job = {
                    "id": job_id,
                    "status": "queued",
                    "account_ids": account_ids,
                    "count_per_account": count,
                    "interval": interval,
                    "label": label,
                    "total_accounts": len(account_ids),
                    "completed_accounts": 0,
                    "total_created": 0,
                    "total_errors": 0,
                    "created_at": now,
                    "updated_at": now,
                    "accounts": {
                        acc_id: _new_batch_account_entry(acc_id, count)
                        for acc_id in account_ids
                    },
                }
                _batch_jobs[job_id] = job
                while len(_batch_jobs) > _BATCH_JOB_HISTORY:
                    _batch_jobs.popitem(last=False)
                _batch_active_id = job_id
                start_runner = True
                _save_batch_state_locked()
    if start_runner:
        threading.Thread(target=_run_batch_job, args=(job_id,), daemon=True).start()
    return jsonify({"ok": True, "job_id": job_id, "job": _batch_job_snapshot(job_id)}), 202


@app.route("/api/create-batch/<job_id>")
def api_create_batch_status(job_id):
    job = _batch_job_snapshot(job_id)
    if not job:
        return jsonify({"ok": False, "error": "批量任务不存在"}), 404
    return jsonify({"ok": True, "job": job})


@app.route("/api/create-batch-current")
def api_create_batch_current():
    with _batch_lock:
        job_id = _batch_active_id or (next(reversed(_batch_jobs), None) if _batch_jobs else None)
    return jsonify({"ok": True, "job": _batch_job_snapshot(job_id) if job_id else None})

@app.route("/api/accounts/<acc_id>/app-password", methods=["POST"])
def api_set_app_password(acc_id):
    data = request.get_json() or {}
    pwd = data.get("app_password","").strip()
    icloud_email = data.get("icloud_email","").strip()
    if not pwd:
        return jsonify({"ok":False,"error":"密码不能为空"}), 400
    try:
        account = _account_mgr.get_account(acc_id)
        if not account:
            return jsonify({"ok": False, "error": "账号不存在"}), 404
        target_email = icloud_email or account.get("icloud_email", "")
        if not target_email:
            return jsonify({"ok": False, "error": "iCloud 邮箱不能为空"}), 400
        from icloud_mail import ICloudMail
        result = ICloudMail(target_email, pwd).test_connection()
        if not result.get("ok"):
            return jsonify(result), 400
        _account_mgr._drop_mail_client(acc_id)
        _account_mgr.update_account(
            acc_id, app_password=pwd, icloud_email=target_email
        )
        return jsonify(result)
    except Exception as e:
        return jsonify({"ok":False,"saved":False,"error":str(e)}), 400

@app.route("/api/accounts/<acc_id>/inbox")
def api_inbox(acc_id):
    limit = request.args.get("limit",50,type=int)
    force = request.args.get("force","0")=="1"
    try:
        emails = _account_mgr.check_inbox(acc_id, limit=limit, force=force)
        stats = _account_mgr._cache.get_stats(acc_id)
        return jsonify({"emails":emails,"count":len(emails),"cached":stats})
    except Exception as e: return jsonify({"emails":[],"count":0,"error":str(e)})

@app.route("/api/accounts/<acc_id>/inbox-stream")
def api_inbox_stream(acc_id):
    limit = request.args.get("limit",50,type=int)
    days = request.args.get("days",7,type=int)
    def generate():
        yield f"data: {json.dumps({'type':'start'})}\n\n"
        try: mail = _account_mgr.get_mail_client(acc_id)
        except Exception as e: yield f"data: {json.dumps({'type':'error','error':str(e)[:200]})}\n\n"; return
        try:
            count = 0
            for msg in mail.stream_inbox(limit=limit, days=days):
                count += 1
                yield f"data: {json.dumps({'type':'email','count':count,'email':msg},ensure_ascii=False)}\n\n"
            yield f"data: {json.dumps({'type':'done','count':count})}\n\n"
        except GeneratorExit: pass
        except Exception as e: yield f"data: {json.dumps({'type':'error','error':str(e)[:200]})}\n\n"
        finally:
            try: mail.disconnect()
            except: pass
    return Response(generate(), mimetype="text/event-stream", headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"})

@app.route("/api/accounts/<acc_id>/message/<msg_id>")
def api_message_body(acc_id, msg_id):
    try:
        mail = _account_mgr.get_mail_client(acc_id)
        try:
            full = mail.fetch_full(msg_id.encode() if isinstance(msg_id,str) else msg_id)
            return jsonify({"ok":True,"message":full})
        finally: mail.disconnect()
    except Exception as e: return jsonify({"ok":False,"error":str(e)})

@app.route("/api/accounts/<acc_id>/mail/<alias_email>")
def api_specific_alias_mail(acc_id, alias_email):
    limit = request.args.get("limit",20,type=int)
    days = request.args.get("days",30,type=int)
    force = request.args.get("force","0")=="1"
    try:
        msgs = _account_mgr.check_alias_mail(acc_id, alias_email, limit=limit, days=days, force=force)
        return jsonify({"emails":msgs,"count":len(msgs),"alias":alias_email})
    except Exception as e: return jsonify({"emails":[],"count":0,"error":str(e)})

@app.route("/api/mail")
def api_mail_by_email():
    email = request.args.get("email","").strip().lower()
    alias = request.args.get("alias","").strip().lower()
    limit = request.args.get("limit",20,type=int)
    days = request.args.get("days",30,type=int)
    if not email: return jsonify({"error":"请提供 email 参数"})
    acc_id = None
    for a in _account_mgr.list_accounts():
        if a.get("icloud_email","").lower()==email or a.get("real_email","").lower()==email: acc_id=a["id"]; break
    if not acc_id: return jsonify({"error":f"未找到邮箱对应的账号: {email}"})
    try:
        if alias:
            msgs = _account_mgr.check_alias_mail(acc_id, alias, limit=limit, days=days)
            return jsonify({"emails":msgs,"count":len(msgs),"alias":alias,"account":email})
        else:
            by_alias = _account_mgr.check_all_aliases_mail(acc_id, limit_per=limit, days=days)
            total = sum(len(v) for v in by_alias.values())
            return jsonify({"by_alias":by_alias,"total":total,"account":email})
    except Exception as e: return jsonify({"error":str(e)})

@app.route("/api/accounts/<acc_id>/alias-mail")
def api_alias_mail(acc_id):
    force = request.args.get("force","0")=="1"
    try:
        by_alias = _account_mgr.check_all_aliases_mail(acc_id, force=force)
        total = sum(len(v) for v in by_alias.values())
        stats = _account_mgr._cache.get_stats(acc_id)
        return jsonify({"by_alias":by_alias,"total":total,"cached":stats})
    except Exception as e: return jsonify({"by_alias":{},"total":0,"error":str(e)})

@app.route("/api/aliases")
def api_aliases():
    try:
        aliases, accounts = _account_mgr.get_all_aliases_with_status(max_workers=5)
        by_account = {}
        for alias in aliases:
            by_account.setdefault(str(alias.get("account_id") or ""), []).append(alias)
        for acc_id, items in by_account.items():
            if acc_id:
                _account_mgr.record_known_aliases(acc_id, items)
        failures = {
            acc_id: status for acc_id, status in accounts.items()
            if not status.get("ok")
        }
        return jsonify({
            "ok": not failures,
            "aliases": aliases,
            "count": len(aliases),
            "accounts": accounts,
            "failures": failures,
        })
    except Exception as e:
        return jsonify({"ok":False,"aliases":[],"count":0,"error":str(e)}), 500

@app.route("/api/pickup-links")
def api_pickup_links():
    """Return opaque pickup URLs; the email address is never embedded in them."""
    try:
        links = _pickup_store.list_all()
        base = PICKUP_BASE_URL or request.host_url.rstrip("/")
        return jsonify({"links": [
            {"account_id": x["account_id"], "email": x["alias_email"], "token": x["token"],
             "url": f"{base}/pickup/{x['token']}", "created_at": x["created_at"]}
            for x in links
        ], "count": len(links)})
    except Exception as e:
        return jsonify({"links": [], "count": 0, "error": str(e)})

@app.route("/api/pickup-links/<acc_id>/<path:alias_email>", methods=["POST"])
def api_ensure_pickup_link(acc_id, alias_email):
    try:
        item = _pickup_store.ensure(acc_id, alias_email)
        base = PICKUP_BASE_URL or request.host_url.rstrip("/")
        return jsonify({"ok": True, "url": f"{base}/pickup/{item['token']}"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400

@app.route("/api/pickup-links/<acc_id>/<path:alias_email>", methods=["DELETE"])
def api_revoke_pickup_link(acc_id, alias_email):
    return jsonify({"ok": _pickup_store.revoke(acc_id, alias_email)})

@app.route("/api/pickup-links/export", methods=["POST"])
def api_export_pickup_links():
    data = request.get_json(silent=True) or {}
    requested = data.get("emails") or []
    if not isinstance(requested, list) or not requested:
        return jsonify({"ok": False, "error": "请先勾选要导出的邮箱"}), 400
    if len(requested) > 5000:
        return jsonify({"ok": False, "error": "单次最多导出 5000 个邮箱"}), 400

    links = _pickup_store.list_all()
    by_email = {item["alias_email"].strip().lower(): item for item in links}
    selected = []
    for email in requested:
        item = by_email.get(str(email or "").strip().lower())
        if item:
            selected.append({"email": item["alias_email"], "account_id": item["account_id"]})
    claimed, skipped = _export_store.claim(selected)
    base = PICKUP_BASE_URL or request.host_url.rstrip("/")
    lines = [
        f"{record['email']}----{base}/pickup/{by_email[record['email']]['token']}"
        for record in claimed
    ]
    return jsonify({
        "ok": True,
        "lines": lines,
        "count": len(lines),
        "skipped": skipped,
        "missing": max(0, len(set(map(str, requested))) - len(selected)),
    })

@app.route("/api/export-history/restore", methods=["POST"])
def api_restore_export_history():
    data = request.get_json(silent=True) or {}
    emails = data.get("emails") or []
    if not isinstance(emails, list) or not emails:
        return jsonify({"ok": False, "error": "请选择要恢复的邮箱"}), 400
    restored = _export_store.restore(emails)
    return jsonify({"ok": True, "restored": restored, "count": len(restored)})

@app.route("/pickup/<token>")
def pickup_page(token):
    """Render immediately; messages are fetched asynchronously by the browser."""
    item = _pickup_store.get_by_token(token)
    if not item:
        return Response("取件链接无效或已撤销", status=404, mimetype="text/plain")
    token_js = json.dumps(token)
    alias_html = _html_escape(item["alias_email"])
    html = """<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>
<title>邮件</title><style>*{box-sizing:border-box}html,body{margin:0;min-height:100%;background:#fff;font-family:system-ui,-apple-system,"Microsoft YaHei",sans-serif}#mailbox{min-height:100vh;background:#fff}.mail-frame{display:block;width:100%;min-height:100vh;border:0;background:#fff}.state{display:flex;align-items:center;justify-content:center;min-height:100vh;color:#718594;background:#f5f8fa}.status{position:fixed;right:12px;top:10px;z-index:10;padding:5px 10px;border-radius:3px;background:rgba(16,26,35,.82);color:#fff;font-size:12px;opacity:0;transition:opacity .2s;pointer-events:none}.status.show{opacity:1}</style></head><body>
<div id='status' class='status'>正在获取最新邮件...</div><main id='mailbox'><div class='state'>正在打开最新邮件...</div></main>
<script>const token=__TOKEN__;let busy=false;let currentId='';const bodies={};const statusEl=document.getElementById('status');function status(text,hold){statusEl.textContent=text;statusEl.classList.add('show');if(!hold)setTimeout(()=>statusEl.classList.remove('show'),1800)}function showEmail(data){const box=document.getElementById('mailbox');box.innerHTML='';if(data.html){const frame=document.createElement('iframe');frame.className='mail-frame';frame.setAttribute('sandbox','allow-same-origin allow-popups');frame.setAttribute('referrerpolicy','no-referrer');const csp=`<meta name="referrer" content="no-referrer"><meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src data: cid:; style-src 'unsafe-inline'; font-src data:;">`;frame.srcdoc=csp+data.html;frame.onload=function(){try{frame.style.height=Math.max(frame.contentDocument.documentElement.scrollHeight+20,window.innerHeight)+'px'}catch(e){}};box.appendChild(frame)}else{const div=document.createElement('div');div.className='state';div.style.whiteSpace='pre-wrap';div.style.alignItems='flex-start';div.style.justifyContent='flex-start';div.style.padding='32px';div.textContent=data.body||'(无正文内容)';box.appendChild(div)}}async function fetchMessages(force){const r=await fetch('/pickup/'+encodeURIComponent(token)+'/messages'+(force?'?force=1':''),{cache:'no-store'});const d=await r.json();if(!r.ok||d.error)throw new Error(d.error||'读取失败');return d}async function openLatest(m){if(!m||!m.id){document.getElementById('mailbox').innerHTML='<div class="state">暂无邮件。</div>';return}if(currentId===String(m.id)&&bodies[m.id])return;currentId=String(m.id);status('正在打开最新邮件...',true);for(let n=0;n<25;n++){try{const r=await fetch('/pickup/'+encodeURIComponent(token)+'/message/'+encodeURIComponent(m.id),{cache:'no-store'});const d=await r.json();if(r.ok&&d.ready){bodies[m.id]=d.message||{};showEmail(bodies[m.id]);status('最新邮件已打开',false);return}}catch(e){}await new Promise(r=>setTimeout(r,800))}status('邮件打开较慢，稍后自动重试',false)}async function sync(){if(busy)return;busy=true;try{status('正在获取最新邮件...',true);let d=await fetchMessages(true);let list=(d.emails||[]).slice().reverse();if(list.length)await openLatest(list[0]);if(d.refreshing){for(let i=0;i<25;i++){await new Promise(r=>setTimeout(r,800));d=await fetchMessages(false);if(!d.refreshing){list=(d.emails||[]).slice().reverse();await openLatest(list[0]);break}}}if(!list.length)document.getElementById('mailbox').innerHTML='<div class="state">暂无邮件。</div>';status('已是最新邮件',false)}catch(e){status('读取失败，稍后自动重试',false)}finally{busy=false}}sync();(function schedule(){setTimeout(async function(){await sync();schedule()},4000+Math.random()*2000)})();</script></body></html>"""
    html = (
        html.replace("__ALIAS__", alias_html)
        .replace("__TOKEN__", token_js)
        .replace(
            "sync();(function schedule(){setTimeout(async function(){await sync();schedule()},4000+Math.random()*2000)})();",
            "let idlePolls=0;async function scheduledSync(){const before=currentId;await sync();idlePolls=currentId&&currentId!==before?0:Math.min(idlePolls+1,5)}scheduledSync();(function schedule(){const base=document.hidden?15000:Math.min(3000+idlePolls*1800,12000);setTimeout(async function(){await scheduledSync();schedule()},base+Math.random()*1500)})();document.addEventListener('visibilitychange',function(){if(!document.hidden)scheduledSync()});window.addEventListener('pageshow',function(e){if(e.persisted)scheduledSync()});",
        )
        .replace("4000+Math.random()*2000", "2500+Math.random()*1500")
    )
    return Response(html, mimetype="text/html", headers={"Cache-Control": "no-store"})

def _prepare_pickup_message(message):
    prepared = dict(message or {})
    prepared["verification_code"] = _pickup_code(
        prepared.get("subject", ""), prepared.get("body", "")
    )
    prepared["clean_body"] = _clean_pickup_body(prepared.get("body", ""))
    return prepared


def _store_pickup_body(account_id, msg_id, message):
    prepared = _prepare_pickup_message(message)
    with _pickup_refresh_lock:
        if account_id in _removed_account_ids:
            return prepared
    _pickup_body_store.put(account_id, str(msg_id), prepared)
    with _pickup_refresh_lock:
        _cache_pickup_body_locked((account_id, str(msg_id)), prepared)
    return prepared


def _refresh_pickup_account(account_id):
    global _pickup_pending
    try:
        with _pickup_refresh_lock:
            if account_id in _removed_account_ids:
                return
        # Pickup links already contain the alias mapping. Avoid the iCloud HME
        # API here so an expired browser cookie cannot block IMAP delivery.
        links = _pickup_store.list_for_account(account_id)
        aliases = [item.get("alias_email", "") for item in links]
        synced = _account_mgr.sync_pickup_mail(account_id, aliases, scan_limit=100, days=30)
        with _pickup_refresh_lock:
            removed = account_id in _removed_account_ids
        if removed:
            _account_mgr._cache.clear_account(account_id)
            _pickup_body_store.delete_account(account_id)
            return
        for msg_id, message in synced.get("bodies", {}).items():
            _store_pickup_body(account_id, msg_id, message)

        # Warm the latest body for each alias once. This makes the first page
        # open fast even after the service has restarted.
        warm_targets = []
        for alias in aliases:
            if len(warm_targets) >= 8:
                break
            messages = _account_mgr._cache.get_alias_mail(account_id, alias)
            if not messages:
                continue
            latest = max(
                messages,
                key=lambda item: int(str(item.get("id", "0")))
                if str(item.get("id", "")).isdigit() else 0,
            )
            msg_id = str(latest.get("id", ""))
            if not msg_id or _pickup_body_store.contains(account_id, msg_id):
                continue
            key = (account_id, msg_id)
            with _pickup_refresh_lock:
                if key in _pickup_body_cache or key in _pickup_body_refreshing:
                    continue
                _pickup_body_refreshing.add(key)
            warm_targets.append((key, latest))

        for key, header in warm_targets:
            try:
                full = _account_mgr.fetch_pickup_message(account_id, key[1])
                if full:
                    full.update(header)
                    _store_pickup_body(account_id, key[1], full)
            except Exception:
                pass
            finally:
                with _pickup_refresh_lock:
                    _pickup_body_refreshing.discard(key)

        with _pickup_refresh_lock:
            _pickup_refresh_errors.pop(account_id, None)
            _pickup_error_log_state.pop(account_id, None)
    except Exception as e:
        now = time.time()
        error_text = str(e)[:160]
        with _pickup_refresh_lock:
            _pickup_refresh_errors[account_id] = error_text
            previous = _pickup_error_log_state.get(account_id)
            should_log = not previous or previous[0] != error_text or now - previous[1] >= 60
            if should_log:
                _pickup_error_log_state[account_id] = (error_text, now)
        if should_log:
            _emit_log("warn", f"取件同步失败 [{account_id}]: {error_text[:100]}")
    finally:
        with _pickup_refresh_lock:
            _pickup_refreshing_accounts.discard(account_id)
            _pickup_pending = max(0, _pickup_pending - 1)


def _schedule_pickup_account_refresh(account_id):
    global _pickup_pending
    now = time.time()
    with _pickup_refresh_lock:
        if account_id in _removed_account_ids:
            return False
        if account_id in _pickup_refreshing_accounts:
            return True
        if now - _pickup_last_account_refresh.get(account_id, 0) < _PICKUP_SYNC_INTERVAL_SECONDS:
            return False
        if _pickup_pending >= _PICKUP_MAX_PENDING:
            return False
        _pickup_refreshing_accounts.add(account_id)
        _pickup_last_account_refresh[account_id] = now
        _pickup_pending += 1
    try:
        _pickup_executor.submit(_refresh_pickup_account, account_id)
        return True
    except RuntimeError:
        with _pickup_refresh_lock:
            _pickup_refreshing_accounts.discard(account_id)
            _pickup_pending = max(0, _pickup_pending - 1)
        return False


def _pickup_sync_loop():
    while not _shutdown_event.is_set():
        account_ids = {
            item.get("account_id")
            for item in _pickup_store.list_all()
            if item.get("account_id")
        }
        for account_id in account_ids:
            _schedule_pickup_account_refresh(account_id)
        _shutdown_event.wait(min(0.5, _PICKUP_SYNC_INTERVAL_SECONDS / 2))

def _pickup_code(subject, body):
    text = f"{subject}\n{body}"
    patterns = [
        r"(?:验证码|校验码|动态码|临时码|verification\s*code|security\s*code|passcode|otp)[^0-9]{0,40}([0-9]{4,8})",
        r"(?<![0-9])([0-9]{6})(?![0-9])",
        r"(?<![0-9])([0-9]{4,8})(?![0-9])",
    ]
    for pattern in patterns:
        for match in re.findall(pattern, text, flags=re.IGNORECASE):
            if not (len(match) == 4 and 1900 <= int(match) <= 2099):
                return match
    return ""

def _clean_pickup_body(body):
    body = re.sub(r"https?://\S+", "", body or "", flags=re.IGNORECASE)
    body = re.sub(r"[ \t]+", " ", body)
    body = re.sub(r"\n\s*\n\s*\n+", "\n\n", body)
    return body.strip()[:2500]

def _cache_pickup_body_locked(key, message):
    global _pickup_body_cache_bytes
    old = _pickup_body_cache.pop(key, None)
    if old:
        _pickup_body_cache_bytes -= old[0]
    size = len(message.get("body", "").encode("utf-8", errors="ignore"))
    size += len(message.get("html", "").encode("utf-8", errors="ignore"))
    _pickup_body_cache[key] = (size, message)
    _pickup_body_cache_bytes += size
    while _pickup_body_cache and (
        len(_pickup_body_cache) > _PICKUP_BODY_MAX_ITEMS
        or _pickup_body_cache_bytes > _PICKUP_BODY_MAX_BYTES
    ):
        _, (removed_size, _) = _pickup_body_cache.popitem(last=False)
        _pickup_body_cache_bytes -= removed_size

def _get_pickup_body_locked(key):
    cached = _pickup_body_cache.pop(key, None)
    if not cached:
        return None
    _pickup_body_cache[key] = cached
    return cached[1]

def _refresh_pickup_body(account_id, msg_id):
    global _pickup_pending
    key = (account_id, msg_id)
    try:
        full = _account_mgr.fetch_pickup_message(account_id, msg_id)
        if not full:
            raise RuntimeError("邮件正文为空")
        _store_pickup_body(account_id, msg_id, full)
        with _pickup_refresh_lock:
            _pickup_refresh_errors.pop(account_id, None)
    except Exception as e:
        with _pickup_refresh_lock:
            _pickup_refresh_errors[account_id] = str(e)[:160]
        _emit_log("warn", f"取件正文读取失败 [{account_id}]: {str(e)[:100]}")
    finally:
        with _pickup_refresh_lock:
            _pickup_body_refreshing.discard(key)
            _pickup_pending = max(0, _pickup_pending - 1)

@app.route("/pickup/<token>/messages")
def pickup_messages(token):
    item = _pickup_store.get_by_token(token)
    if not item:
        return jsonify({"error": "取件链接无效或已撤销"}), 404
    cached = _account_mgr._cache.get_alias_mail(item["account_id"], item["alias_email"])
    cached = sorted(
        cached,
        key=lambda message: int(str(message.get("id", "0")))
        if str(message.get("id", "")).isdigit() else 0,
    )[-20:]
    account_id = item["account_id"]
    _schedule_pickup_account_refresh(account_id)
    with _pickup_refresh_lock:
        refreshing = account_id in _pickup_refreshing_accounts
        refresh_error = _pickup_refresh_errors.get(account_id)
    public_error = "邮件同步暂时失败" if refresh_error and not cached else None
    warning = "邮件同步暂时失败，当前显示缓存内容" if refresh_error and cached else None
    return jsonify({"emails": cached, "count": len(cached), "refreshing": refreshing, "error": public_error, "warning": warning}), 200, {"Cache-Control": "no-store"}

@app.route("/pickup/<token>/message/<msg_id>")
def pickup_message(token, msg_id):
    global _pickup_pending
    item = _pickup_store.get_by_token(token)
    if not item:
        return jsonify({"error": "取件链接无效或已撤销"}), 404
    account_id = item["account_id"]
    allowed = _account_mgr._cache.get_alias_mail(account_id, item["alias_email"])
    if msg_id not in {str(m.get("id", "")) for m in allowed}:
        return jsonify({"error": "邮件不存在"}), 404
    key = (account_id, msg_id)
    with _pickup_refresh_lock:
        cached_body = _get_pickup_body_locked(key)
        if cached_body is not None:
            return jsonify({"ready": True, "message": cached_body}), 200, {"Cache-Control": "no-store"}

    persisted_body = _pickup_body_store.get(account_id, msg_id)
    if persisted_body is not None:
        with _pickup_refresh_lock:
            _cache_pickup_body_locked(key, persisted_body)
        return jsonify({"ready": True, "message": persisted_body}), 200, {"Cache-Control": "no-store"}

    with _pickup_refresh_lock:
        refreshing = key in _pickup_body_refreshing
        if not refreshing and _pickup_pending < _PICKUP_MAX_PENDING:
            _pickup_body_refreshing.add(key)
            _pickup_pending += 1
            refreshing = True
            _pickup_executor.submit(_refresh_pickup_body, account_id, msg_id)
    return jsonify({"ready": False, "refreshing": refreshing}), 202, {"Cache-Control": "no-store"}

@app.route("/api/emails")
def api_emails():
    limit = request.args.get("limit",0,type=int)
    emails = []
    pickup_by_email = {
        item.get("alias_email", "").strip().lower(): item
        for item in _pickup_store.list_all()
    }
    pickup_created_at = {
        email: item.get("created_at", "")
        for email, item in pickup_by_email.items()
    }
    f = RESULTS_DIR / "latest_emails.txt"
    if f.exists():
        lines = f.read_text(encoding="utf-8").strip().split("\n")
        if limit>0 and len(lines)>limit: lines = lines[-limit:]
        for line in lines:
            line = line.strip()
            if line and "@" in line:
                parts = line.split("\t")
                email = parts[0]
                created_at = parts[2] if len(parts) > 2 else pickup_created_at.get(email.lower(), "")
                emails.append({
                    "email": email,
                    "account_id": parts[1] if len(parts) > 1 else "",
                    "created_at": created_at,
                })
    emails.reverse()
    history = _export_store.status_map(item["email"] for item in emails)
    exported_count = 0
    base = PICKUP_BASE_URL or request.host_url.rstrip("/")
    for item in emails:
        item["pickup_url"] = ""
        pickup = pickup_by_email.get(item["email"].strip().lower())
        if pickup:
            item["account_id"] = pickup["account_id"]
            item["pickup_url"] = f"{base}/pickup/{pickup['token']}"
        elif item["account_id"] in _account_mgr.accounts and item["email"]:
            pickup = _pickup_store.ensure(item["account_id"], item["email"])
            item["pickup_url"] = f"{base}/pickup/{pickup['token']}"
        record = history.get(item["email"].strip().lower())
        item["exported"] = bool(record)
        item["exported_at"] = record.get("exported_at", "") if record else ""
        if record:
            exported_count += 1
    return jsonify({
        "emails": emails,
        "count": len(emails),
        "exported_count": exported_count,
        "unexported_count": len(emails) - exported_count,
    })

@app.route("/api/scheduler/start", methods=["POST"])
def api_scheduler_start():
    global _scheduler_thread
    with _scheduler_lock:
        if _scheduler_thread and _scheduler_thread.is_alive():
            _save_scheduler_enabled(True)
            return jsonify({"ok":True,"already_running":True})
        _scheduler_stop_event.clear()
        _scheduler_thread = threading.Thread(target=_scheduler_loop, daemon=True)
        _scheduler_thread.start()
        _update_state(running=True)
        _save_scheduler_enabled(True)
        return jsonify({"ok":True,"already_running":False})

@app.route("/api/scheduler/stop", methods=["POST"])
def api_scheduler_stop():
    _scheduler_stop_event.set()
    _update_state(running=False, creating=False, next_trigger=None, round_status="正在停止")
    _save_scheduler_enabled(False)
    return jsonify({"ok":True})

@app.route("/api/logs")
def api_logs():
    after = request.args.get("after", 0, type=int) or 0
    with _log_condition:
        entries = [dict(entry) for entry in _log_entries if entry["seq"] > after]
        seq = _log_seq
    return jsonify({"ok": True, "seq": seq, "logs": entries[-500:]})

@app.route("/api/log-stream")
def api_log_stream():
    start_cursor = request.args.get("after", 0, type=int) or 0
    last_event_id = request.headers.get("Last-Event-ID", "")
    if last_event_id.isdigit():
        start_cursor = max(start_cursor, int(last_event_id))
    def generate():
        cursor = start_cursor
        yield "retry: 3000\n: connected\n\n"
        while True:
            with _log_condition:
                _log_condition.wait_for(
                    lambda: _log_seq > cursor or _shutdown_event.is_set(), timeout=15
                )
                entries = [entry for entry in _log_entries if entry["seq"] > cursor]
            if _shutdown_event.is_set():
                return
            if entries:
                for entry in entries:
                    cursor = entry["seq"]
                    yield f"id: {cursor}\ndata: {json.dumps(entry,ensure_ascii=False)}\n\n"
                continue
            yield ": heartbeat\n\n"
    return Response(generate(), mimetype="text/event-stream", headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"})

def main():
    import argparse, os, signal as _signal
    parser = argparse.ArgumentParser(description="iCloud HME Web UI")
    parser.add_argument("--port",type=int,default=int(os.environ.get("PORT",5050)))
    parser.add_argument("--host",type=str,default=os.environ.get("HOST","127.0.0.1"))
    parser.add_argument("--scheduler",action="store_true",help="启动时自动运行调度器")
    parser.add_argument("--no-sync",action="store_true",help="跳过时间校准")
    args = parser.parse_args()
    if _public_bind_blocked(args.host, ADMIN_ACCESS_TOKEN):
        print("[!] Refusing to bind", args.host, "without ADMIN_ACCESS_TOKEN")
        print("    Use --host 127.0.0.1, or set ADMIN_ACCESS_TOKEN first")
        raise SystemExit(2)
    if not args.no_sync:
        offset = _sync_time()
        if abs(offset)>0.5: print(f"[*] Time sync: offset {offset:.1f}s")
    threading.Thread(target=_health_loop, daemon=True).start()
    accounts = _account_mgr.list_accounts()
    if accounts:
        print(f"[+] {len(accounts)} account(s) loaded")
        for a in accounts: print(f"    [OK] {a.get('name','?')} - {a.get('real_email','?')} ({a.get('alias_total',0)} aliases)")
    else: print("[*] No accounts yet")
    _emit_log("info", f"服务已启动，加载 {len(accounts)} 个主账号")
    migrated = sum(_DATA_MIGRATION_STATS.values())
    if migrated:
        _emit_log(
            "info",
            "旧账号数据迁移完成: "
            + ", ".join(
                f"{key}={value}"
                for key, value in _DATA_MIGRATION_STATS.items()
                if value
            ),
        )
    threading.Thread(
        target=_pickup_sync_loop, daemon=True, name="pickup-sync"
    ).start()
    _emit_log(
        "info",
        f"取件后台同步已启动，账号级间隔 {_PICKUP_SYNC_INTERVAL_SECONDS:g} 秒",
    )
    _resume_batch_job_if_needed()
    if args.scheduler or _load_scheduler_enabled():
        global _scheduler_thread
        _scheduler_stop_event.clear()
        _scheduler_thread = threading.Thread(target=_scheduler_loop, daemon=True)
        _scheduler_thread.start()
        _update_state(running=True)
        _save_scheduler_enabled(True)
        print("[+] Scheduler auto-started")
    def _shutdown(sig,frame):
        print("\n[*] Shutting down...")
        _scheduler_stop_event.set()
        _shutdown_event.set()
        _pickup_executor.shutdown(wait=False, cancel_futures=True)
        os._exit(0)
    _signal.signal(_signal.SIGINT, _shutdown)
    _signal.signal(_signal.SIGTERM, _shutdown)
    try:
        from waitress import serve
        print(f"\n  Production → http://{args.host}:{args.port}\n")
        serve(app, host=args.host, port=args.port, threads=96, connection_limit=2000)
    except ImportError:
        print(f"\n  Dev server → http://{args.host}:{args.port}\n")
        app.run(host=args.host, port=args.port, debug=False, threaded=True)

if __name__=="__main__": main()
