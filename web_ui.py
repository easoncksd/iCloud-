#!/usr/bin/env python3
"""iCloud HME Web UI — 多账号聚合管理平台 — Flask single-page app."""
import sys, os, json, time, queue, secrets, threading, re, hashlib
from collections import OrderedDict, deque
from concurrent.futures import ThreadPoolExecutor, as_completed
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
    1.0, float(os.environ.get("PICKUP_SYNC_INTERVAL_SECONDS", "2"))
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
    1, min(20, int(os.environ.get("BATCH_MAX_ACCOUNT_WORKERS", "5")))
)

_TEMPORARY_CREATE_LIMIT_MARKERS = (
    "right now",
    "try again later",
    "rate limit",
    "too many requests",
    "429",
    "temporarily",
    "throttle",
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
_manual_create_lock = threading.Lock()
_manual_creating_accounts = set()


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
    """后台调度器：北京时间 7:00-20:00，随机间隔 60-90min，每账号随机 3-5 个。"""
    import random as _random
    from icloud_hme import ICloudHME
    _update_state(running=True, round_status="等待触发窗口")
    _emit_log("info", "调度器已启动 (BJ 7-20h, 间隔 60-90min, 每轮 3-5 个)")
    def _bj_hour() -> int: return _now().hour
    while not _scheduler_stop_event.is_set() and not _shutdown_event.is_set():
        h = _bj_hour()
        if h < 7 or h >= 20: _update_state(round_status=f"非窗口时段 (BJ {h}:00)，等待..."); _scheduler_stop_event.wait(1800); continue
        active_accounts = [a for a in _account_mgr.list_accounts() if a.get("status") == "active"]
        if not active_accounts: _update_state(creating=False, round_status="无活跃账号，跳过"); _scheduler_stop_event.wait(1800); continue
        round_total = 0
        for i, account in enumerate(active_accounts):
            if _scheduler_stop_event.is_set() or _shutdown_event.is_set(): break
            acc_id = account["id"]; acc_name = account.get("name", acc_id)
            target_count = _random.randint(3, 5)
            _emit_log("info", f"[{acc_name}] 本轮目标 {target_count} 个")
            client = ICloudHME(account["cookies"], host=account.get("host","icloud.com"), verbose=False)
            created = 0; errors = 0
            while created < target_count and errors < 3 and not _scheduler_stop_event.is_set() and not _shutdown_event.is_set():
                try:
                    result = client.create_alias(label=f"{acc_name} {_now().strftime('%m%d%H%M')}", max_retries=2)
                    email = result.get("email","")
                    if email:
                        created += 1; round_total += 1
                        _emit_log("success", f"[{acc_name}] ({created}/{target_count}) {email}")
                        _increment_state(today_created=1, total_created=1)
                        with open(str(RESULTS_DIR/"latest_emails.txt"),"a",encoding="utf-8") as f: f.write(f"{email}\t{acc_id}\n")
                        _account_mgr.update_account(acc_id, alias_total=account.get("alias_total",0)+1)
                        account["alias_total"] = account.get("alias_total",0)+1
                        errors = 0; time.sleep(_random.uniform(15,45))
                    else: errors += 1
                except Exception as e:
                    err_str = str(e)
                    if _is_limit_error(err_str): _emit_log("info",f"[{acc_name}] 触达上限: {err_str[:60]}"); break
                    errors += 1; _emit_log("warn",f"[{acc_name}] 失败: {err_str[:80]}")
            if i < len(active_accounts)-1: time.sleep(_random.uniform(120,300))
        _update_state(creating=False, current_round_created=round_total, round_status=f"本轮创建 {round_total} 个")
        interval_sec = _random.randint(3600,5400)
        target = _now() + timedelta(seconds=interval_sec)
        _update_state(next_trigger=target.timestamp())
        _emit_log("info", f"下轮 {target.strftime('%H:%M')} (间隔 {interval_sec//60}min)")
        _scheduler_stop_event.wait(interval_sec)
    _update_state(running=False, next_trigger=None, round_status="已停止")
    _emit_log("info", "调度器已停止")

def _health_loop():
    _error_reported = set()
    while not _shutdown_event.is_set():
        if _shutdown_event.wait(300): break
        for account in _account_mgr.list_accounts():
            if account.get("status") != "active": continue
            try: _account_mgr.validate_account(account["id"]); _error_reported.discard(account["id"])
            except Exception as e:
                if account["id"] not in _error_reported: _emit_log("warn",f"健康检查失败 [{account.get('name','?')}]: {str(e)[:100]}"); _error_reported.add(account["id"])

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
.sidebar-foot{margin-top:auto;padding:12px;border-top:1px solid var(--line);color:var(--muted);font-size:12px}
.main{flex:1;min-width:0;display:flex;flex-direction:column}
.topbar{display:flex;align-items:center;justify-content:space-between;gap:16px;padding:18px 28px 12px;border-bottom:1px solid var(--line);background:var(--bg);position:sticky;top:0;z-index:5}
.topbar h1{font-size:22px;font-weight:700;letter-spacing:0}
.topbar-actions{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
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
.panel-header{display:flex;justify-content:space-between;align-items:center;padding:14px 16px;border-bottom:1px solid var(--line);font-weight:600;flex-wrap:wrap;gap:10px}
.panel-header>div{display:flex;flex-wrap:wrap;gap:8px;align-items:center}
.panel-body{padding:0}#aliasTableContainer{overflow-x:auto}
.inbox-tools{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.email-table{width:100%;min-width:920px;border-collapse:collapse}
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
}
@media (prefers-reduced-motion:reduce){
  .is-enter,.modal-overlay,.modal-box{animation:none}
  .btn,.acc-card,.work-stat,.nav-item,.copy-toast,.progress-bar .fill,.email-table td,.segmented button{transition:none}
  .btn:active,.acc-card:hover,.work-stat:hover{transform:none}
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
  .inbox-tools select,.inbox-tools input[type=text]{flex:1 1 220px;width:auto!important;min-width:0}
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
  <a class="nav-item" data-tab="accounts"><svg class="nav-ico" viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" d="M12 12a4 4 0 1 0-4-4 4 4 0 0 0 4 4zm0 2c-3.4 0-8 1.7-8 5v1h16v-1c0-3.3-4.6-5-8-5z"/></svg>账号</a>
  <a class="nav-item active" data-tab="emails"><svg class="nav-ico" viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" d="M20 4H4a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2V6a2 2 0 0 0-2-2zm0 4-8 5L4 8V6l8 5 8-5z"/></svg>邮箱</a>
  <a class="nav-item" data-tab="inbox"><svg class="nav-ico" viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" d="M19 3H5a2 2 0 0 0-2 2v3h6l1 2h4l1-2h6V5a2 2 0 0 0-2-2zm3 7h-6.4l-1 2H9.4l-1-2H2v9a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2z"/></svg>收件箱</a>
  <a class="nav-item" data-tab="settings"><svg class="nav-ico" viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" d="M19.4 13a7.7 7.7 0 0 0 .1-1 7.7 7.7 0 0 0-.1-1l2.1-1.6-2-3.5-2.5 1a7 7 0 0 0-1.7-1L14.8 2h-4l-.5 2.9A7 7 0 0 0 8.6 6L6.1 5l-2 3.5L6.2 10a7.7 7.7 0 0 0-.1 1 7.7 7.7 0 0 0 .1 1L4.1 13.6l2 3.5 2.5-1a7 7 0 0 0 1.7 1l.5 2.9h4l.5-2.9a7 7 0 0 0 1.7-1l2.5 1 2-3.5zM12 15.5A3.5 3.5 0 1 1 15.5 12 3.5 3.5 0 0 1 12 15.5z"/></svg>设置</a>
  <a class="nav-item" data-tab="batch" style="display:none">任务</a>
  <a class="nav-item" data-tab="logs" style="display:none">日志</a>
  <div class="side-overview" id="sideOverview">
    <div class="side-kicker">概况</div>
    <div class="side-stat"><span>账号</span><b id="sideStatAccounts">0</b></div>
    <div class="side-stat"><span>邮箱</span><b id="sideStatEmails">0</b></div>
    <div class="side-stat"><span>可收信</span><b id="sideStatReady">0</b></div>
    <div class="side-task"><span class="status-dot" id="sideTaskDot"></span><span id="sideTaskText">任务空闲</span></div>
  </div>
  <div class="sidebar-foot">
    <div>先加账号，再创建邮箱，然后看信或导出。</div>
  </div>
</aside>
<main class="main">
  <div class="topbar">
    <h1 id="tabTitle">邮箱</h1>
    <div class="topbar-actions">
      <div class="task-pill" id="taskPill"><span class="status-dot" id="schedDot"></span><span id="schedLabel">任务空闲</span></div>
      <button class="btn btn-outline btn-sm" onclick="refreshAll()">刷新</button>
      <button class="btn btn-primary btn-sm" id="topPrimaryBtn" onclick="handlePrimaryAction()">添加账号</button>
    </div>
  </div>
  <div class="page">
    <div id="emptyState" class="hero-empty" style="display:none">
      <h2 id="emptyTitle">先添加账号</h2>
      <p id="emptyText">导入 Cookie 后，就可以创建隐私邮箱。</p>
      <button class="btn btn-primary" id="emptyActionBtn" onclick="handlePrimaryAction()">添加账号</button>
    </div>
    <div id="view-accounts" style="display:none">
      <div class="work-strip" id="accStrip">
        <div class="work-stat"><span>账号</span><b id="accStripAccounts">0</b></div>
        <div class="work-stat"><span>邮箱</span><b id="accStripEmails">0</b></div>
        <div class="work-stat"><span>今日新建</span><b id="accStripToday">0</b></div>
        <div class="work-stat"><span>当前任务</span><b id="accStripTask">空闲</b></div>
      </div>
      <div class="toolbar">
        <button class="btn btn-primary" onclick="showAddAccountModal()">添加账号</button>
      </div>
      <div class="account-grid" id="accCards"></div>
    </div>
    <div id="view-emails">
      <div class="panel">
        <div class="panel-header">
          <span>邮箱列表</span>
          <div>
            <span class="hint" id="emailCount">0</span>
            <button class="btn btn-outline btn-sm" onclick="refreshEmails().then(renderAliasTable)">刷新</button>
            <button class="btn btn-outline btn-sm" id="btnAliasSync" onclick="refreshAliases()" title="从云端同步标签和状态">云端同步</button>
            <button class="btn btn-outline btn-sm" onclick="copyAll()">复制全部</button>
            <button class="btn btn-outline btn-sm" onclick="exportCSV()">CSV</button>
            <button class="btn btn-primary btn-sm" onclick="exportSelectedPickupTxt()">导出已选 TXT</button>
            <button class="btn btn-primary btn-sm" onclick="showCreateDrawer()">创建邮箱</button>
          </div>
        </div>
        <div class="filter-bar">
          <span class="hint">筛选账号:</span>
          <select id="aliasFilter" onchange="aliasPage=1;renderAliasTable()"><option value="all">全部账号</option></select>
          <div class="segmented" aria-label="导出状态筛选">
            <button type="button" class="active" data-export-filter="unexported" onclick="setExportFilter('unexported')" id="exportCountUnexported">未导出 0</button>
            <button type="button" data-export-filter="exported" onclick="setExportFilter('exported')" id="exportCountExported">已导出 0</button>
            <button type="button" data-export-filter="all" onclick="setExportFilter('all')" id="exportCountAll">全部 0</button>
          </div>
          <div class="pager" id="aliasPager">
            <span class="hint">每页</span>
            <div class="segmented" aria-label="每页数量">
              <button type="button" data-page-size="20" onclick="setAliasPageSize(20)">20</button>
              <button type="button" data-page-size="50" class="active" onclick="setAliasPageSize(50)">50</button>
            </div>
            <span class="hint" id="aliasPageInfo"></span>
            <button class="btn btn-outline btn-sm" id="btnAliasPrev" onclick="setAliasPage(aliasPage-1)">上一页</button>
            <button class="btn btn-outline btn-sm" id="btnAliasNext" onclick="setAliasPage(aliasPage+1)">下一页</button>
          </div>
        </div>
        <div class="panel-body">
          <div id="aliasTableContainer" class="empty"><div class="icon"></div>还没有邮箱。请先添加账号，再点击「创建邮箱」。</div>
        </div>
      </div>
    </div>
    <div id="view-inbox" style="display:none">
      <div class="panel">
        <div class="panel-header">
          <span>收件箱</span>
          <div class="inbox-tools">
            <select id="inboxAccount" onchange="refreshInbox()"></select>
            <input type="number" id="inboxLimit" value="20" min="1" max="100" title="邮件数量">
            <input type="text" id="aliasSearchInput" placeholder="输入邮箱地址查件..." title="输入隐私邮箱地址查件">
            <button class="btn btn-outline btn-sm" id="btnInboxRefresh" onclick="refreshInbox()">刷新</button>
            <button class="btn btn-outline btn-sm" id="btnInboxForce" onclick="refreshInbox(true)" title="跳过缓存重新拉取">强制刷新</button>
            <button class="btn btn-outline btn-sm" id="btnInboxSearch" onclick="searchAliasMail()" title="查询指定邮箱的收件">查件</button>
            <button class="btn btn-outline btn-sm" id="btnInboxAll" onclick="checkAliasMail()" title="检查所有隐私邮箱的收件">全部</button>
            <button class="btn btn-outline btn-sm" id="btnInboxSettings" onclick="openInboxSettings()" title="设置收信密码">设置收信密码</button>
            <span class="hint" id="cacheStatus"></span>
          </div>
        </div>
        <div class="panel-body">
          <div id="inboxMsgs" class="empty"><div class="icon"></div><div class="empty-title">收件前先完成设置</div><ol class="empty-steps"><li>在「账号」里添加 Apple 账号</li><li>为账号设置收信密码</li><li>选择账号后即可看信</li></ol><button class="btn btn-primary" onclick="openInboxSettings()">开始设置</button></div>
        </div>
      </div>
    </div>
    <div id="view-settings" style="display:none">
      <div class="panel">
        <div class="settings-section">
          <h3>自动创建</h3>
          <p>每小时自动给未满的账号创建邮箱。</p>
          <div class="toolbar">
            <span class="status-dot" id="schedDotSettings"></span>
            <span id="schedLabelSettings">已停止</span>
            <button class="btn btn-sm" id="btnSched" onclick="toggleScheduler()">启动自动创建</button>
          </div>
        </div>
        <div class="settings-section">
          <h3>创建邮箱</h3>
          <p>不同主账号最多 5 个并行；同一账号仍逐个创建，触发 Apple 临时限制时每次等待 1 分钟后自动续建。</p>
          <div id="batchAccCount" class="hint">0 个可用账号</div>
          <div class="chk-group" id="batchChkGroup"></div>
          <div class="toolbar">
            <label>每账号数量</label>
            <input type="number" id="batchCount" value="5" min="1" max="750">
            <label>标签</label>
            <input type="text" id="batchLabel" placeholder="可选">
            <button class="btn btn-primary" id="btnBatchExec" onclick="execBatchCreate()">开始创建</button>
          </div>
          <div id="batchProgress"></div>
        </div>
        <div class="settings-section">
          <h3>任务与日志</h3>
          <p>创建、同步和限制解除都会出现在这里。</p>
          <button class="btn btn-outline btn-sm" onclick="clearLogs()">清屏</button>
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
<script>var E=function(id){return document.getElementById(id)};
var state={running:false,creating:false,round_status:'',total_created:0,today_created:0,current_round_created:0,next_trigger:null};
var accounts=[],emails=[],logs=[],logCursor=0;
var curTab='emails',sseConn=null;
var pickupLinksByEmail={};var pickupLinksLoaded=false;var pickupSelected={};var exportFilter='unexported';var aliasPage=1;var aliasPageSize=50;
var batchJob=null;var batchPollTimer=null;
var _refreshBusy=false;var _createBusyByAccount={};var _aliasesBusy=false;
var _inboxBusy=false;var _inboxSse=null;var _inboxStreamMsgs=[];
var _inboxRequestSeq=0;var _inboxRenderedAccount='';var _expandedEmail=null;
document.querySelectorAll('.nav-item').forEach(function(el){
  el.addEventListener('click',function(){showTab(this.dataset.tab);});
});
function showView(id,on){var el=E(id);if(!el)return;var shown=el.style.display==='block';if(on){el.style.display='block';if(!shown){el.classList.remove('is-enter');void el.offsetWidth;el.classList.add('is-enter');}}else{el.style.display='none';el.classList.remove('is-enter');}}
function showTab(tab){
  if(tab==='batch'||tab==='logs'||tab==='dashboard'||tab==='docs')tab='settings';
  curTab=tab;
  document.querySelectorAll('.nav-item').forEach(function(n){n.classList.toggle('active',n.dataset.tab===curTab);});
  var titles={accounts:'账号',emails:'邮箱',inbox:'收件箱',settings:'设置'};
  E('tabTitle').textContent=titles[curTab]||curTab;
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
  if(curTab==='inbox')updateInboxAccountSelect();
}
function handlePrimaryAction(){
  if(!accounts.length){showAddAccountModal();return;}
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
    E('emptyTitle').textContent='先添加账号';
    E('emptyText').textContent='导入 Cookie 后，就可以创建隐私邮箱。';
    E('emptyActionBtn').textContent='添加账号';
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
  if(!accounts.length){btn.textContent='添加账号';btn.onclick=showAddAccountModal;}
  else {btn.textContent='创建邮箱';btn.onclick=showCreateDrawer;}
}
async function api(path,opts){var timeout=(opts||{}).timeout||60000;if(opts)delete opts.timeout;var ctrl=new AbortController();var t=setTimeout(function(){ctrl.abort()},timeout);try{var r=await fetch(path,Object.assign({signal:ctrl.signal},opts||{}));clearTimeout(t);return r.json();}catch(e){clearTimeout(t);var msg=(e.name==='AbortError')?('请求超时 ('+(timeout/1000)+'s)'):(e.message||'网络错误');return{ok:false,error:msg};}}
async function apiSlow(path,opts){return api(path,Object.assign({timeout:60000},opts||{}));}
async function refreshAll(){if(_refreshBusy)return;_refreshBusy=true;try{var _a=api('/api/accounts'),_s=api('/api/state');var a=await _a,s=await _s;accounts=a.accounts||[];state=s;renderSidebar();renderDashboard();updateEmptyState();if(curTab==='emails'){await refreshEmails();renderAliasTable();}if(curTab==='settings')renderBatchPanel();updateInboxAccountSelect();}finally{_refreshBusy=false;}}
async function refreshLight(){if(_refreshBusy)return;var s=await api('/api/state');state=s;renderSidebar();}
async function refreshEmails(){var d=await api('/api/emails');emails=d.emails||[];pickupLinksByEmail={};emails.forEach(function(e){if(e.pickup_url)pickupLinksByEmail[String(e.email||'').toLowerCase()]=e.pickup_url;var acc=accounts.find(function(a){return a.id===e.account_id});e.account_name=acc?(acc.name||acc.real_email||''):(e.account_id||'');e.account_email=acc?(acc.real_email||''):'';});pickupLinksLoaded=true;E('emailCount').textContent=emails.length;updateEmailFilter();}

function renderSidebar(){
  var running=state.running;
  var creating=!!(state.creating||(batchJob&&(batchJob.status==='queued'||batchJob.status==='running')));
  ['schedDot','schedDotSettings','sideTaskDot'].forEach(function(id){var el=E(id);if(el)el.className='status-dot '+(creating?'online busy':(running?'online':'offline'));});
  var sm=running?(state.creating?'正在创建邮箱':'等待下一轮'):'任务空闲';
  if(batchJob&&(batchJob.status==='queued'||batchJob.status==='running'))sm='正在创建邮箱';
  if(E('schedLabel'))E('schedLabel').textContent=sm;
  if(E('schedLabelSettings'))E('schedLabelSettings').textContent=running?(state.creating?'创建中':'等待下轮'):'已停止';
  if(E('sideTaskText'))E('sideTaskText').textContent=sm;
  if(E('sideStatAccounts'))E('sideStatAccounts').textContent=accounts.length;
  var aliasCount=emails.length||state.alias_count||accounts.reduce(function(n,a){return n+(a.alias_total||0);},0);
  if(E('sideStatEmails'))E('sideStatEmails').textContent=aliasCount;
  if(E('sideStatReady'))E('sideStatReady').textContent=accounts.filter(function(a){return a.has_app_password}).length;
  var bs=E('btnSched');
  if(bs){bs.textContent=running?'停止自动创建':'启动自动创建';bs.className='btn btn-sm '+(running?'btn-danger':'btn-primary');}
  renderAccountStrip();
}
function accountBatchItem(accId){if(!batchJob||!batchJob.accounts)return null;return batchJob.accounts[accId]||null;}
function renderAccountStrip(){if(!E('accStripAccounts'))return;var aliasCount=emails.length||state.alias_count||accounts.reduce(function(n,a){return n+(a.alias_total||0);},0);E('accStripAccounts').textContent=accounts.length;E('accStripEmails').textContent=aliasCount;E('accStripToday').textContent=state.today_created||0;var busy=!!(batchJob&&(batchJob.status==='queued'||batchJob.status==='running'));var task='空闲';if(busy){var target=batchTargetCount(batchJob)||0;var created=batchJob.total_created||0;task=created+(target?(' / '+target):'');task=(jobDisplayStatus(batchJob)==='waiting'?'等待限制解除':' 创建中')+' '+task;}else if(state.creating){task='正在创建邮箱';}else if(state.running){task='等待下一轮';}E('accStripTask').textContent=task;E('accStripTask').parentElement.classList.toggle('is-live',busy||!!state.creating);}
function renderDashboard(){
  renderAccountStrip();
  var c=E('accCards');
  if(!c)return;
  if(!accounts.length){c.innerHTML='';return;}
  var limit=750;
  c.innerHTML=accounts.map(function(a){
    var stCls=a.status==='active'?'ok':'err';
    var stText=a.status==='active'?'登录有效':(a.last_error||'登录已过期，请重新导入 Cookie');
    var mailReady=a.has_app_password?'可以收信':'还不能收信';
    var email=a.real_email||'';
    var used=a.alias_total||0;
    var pct=Math.min(100, used*100/limit);
    var job=accountBatchItem(a.id);
    var jobBusy=job&&(job.status==='queued'||job.status==='running'||job.status==='waiting');
    var jobHtml='';
    if(jobBusy){var accTarget=parseInt(batchJob.count_per_account,10)||0,accCreated=job.created||0,accErrors=job.errors||0,mode=job.status==='waiting'?'is-wait':'is-run';jobHtml='<div class="acc-job"><div class="progress-head"><strong>'+esc(batchStatusText(job.status))+'</strong><span>'+accCreated+(accTarget?(' / '+accTarget):'')+'</span></div>'+progressBarHtml(accCreated,accErrors,accTarget||Math.max(accCreated+accErrors,1),mode)+'</div>';}
    return '<div class="acc-card'+(jobBusy?' is-busy':'')+'"><div class="acc-top"><div><div class="acc-title">'+esc(a.name||'未命名')+'</div><div class="acc-email">'+esc(email)+'</div></div><span class="status-badge '+stCls+'">'+esc(stText.substring(0,24))+'</span></div><div class="acc-usage"><div class="progress-head"><span>邮箱容量</span><span>'+used+' / '+limit+'</span></div><div class="progress-bar"><div class="fill ok" style="width:'+pct+'%"></div></div></div><div class="acc-stats"><div>'+esc(mailReady)+'</div></div>'+jobHtml+'<div class="acc-actions"><button class="btn btn-primary btn-xs" onclick="createForAccount(\''+escAttr(a.id)+'\',5)">创建邮箱</button><button class="btn btn-outline btn-xs" onclick="validateAccount(\''+escAttr(a.id)+'\')">检查登录</button><button class="btn btn-outline btn-xs" onclick="showAppPwdModal(\''+escAttr(a.id)+'\')">设置收信</button><button class="btn btn-outline btn-xs" onclick="removeAccount(\''+escAttr(a.id)+'\')">删除</button></div></div>';
  }).join('');
}
function updateEmailFilter(){var sel=E('aliasFilter');if(!sel)return;var old=sel.value;sel.innerHTML='<option value="all">全部账号 ('+emails.length+')</option>';var byAcc={};emails.forEach(function(e){var ak=e.account_id||'?';byAcc[ak]=(byAcc[ak]||0)+1;});Object.keys(byAcc).forEach(function(ak){var acc=accounts.find(function(x){return x.id===ak});var label=acc?(acc.name||acc.real_email||ak):ak;sel.innerHTML+='<option value="'+escAttr(ak)+'">'+esc(label)+' ('+byAcc[ak]+')</option>';});sel.value=old||'all';}
async function loadPickupLinks(){var d=await apiSlow('/api/pickup-links');if(d.error){toast('取件链接生成失败: '+d.error,true);return}pickupLinksByEmail={};(d.links||[]).forEach(function(x){pickupLinksByEmail[String(x.email||'').toLowerCase()]=x.url});pickupLinksLoaded=true;}
function setAliasPageSize(size){aliasPageSize=parseInt(size,10)||50;aliasPage=1;renderAliasTable();}
function setAliasPage(page){aliasPage=parseInt(page,10)||1;if(aliasPage<1)aliasPage=1;renderAliasTable();}
function updateAliasPager(total){var pages=Math.max(1,Math.ceil((total||0)/aliasPageSize));if(aliasPage>pages)aliasPage=pages;if(aliasPage<1)aliasPage=1;var start=total?((aliasPage-1)*aliasPageSize+1):0;var end=Math.min(aliasPage*aliasPageSize,total||0);var info=E('aliasPageInfo');if(info)info.textContent=total?(start+'-'+end+' / '+total):'0';var prev=E('btnAliasPrev'),next=E('btnAliasNext');if(prev)prev.disabled=aliasPage<=1||!total;if(next)next.disabled=aliasPage>=pages||!total;document.querySelectorAll('[data-page-size]').forEach(function(btn){btn.classList.toggle('active',String(aliasPageSize)===String(btn.dataset.pageSize));});}
function setExportFilter(value){exportFilter=value;aliasPage=1;document.querySelectorAll('[data-export-filter]').forEach(function(btn){btn.classList.toggle('active',btn.dataset.exportFilter===value)});renderAliasTable();}
function togglePickupSelected(email,checked){var key=String(email||'').toLowerCase();if(checked)pickupSelected[key]=true;else delete pickupSelected[key];}
function toggleAllPickup(){var checks=document.querySelectorAll('#aliasTableContainer input.pickup-check:not(:disabled)');var shouldCheck=Array.from(checks).some(function(c){return !c.checked});checks.forEach(function(c){c.checked=shouldCheck;togglePickupSelected(c.dataset.email,shouldCheck);});}
function copyPickup(url){if(!url){toast('取件链接尚未生成',true);return}navigator.clipboard.writeText(url).then(function(){toast('取件链接已复制')});}
function visibleAliases(){var accountFilter=E('aliasFilter').value;return emails.filter(function(e){if(accountFilter!=='all'&&e.account_id!==accountFilter)return false;if(exportFilter==='exported')return !!e.exported;if(exportFilter==='unexported')return !e.exported;return true;});}
function formatExportTime(value){if(!value)return '--';try{return new Date(value).toLocaleString('zh-CN',{hour12:false})}catch(_){return value}}
async function exportSelectedPickupTxt(){var selected=emails.filter(function(e){return !e.exported&&pickupSelected[String(e.email||'').toLowerCase()]}).map(function(e){return e.email});if(!selected.length){toast('请先勾选未导出的邮箱',true);return}var d=await apiSlow('/api/pickup-links/export',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({emails:selected})});if(!d.ok){toast('导出失败: '+(d.error||'未知错误'),true);return}if(!(d.lines||[]).length){toast('所选邮箱均已导出，未重复生成文件',true);await refreshEmails();renderAliasTable();return}var b=new Blob(['\uFEFF'+d.lines.join('\n')],{type:'text/plain;charset=utf-8'}),a=document.createElement('a');a.href=URL.createObjectURL(b);a.download='icloud_mail_pickup_links_'+new Date().toISOString().slice(0,10)+'.txt';a.click();setTimeout(function(){URL.revokeObjectURL(a.href)},1000);selected.forEach(function(email){delete pickupSelected[String(email).toLowerCase()]});await refreshEmails();renderAliasTable();toast('已导出 '+d.count+' 条');}
async function restoreExportedEmail(email){if(!confirm('确认将 '+email+' 恢复为未导出？'))return;var d=await api('/api/export-history/restore',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({emails:[email]})});if(!d.ok){toast('恢复失败: '+(d.error||'未知错误'),true);return}await refreshEmails();renderAliasTable();toast('已恢复为未导出');}
function renderAliasTable(){updateEmailFilter();var filtered=visibleAliases();var exportedCount=emails.filter(function(e){return e.exported}).length;var unexportedCount=emails.length-exportedCount;E('exportCountUnexported').textContent='未导出 '+unexportedCount;E('exportCountExported').textContent='已导出 '+exportedCount;E('exportCountAll').textContent='全部 '+emails.length;E('emailCount').textContent=filtered.length+' / '+emails.length;updateAliasPager(filtered.length);var c=E('aliasTableContainer');if(!filtered.length){c.innerHTML='<div class="empty"><div class="icon"></div>'+(emails.length?'当前筛选下没有邮箱':'还没有邮箱。请先添加账号，再点击「创建邮箱」。')+'</div>';return;}if(!pickupLinksLoaded){c.innerHTML='<div class="empty">正在生成取件链接...</div>';loadPickupLinks().then(renderAliasTable);return;}var start=(aliasPage-1)*aliasPageSize;var pageItems=filtered.slice(start,start+aliasPageSize);var pages=Math.max(1,Math.ceil(filtered.length/aliasPageSize));var h='<table class="email-table"><thead><tr><th style="width:42px"><input type="checkbox" title="全选本页" onclick="toggleAllPickup()"></th><th>#</th><th>邮箱地址</th><th>取件链接</th><th>所属账号</th><th>标签</th><th>创建时间</th><th>导出状态</th><th>邮箱状态</th></tr></thead><tbody>';pageItems.forEach(function(e,i){var key=String(e.email||'').toLowerCase();var url=pickupLinksByEmail[key]||'';var checked=pickupSelected[key]&&!e.exported?' checked':'';var disabled=e.exported?' disabled':'';var accName=e.account_name||e.account_email||e.account_id||'--';var activeHtml=e.hasOwnProperty('active')?(e.active?'<span style="color:var(--green)">可用</span>':'<span style="color:var(--red)">停用</span>'):'<span style="color:var(--muted)">--</span>';var exportHtml=e.exported?'<span style="color:var(--green)">已导出</span><div class="hint">'+esc(formatExportTime(e.exported_at))+'</div><button class="copy-btn" onclick="restoreExportedEmail(\''+escAttr(e.email||'')+'\')" title="恢复后可再次导出">恢复</button>':'<span style="color:var(--muted)">未导出</span>';h+='<tr><td><input class="pickup-check" type="checkbox" data-email="'+escAttr(e.email||'')+'"'+checked+disabled+' onchange="togglePickupSelected(this.dataset.email,this.checked)"></td><td class="hint">'+(start+i+1)+'</td><td class="mono">'+esc(e.email||'')+'</td><td class="pickup-cell">'+(url?'<button class="copy-btn" onclick="copyPickup(\''+escAttr(url)+'\')" title="'+escAttr(url)+'">复制链接</button>':'<span class="hint">生成失败</span>')+'</td><td>'+esc(accName)+'</td><td class="hint">'+esc((e.label||'').substring(0,30))+'</td><td style="white-space:nowrap">'+esc(formatExportTime(e.created_at))+'</td><td>'+exportHtml+'</td><td>'+activeHtml+'</td></tr>';});h+='</tbody></table>';h+='<div class="pager pager-bottom"><span class="hint">'+(start+1)+'-'+(start+pageItems.length)+' / '+filtered.length+'</span><button class="btn btn-outline btn-sm" onclick="setAliasPage(aliasPage-1)"'+(aliasPage<=1?' disabled':'')+'>上一页</button><button class="btn btn-outline btn-sm" onclick="setAliasPage(aliasPage+1)"'+(aliasPage>=pages?' disabled':'')+'>下一页</button></div>';c.innerHTML=h;}
function batchStatusText(status){var labels={waiting:'等待 Apple 限制解除',queued:'等待中',running:'创建中',completed:'已完成',partial:'部分成功',limited:'Apple 已限制',failed:'失败'};return labels[status]||status||'--';}
function renderBatchPanel(){var activeAccs=accounts.filter(function(a){return a.status==='active'});E('batchAccCount').textContent=activeAccs.length+' 个可用账号';var g=E('batchChkGroup');if(!activeAccs.length){g.innerHTML='<span class="hint">没有可用账号，请先添加</span>';E('btnBatchExec').disabled=true;}else{g.innerHTML=activeAccs.map(function(a){var email=a.real_email||a.name||a.id;var limited=a.create_status==='limited';var note=limited?'<span style="color:var(--red);font-size:12px">上次触发限制，本次会再试一次</span>':'';return'<label class="chk-item"><input type="checkbox" value="'+escAttr(a.id)+'" checked><span><strong>'+esc(a.name||email.substring(0,20))+'</strong> '+note+'</span></label>';}).join('');E('btnBatchExec').disabled=!!(batchJob&&(batchJob.status==='queued'||batchJob.status==='running'));}if(batchJob)renderBatchJob(batchJob);else loadCurrentBatchJob();}
async function loadCurrentBatchJob(){var d=await api('/api/create-batch-current');if(d.ok&&d.job){batchJob=d.job;renderBatchJob(batchJob);if(batchJob.status==='queued'||batchJob.status==='running')scheduleBatchPoll();}}
function jobDisplayStatus(job){var waiting=false,runningAcc=false;Object.keys(job.accounts||{}).forEach(function(id){var st=(job.accounts[id]||{}).status;if(st==='waiting')waiting=true;if(st==='running')runningAcc=true;});if((job.status==='queued'||job.status==='running')&&waiting&&!runningAcc)return 'waiting';return job.status;}
function batchTargetCount(job){var per=parseInt(job.count_per_account,10)||0;var accs=job.total_accounts||Object.keys(job.accounts||{}).length||0;var target=per*accs;return target||((job.total_created||0)+(job.total_errors||0));}
function progressBarHtml(created,errors,target,mode){var createdPct=target?Math.min(100,created*100/target):0;var errorPct=target?Math.min(100-createdPct,errors*100/target):0;if(created&&createdPct<1.2)createdPct=1.2;return '<div class="progress-bar'+(mode?(' '+mode):'')+'"><div class="fill ok" style="width:'+createdPct+'%"></div>'+(errorPct?('<div class="fill err" style="width:'+errorPct+'%"></div>'):'')+'</div>';}
function retryLeftText(retryAt){if(!retryAt)return '';var t=Date.parse(retryAt);if(!t)return '';var sec=Math.max(0,Math.round((t-Date.now())/1000));if(sec<=0)return '即将继续';if(sec<60)return '约 '+sec+' 秒后继续';return '约 '+Math.ceil(sec/60)+' 分钟后继续';}
function renderBatchJob(job){var box=E('batchProgress');if(!job){box.innerHTML='';return}var total=job.total_accounts||0,done=job.completed_accounts||0,created=job.total_created||0,errors=job.total_errors||0,target=batchTargetCount(job)||0;var processed=target?Math.min(target,created+errors):created+errors;var pct=target?Math.round(processed*100/target):0;var displayStatus=jobDisplayStatus(job);var statusColor=displayStatus==='completed'?'var(--green)':(displayStatus==='failed'||displayStatus==='limited'||displayStatus==='waiting')?'var(--red)':'var(--ink)';var running=job.status==='queued'||job.status==='running';var barMode=displayStatus==='waiting'?'is-wait':(running?'is-run':'');var h='<div class="progress-card"><div class="progress-head"><strong style="color:'+statusColor+'">'+esc(batchStatusText(displayStatus))+'</strong><span>'+created+' / '+target+' · '+pct+'%</span></div>'+progressBarHtml(created,errors,target,barMode)+'<div class="progress-meta"><span>'+done+'/'+total+' 个账号完成</span><span>'+created+' 成功 / '+errors+' 失败</span></div>';Object.keys(job.accounts||{}).forEach(function(id){var item=job.accounts[id],color=item.status==='completed'?'var(--green)':(item.status==='limited'||item.status==='failed'||item.status==='waiting')?'var(--red)':'var(--muted)';var accTarget=parseInt(job.count_per_account,10)||0,accCreated=item.created||0,accErrors=item.errors||0;var accMode=item.status==='waiting'?'is-wait':((item.status==='running'||item.status==='queued')?'is-run':'');var extra=retryLeftText(item.retry_at);h+='<div class="progress-item"><div class="progress-head"><strong>'+esc(item.name||id)+'</strong><span style="color:'+color+'">'+esc(batchStatusText(item.status))+(accTarget?(' · '+accCreated+' / '+accTarget):(' · '+accCreated))+'</span></div>'+progressBarHtml(accCreated,accErrors,accTarget||Math.max(accCreated+accErrors,1),accMode)+(item.error?('<div class="progress-note" style="color:var(--red)">'+esc(item.error)+(extra?(' · '+esc(extra)):'' )+'</div>'):'')+'</div>';});h+='</div>';box.innerHTML=h;E('btnBatchExec').disabled=running;E('btnBatchExec').textContent=running?'正在创建...':'开始创建';renderSidebar();if(curTab==='accounts')renderDashboard();}
function scheduleBatchPoll(){if(batchPollTimer)clearTimeout(batchPollTimer);batchPollTimer=setTimeout(pollBatchJob,1200);}
async function pollBatchJob(){if(!batchJob||!batchJob.id)return;var d=await api('/api/create-batch/'+encodeURIComponent(batchJob.id));if(!d.ok){toast('获取进度失败: '+(d.error||'未知错误'),true);return}batchJob=d.job;renderBatchJob(batchJob);if(batchJob.status==='queued'||batchJob.status==='running'){scheduleBatchPoll();return}await refreshAll();if(batchJob.total_created){toast('创建完成: '+batchJob.total_created+' 个成功');}else{toast('本次没有创建成功，请查看账号错误',true);}}
async function execBatchCreate(){var checks=document.querySelectorAll('#batchChkGroup input:checked');var ids=[];checks.forEach(function(c){ids.push(c.value)});if(!ids.length){toast('请勾选至少一个账号',true);return}var count=Math.max(1,Math.min(parseInt(E('batchCount').value)||5,750));E('batchCount').value=count;var label=E('batchLabel').value.trim();var btn=E('btnBatchExec');btn.disabled=true;btn.textContent='正在启动...';var d=await api('/api/create-batch',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({account_ids:ids,count_per_account:count,label:label})});if(!d.ok){btn.disabled=false;btn.textContent='开始创建';if(d.job_id){batchJob={id:d.job_id,status:'running'};scheduleBatchPoll();}toast(d.error||'创建任务启动失败',true);return}batchJob=d.job;renderBatchJob(batchJob);scheduleBatchPoll();}
function setInboxBusy(busy){_inboxBusy=busy;['btnInboxSearch','btnInboxAll'].forEach(function(id){var btn=E(id);if(btn)btn.disabled=busy});}
function beginInboxRequest(){if(_inboxSse){_inboxSse.close();_inboxSse=null}_inboxStreamMsgs=[];_inboxRequestSeq+=1;setInboxBusy(true);return _inboxRequestSeq;}
function inboxRequestCurrent(seq,accId){return seq===_inboxRequestSeq&&E('inboxAccount').value===accId;}
function finishInboxRequest(seq){if(seq!==_inboxRequestSeq)return;setInboxBusy(false);}
function inboxSetupHintHtml(){
  var accId=E('inboxAccount')?E('inboxAccount').value:'';
  var acc=accId?accounts.find(function(a){return a.id===accId}):null;
  if(acc&&!acc.has_app_password){
    return '<div class="empty"><div class="icon"></div><div class="empty-title">这个账号还不能收信</div><p>请先设置收信密码（Apple 的 App 专用密码），然后就可以查看邮件。</p><button class="btn btn-primary" onclick="showAppPwdModal(\''+escAttr(accId)+'\')">去设置密码</button></div>';
  }
  if(!accounts.length){
    return '<div class="empty"><div class="icon"></div><div class="empty-title">收件前先完成设置</div><ol class="empty-steps"><li>在「账号」里添加 Apple 账号</li><li>为账号设置收信密码</li><li>选择账号后即可看信</li></ol><button class="btn btn-primary" onclick="showAddAccountModal()">添加账号</button></div>';
  }
  return '<div class="empty"><div class="icon"></div><div class="empty-title">请先选择账号</div><p>添加账号后，还要设置收信密码，才能在这里看信。</p><button class="btn btn-primary" onclick="openInboxSettings()">设置收信密码</button></div>';
}
function renderInboxSetupHint(){var el=E('inboxMsgs');if(el)el.innerHTML=inboxSetupHintHtml();}
function renderInboxSetupHintIfNeeded(){
  var sel=E('inboxAccount');
  var accId=sel?sel.value:'';
  var acc=accId?accounts.find(function(a){return a.id===accId}):null;
  if(!accId||(acc&&!acc.has_app_password))renderInboxSetupHint();
}
function refreshInbox(force){var accId=E('inboxAccount').value;if(!accId){beginInboxRequest();finishInboxRequest(_inboxRequestSeq);renderInboxSetupHint();return}var acc=accounts.find(function(a){return a.id===accId});if(acc&&!acc.has_app_password){renderInboxSetupHint();return}var seq=beginInboxRequest();var limit=parseInt(E('inboxLimit').value)||20;if(force){E('inboxMsgs').innerHTML='<div class="empty"><div class="icon"></div>正在重新拉取邮件...</div>';api('/api/accounts/'+encodeURIComponent(accId)+'/inbox?limit='+limit+'&force=1',{timeout:120000}).then(function(d){if(!inboxRequestCurrent(seq,accId))return;if(d.error){E('inboxMsgs').innerHTML='<div class="empty"><div class="icon"></div>'+esc(d.error||'连接失败')+'</div>';finishInboxRequest(seq);return}renderInboxMsgs(d.emails||[],'收件箱 ('+(d.count||0)+' 封)',accId);updateCacheStatus(d.cached);finishInboxRequest(seq);});return}startInboxStream(accId,seq);}
function startInboxStream(accId,seq){E('inboxMsgs').innerHTML='<div class="empty"><div class="icon"></div>正在拉取邮件...</div>';var limit=parseInt(E('inboxLimit').value)||20;var source=new EventSource('/api/accounts/'+encodeURIComponent(accId)+'/inbox-stream?limit='+limit);_inboxSse=source;source.onmessage=function(e){if(!inboxRequestCurrent(seq,accId)||_inboxSse!==source){source.close();return}try{var d=JSON.parse(e.data);if(d.type==='email'){_inboxStreamMsgs.push(d.email);renderInboxMsgs(_inboxStreamMsgs,'收件箱 ('+d.count+' 封, 加载中...)',accId)}else if(d.type==='done'){source.close();_inboxSse=null;renderInboxMsgs(_inboxStreamMsgs,'收件箱 ('+d.count+' 封)',accId);finishInboxRequest(seq)}else if(d.type==='error'){source.close();_inboxSse=null;E('inboxMsgs').innerHTML='<div class="empty"><div class="icon"></div>'+esc(d.error||'连接失败')+'</div>';finishInboxRequest(seq)}}catch(_){}};source.onerror=function(){if(!inboxRequestCurrent(seq,accId)||_inboxSse!==source){source.close();return}source.close();_inboxSse=null;if(_inboxStreamMsgs.length){renderInboxMsgs(_inboxStreamMsgs,'收件箱 ('+_inboxStreamMsgs.length+' 封, 连接中断)',accId)}else{E('inboxMsgs').innerHTML='<div class="empty"><div class="icon"></div>连接失败</div>'}finishInboxRequest(seq);};}
async function searchAliasMail(){var accId=E('inboxAccount').value,alias=E('aliasSearchInput').value.trim();if(!accId){toast('请先选择账号',true);return}if(!alias){toast('请输入邮箱地址',true);return}var seq=beginInboxRequest();E('inboxMsgs').innerHTML='<div class="empty"><div class="icon"></div>查询 '+esc(alias)+' ...</div>';var d=await api('/api/accounts/'+encodeURIComponent(accId)+'/mail/'+encodeURIComponent(alias)+'?limit=30',{timeout:120000});if(!inboxRequestCurrent(seq,accId))return;if(d.error){E('inboxMsgs').innerHTML='<div class="empty"><div class="icon"></div>'+esc(d.error)+'</div>'}else{renderInboxMsgs(d.emails||[],esc(alias)+' ('+(d.count||0)+' 封)',accId)}finishInboxRequest(seq);}
async function checkAliasMail(){var accId=E('inboxAccount').value;if(!accId){toast('请先选择账号',true);return}var seq=beginInboxRequest();E('inboxMsgs').innerHTML='<div class="empty"><div class="icon"></div>正在检查各邮箱的收件...</div>';var d=await api('/api/accounts/'+encodeURIComponent(accId)+'/alias-mail',{timeout:120000});if(!inboxRequestCurrent(seq,accId))return;if(d.error){E('inboxMsgs').innerHTML='<div class="empty"><div class="icon"></div>'+esc(d.error||'查询失败')+'</div>';finishInboxRequest(seq);return}var byAlias=d.by_alias||{},total=0,aliasKeys=Object.keys(byAlias),h='';if(!aliasKeys.length){E('inboxMsgs').innerHTML='<div class="empty"><div class="icon"></div>所有隐私邮箱暂无收件</div>';finishInboxRequest(seq);return}aliasKeys.forEach(function(alias){var msgs=byAlias[alias]||[];total+=msgs.length;h+='<div style="padding:8px 14px;border-bottom:1px solid var(--line);font-weight:600">'+esc(alias)+' ('+msgs.length+' 封)</div>';msgs.forEach(function(m){h+='<div style="padding:6px 20px;border-bottom:1px solid var(--line);font-size:12px;display:flex;justify-content:space-between;flex-wrap:wrap;gap:4px"><span><strong>'+esc(m.subject||'(无主题)')+'</strong></span><span style="color:var(--muted)">'+esc(m.from||'').substring(0,30)+'</span><span class="hint">'+(m.date||'').substring(0,19)+'</span></div>';});});E('inboxMsgs').innerHTML='<div class="hint" style="padding:8px 14px;border-bottom:1px solid var(--line)">共 '+aliasKeys.length+' 个邮箱收到 '+total+' 封邮件</div>'+h;finishInboxRequest(seq);}
function renderInboxMsgs(msgs,title,accountId){_inboxRenderedAccount=accountId||E('inboxAccount').value;if(!msgs.length){E('inboxMsgs').innerHTML='<div class="empty"><div class="icon"></div>收件箱为空</div>';return}var h='<div class="hint" style="padding:8px 16px;border-bottom:1px solid var(--line)">'+esc(title)+'</div>';msgs.forEach(function(m,i){var mid=m.id||'m'+i;h+='<div class="email-item" style="border-bottom:1px solid var(--line);cursor:pointer" onclick="toggleEmail(\''+escAttr(mid)+'\',\''+escAttr(m.id||'')+'\',\''+escAttr(_inboxRenderedAccount)+'\')"><div style="padding:12px 16px;display:flex;justify-content:space-between;align-items:flex-start;gap:12px"><div style="flex:1;min-width:0"><div style="font-weight:600;margin-bottom:4px">'+esc(m.subject||'(无主题)')+'</div><div style="font-size:12px;color:var(--muted)">'+esc(m.from||'')+'</div><div class="hint" style="margin-top:2px">To: '+esc((m.to||'').substring(0,50))+'</div></div><div class="hint" style="white-space:nowrap">'+(m.date||'').substring(0,19)+'</div></div><div id="'+escAttr(mid)+'_body" style="display:none;padding:0 16px 16px;line-height:1.7;color:var(--muted);white-space:pre-wrap;word-break:break-word;max-height:400px;overflow-y:auto;border-top:1px solid var(--line)"></div></div>'});E('inboxMsgs').innerHTML=h;}
async function toggleEmail(domId,msgId,accountId){var bodyEl=E(domId+'_body');if(!bodyEl)return;if(_expandedEmail&&_expandedEmail!==domId){var prev=E(_expandedEmail+'_body');if(prev)prev.style.display='none'}if(bodyEl.style.display==='block'){bodyEl.style.display='none';_expandedEmail=null;return}bodyEl.style.display='block';_expandedEmail=domId;if(bodyEl.textContent.trim()&&bodyEl.textContent!=='加载中...')return;bodyEl.textContent='加载中...';if(!msgId||!accountId){bodyEl.textContent='(无法获取邮件正文)';return}var d=await api('/api/accounts/'+encodeURIComponent(accountId)+'/message/'+encodeURIComponent(msgId),{timeout:120000});if(!d.ok||!d.message){bodyEl.textContent='(获取失败: '+(d.error||'未知')+')';return}bodyEl.textContent=d.message.body||'(无正文内容)';}
function updateCacheStatus(cached){if(!cached)return;var age=cached.cache_age_sec||0;var txt=age<300?'缓存 '+(age<60?Math.round(age)+'s':Math.round(age/60)+'m')+' 前':'';E('cacheStatus').textContent=cached.inbox_cached?' | '+cached.inbox_cached+' 封已缓存 '+txt:'';}
function openInboxSettings(){
  var sel=E('inboxAccount');
  var accId=sel?sel.value:'';
  if(!accounts.length){showAddAccountModal();return;}
  if(!accId){
    var need=accounts.find(function(a){return !a.has_app_password});
    if(!need){toast('请先选择账号',true);return;}
    sel.value=need.id;
    showAppPwdModal(need.id);
    return;
  }
  showAppPwdModal(accId);
}
function showAppPwdModal(accId){var acc=accounts.find(function(a){return a.id===accId});var name=acc?(acc.name||acc.real_email||accId):accId;var icloudEmail='';if(acc&&acc.icloud_email&&(acc.icloud_email.indexOf('@icloud.com')>=0||acc.icloud_email.indexOf('@me.com')>=0||acc.icloud_email.indexOf('@mac.com')>=0)){icloudEmail=acc.icloud_email;}else if(acc&&acc.real_email&&(acc.real_email.indexOf('@icloud.com')>=0||acc.real_email.indexOf('@me.com')>=0)){icloudEmail=acc.real_email;}var hasPwd=acc&&acc.has_app_password;var h='<div class="modal-overlay" id="appPwdModal" onclick="if(event.target===this)closeAppPwdModal()"><div class="modal-box"><h3>'+(hasPwd?'修改':'设置')+'收信密码</h3><p>账号: <b>'+esc(name)+'</b><br>在 <a href="https://account.apple.com/" target="_blank" rel="noopener noreferrer">account.apple.com</a> → 登录与安全 → App 专用密码 生成。</p><label class="hint">iCloud 邮箱</label><input type="text" id="icloudEmailInput" value="'+escAttr(icloudEmail)+'" placeholder="xxx@icloud.com"><label class="hint">App 专用密码'+(hasPwd?' (重新输入以更新)':'')+'</label><input type="password" id="appPwdInput" placeholder="xxxx-xxxx-xxxx-xxxx"><div class="modal-actions"><button class="btn btn-outline" onclick="closeAppPwdModal()">取消</button><button class="btn btn-primary" id="btnSetPwd" onclick="setAppPassword(\''+escAttr(accId)+'\')">保存并测试</button></div><div class="modal-msg" id="appPwdMsg"></div></div></div>';document.body.insertAdjacentHTML('beforeend',h);}
function closeAppPwdModal(){var m=E('appPwdModal');if(m)m.remove()}
async function setAppPassword(accId){var pwd=E('appPwdInput').value.trim();var email=E('icloudEmailInput').value.trim();if(!email){E('appPwdMsg').innerHTML='<span style="color:var(--red)">请输入 iCloud 邮箱</span>';return}if(!pwd){E('appPwdMsg').innerHTML='<span style="color:var(--red)">请输入密码</span>';return}var btn=E('btnSetPwd');btn.disabled=true;btn.textContent='测试中...';var d=await api('/api/accounts/'+encodeURIComponent(accId)+'/app-password',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({app_password:pwd,icloud_email:email})});btn.disabled=false;btn.textContent='保存并测试';if(d.ok){E('appPwdMsg').innerHTML='<span style="color:var(--green)">连接成功，收件箱 '+d.inbox_count+' 封</span>';var acc=accounts.find(function(a){return a.id===accId});if(acc){acc.has_app_password=true;acc.icloud_email=email;}setTimeout(closeAppPwdModal,1500);updateInboxAccountSelect();renderDashboard();if(curTab==='inbox')refreshInbox();}else{E('appPwdMsg').innerHTML='<span style="color:var(--red)">'+esc(d.error||'连接失败')+'</span>';}}
function setCreateBusy(accId,busy){if(busy)_createBusyByAccount[accId]=true;else delete _createBusyByAccount[accId];document.querySelectorAll('.acc-actions button').forEach(function(btn){var action=btn.getAttribute('onclick')||'';if(action.indexOf("createForAccount('"+accId+"'")>=0)btn.disabled=busy;});}
async function createForAccount(accId,count){if(_createBusyByAccount[accId]){toast('该账号正在创建，请稍候',true);return}setCreateBusy(accId,true);try{var d=await api('/api/accounts/'+encodeURIComponent(accId)+'/create',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({count:count})});if(d.ok)toast('成功创建 '+d.created+' 个');else toast(d.error||'创建失败',true);}finally{setCreateBusy(accId,false);await refreshAll();}}
async function validateAccount(accId){var d=await api('/api/accounts/'+encodeURIComponent(accId)+'/validate',{method:'POST'});if(d.ok)toast('登录有效: '+d.real_email);else toast('登录已过期，请重新导入 Cookie',true);refreshAll();}
async function removeAccount(accId){if(!confirm('确认删除该账号？'))return;var d=await api('/api/accounts/'+encodeURIComponent(accId)+'/remove',{method:'POST'});if(d.ok)toast('已删除');refreshAll();}
async function toggleScheduler(){var act=state.running?'stop':'start';var d=await api('/api/scheduler/'+act,{method:'POST'});if(d.ok)toast(state.running?'自动创建已停止':'自动创建已启动');refreshAll();}
function copyOne(email){navigator.clipboard.writeText(email).then(function(){toast('已复制: '+email)});}
function copyAll(){var filtered=visibleAliases();if(!filtered.length){toast('当前筛选下没有邮箱',true);return}navigator.clipboard.writeText(filtered.map(function(e){return e.email}).join('\n')).then(function(){toast('已复制 '+filtered.length+' 个')});}
function csvCell(v){v=String(v==null?'':v);if(/^[=+\-@]/.test(v))v="'"+v;return '"'+v.replace(/"/g,'""')+'"';}
function exportCSV(){var filtered=visibleAliases();if(!filtered.length){toast('当前筛选下没有邮箱',true);return}var csv='email,account,label,active\n'+filtered.map(function(e){return [e.email,e.account_name||e.account_id||'',e.label||'',e.hasOwnProperty('active')?(e.active?'yes':'no'):''].map(csvCell).join(',');}).join('\n');var b=new Blob(['\uFEFF'+csv],{type:'text/csv'}),a=document.createElement('a'),u=URL.createObjectURL(b);a.href=u;a.download='icloud_mail_aliases.csv';a.click();setTimeout(function(){URL.revokeObjectURL(u)},1000);toast('已导出 '+filtered.length+' 个');}
function clearLogs(){logs=[];E('logFeed').innerHTML=''}
function toast(msg,isErr){var t=E('toast');t.textContent=msg;t.style.background=isErr?'var(--red)':'var(--ink)';t.classList.add('show');setTimeout(function(){t.classList.remove('show')},2200);}
function connectSSE(){if(sseConn){sseConn.close();sseConn=null}sseConn=new EventSource('/api/log-stream?after='+logCursor);sseConn.onmessage=function(e){try{var entry=JSON.parse(e.data);if((entry.seq||0)<=logCursor)return;logCursor=entry.seq||logCursor;logs.push(entry);if(logs.length>500)logs=logs.slice(-500);if(curTab==='settings')renderLogs();if(entry.msg&&entry.msg.indexOf('创建')>=0)refreshLight();}catch(_){}};sseConn.onerror=function(){sseConn.close();sseConn=null;setTimeout(connectSSE,5000)};}
function renderLogs(){var f=E('logFeed');if(!f)return;f.innerHTML=logs.map(function(l){return'<div class="log-line '+l.level+'"><span class="log-time">'+esc(l.time)+'</span>'+esc(l.msg)+'</div>';}).join('\n');f.scrollTop=f.scrollHeight;}
function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;')}
function escAttr(s){return String(s).replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/'/g,'&#39;')}
function showAddAccountModal(){var h='<div class="modal-overlay" id="addAccModal" onclick="if(event.target===this)closeAddAccModal()"><div class="modal-box"><h3>添加账号</h3><p>Chrome 安装 <a href="https://chromewebstore.google.com/detail/cookie-editor/hlkenndednhfkekhgcdicdfddnkalmdm" target="_blank" rel="noopener noreferrer">Cookie Editor</a>，登录 icloud.com 后导出 Header String 粘贴即可。<br>也支持 JSON：<code>{"name1":"value1"}</code> <a href="https://chromewebstore.google.com/detail/cookie-editor/hlkenndednhfkekhgcdicdfddnkalmdm" target="_blank" rel="noopener noreferrer">下载扩展</a></p><input type="text" id="accNameInput" placeholder="账号名称，例如：主号"><textarea id="cookieInput" placeholder="粘贴 Cookie，支持 Header String 或 JSON"></textarea><div class="modal-actions"><button class="btn btn-outline" onclick="closeAddAccModal()">取消</button><button class="btn btn-primary" id="btnAddAccount" onclick="addAccount()">添加账号</button></div><div class="modal-msg" id="addAccMsg"></div></div></div>';document.body.insertAdjacentHTML('beforeend',h);}
function closeAddAccModal(){var m=E('addAccModal');if(m)m.remove()}
async function addAccount(){var name=E('accNameInput').value.trim()||'未命名账号';var cookies=E('cookieInput').value.trim();if(!cookies){E('addAccMsg').innerHTML='<span style="color:var(--red)">请粘贴 Cookie</span>';return}var btn=E('btnAddAccount');btn.disabled=true;btn.textContent='正在检查登录...';var d=await api('/api/accounts/add',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:name,cookie_input:cookies})});btn.disabled=false;btn.textContent='添加账号';if(d.ok){E('addAccMsg').innerHTML='<span style="color:var(--green)">已添加 '+esc(d.real_email||'')+'</span>';setTimeout(closeAddAccModal,1200);refreshAll();}else{E('addAccMsg').innerHTML='<span style="color:var(--red)">'+esc(d.error||'登录已过期，请重新导入 Cookie')+'</span>';}}
async function refreshAliases(){if(_aliasesBusy){toast('云端同步正在进行',true);return}_aliasesBusy=true;var btn=E('btnAliasSync');if(btn){btn.disabled=true;btn.textContent='同步中...'}try{var d=await api('/api/aliases',{timeout:120000});if(d.error&&d.ok===false){toast('云端同步失败: '+d.error,true);return}var apiAliases=d.aliases||[],apiMap={};apiAliases.forEach(function(a){apiMap[String(a.email||'').toLowerCase()]=a;});emails.forEach(function(e){var apiData=apiMap[String(e.email||'').toLowerCase()];if(apiData){e.label=apiData.label||'';e.active=apiData.active;e.anonymousId=apiData.anonymousId;e.created_at=apiData.createdAt||e.created_at;e.account_name=apiData.account_name||e.account_name;e.account_email=apiData.account_email||e.account_email;}});E('emailCount').textContent=emails.length;updateEmailFilter();renderAliasTable();var failed=Object.keys(d.failures||{});if(failed.length){toast('同步完成，但有 '+failed.length+' 个账号失败',true)}else{toast('云端同步完成: '+apiAliases.length+' 个邮箱')}}finally{_aliasesBusy=false;if(btn){btn.disabled=false;btn.textContent='云端同步'}}}
function updateInboxAccountSelect(){var sel=E('inboxAccount');if(!sel)return;var old=sel.value;sel.innerHTML='<option value="">选择账号</option>';accounts.forEach(function(a){var hasPwd=a.has_app_password?'可收信':'未设密码';var imapEmail=a.icloud_email||a.real_email||'';sel.innerHTML+='<option value="'+escAttr(a.id)+'">'+esc((a.name||a.real_email||a.id).substring(0,20))+' | '+esc(imapEmail.substring(0,25))+' '+hasPwd+'</option>';});sel.value=old||'';renderInboxSetupHintIfNeeded();}
function renderDocs(){var el=E('docsContent');if(el)el.innerHTML='';}
refreshAll();connectSSE();setInterval(refreshLight,10000);setInterval(refreshAll,30000);
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
    try:
        account = _account_mgr.add_account(name, cookie_input)
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


def _batch_uses_account(acc_id):
    with _batch_lock:
        if not _batch_active_id:
            return False
        job = _batch_jobs.get(_batch_active_id) or {}
        if job.get("status") not in ("queued", "running"):
            return False
        item = (job.get("accounts") or {}).get(acc_id) or {}
        return item.get("status") in ("queued", "running", "waiting")


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
        results = _account_mgr.create_aliases_for_account(
            acc_id, remaining, label, progress_callback=record_progress
        )
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
        job = _batch_jobs[job_id]
        job["status"] = "running"
        job["started_at"] = job.get("started_at") or datetime.now(_BJ_TZ).isoformat()
        account_ids = [
            acc_id for acc_id in job["account_ids"]
            if not (
                job["accounts"][acc_id].get("finished_at")
                and job["accounts"][acc_id].get("status") in (
                    "completed", "partial", "failed", "limited"
                )
            )
        ]
        count = job["count_per_account"]
        label = job["label"]
        _save_batch_state_locked()
    total_accounts = len(job["account_ids"])
    workers = min(_BATCH_MAX_ACCOUNT_WORKERS, max(1, len(account_ids)))
    _update_state(
        creating=True,
        round_status=f"批量创建 {job.get('completed_accounts', 0)}/{total_accounts} 个账号",
    )
    _emit_log(
        "info", f"批量任务启动: {total_accounts} 个账号 x{count}，并行账号数 {workers}"
    )

    try:
        if account_ids:
            with ThreadPoolExecutor(
                max_workers=workers, thread_name_prefix="batch-account"
            ) as executor:
                futures = {
                    executor.submit(_run_batch_account, job, acc_id, count, label): acc_id
                    for acc_id in account_ids
                }
                for future in as_completed(futures):
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
        if _batch_active_id:
            active = _batch_jobs.get(_batch_active_id)
            if active and active.get("status") in ("queued", "running"):
                return jsonify({
                    "ok": False,
                    "error": "已有批量任务正在运行",
                    "job_id": _batch_active_id,
                }), 409
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
                acc_id: {
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
                }
                for acc_id in account_ids
            },
        }
        _batch_jobs[job_id] = job
        while len(_batch_jobs) > _BATCH_JOB_HISTORY:
            _batch_jobs.popitem(last=False)
        _batch_active_id = job_id
        _save_batch_state_locked()
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
    try:
        msgs = _account_mgr.check_alias_mail(acc_id, alias_email, limit=limit, days=days)
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
            return jsonify({"ok":True,"already_running":True})
        _scheduler_stop_event.clear()
        _scheduler_thread = threading.Thread(target=_scheduler_loop, daemon=True)
        _scheduler_thread.start()
        _update_state(running=True)
        return jsonify({"ok":True,"already_running":False})

@app.route("/api/scheduler/stop", methods=["POST"])
def api_scheduler_stop():
    _scheduler_stop_event.set()
    _update_state(running=False, creating=False, next_trigger=None, round_status="正在停止")
    return jsonify({"ok":True})

@app.route("/api/log-stream")
def api_log_stream():
    start_cursor = request.args.get("after", 0, type=int) or 0
    last_event_id = request.headers.get("Last-Event-ID", "")
    if last_event_id.isdigit():
        start_cursor = max(start_cursor, int(last_event_id))
    def generate():
        cursor = start_cursor
        while True:
            with _log_condition:
                has_data = _log_condition.wait_for(
                    lambda: _log_seq > cursor or _shutdown_event.is_set(), timeout=30
                )
                entries = [entry for entry in _log_entries if entry["seq"] > cursor]
            if _shutdown_event.is_set():
                return
            if not has_data or not entries:
                yield ": heartbeat\n\n"
                continue
            for entry in entries:
                cursor = entry["seq"]
                yield f"id: {cursor}\ndata: {json.dumps(entry,ensure_ascii=False)}\n\n"
    return Response(generate(), mimetype="text/event-stream", headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"})

def main():
    import argparse, os, signal as _signal
    parser = argparse.ArgumentParser(description="iCloud HME Web UI")
    parser.add_argument("--port",type=int,default=int(os.environ.get("PORT",5050)))
    parser.add_argument("--host",type=str,default=os.environ.get("HOST","0.0.0.0"))
    parser.add_argument("--scheduler",action="store_true",help="启动时自动运行调度器")
    parser.add_argument("--no-sync",action="store_true",help="跳过时间校准")
    args = parser.parse_args()
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
    if args.scheduler:
        global _scheduler_thread
        _scheduler_stop_event.clear()
        _scheduler_thread = threading.Thread(target=_scheduler_loop, daemon=True)
        _scheduler_thread.start()
        _update_state(running=True)
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
