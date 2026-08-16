#!/usr/bin/env python3
"""iCloud HME Web UI — 多账号聚合管理平台 — Flask single-page app."""
import sys, os, json, time, queue, secrets, threading, re, hashlib
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from html import escape as _html_escape
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path: sys.path.insert(0, str(HERE))

from flask import Flask, Response, request, jsonify, render_template_string, redirect
from icloud_hme import ICloudHME, extract_chrome_cookies
from account_manager import AccountManager
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

_log_queue = queue.Queue()
_today_key = datetime.now().strftime("%Y%m%d")
_global_state = {"running":False,"creating":False,"round_status":"","total_created":0,"today_created":0,"current_round_created":0,"next_trigger":None,"last_error":None,"cookies_ok":False,"alias_count":0,"alias_active":0}
_lock = threading.Lock()
_scheduler_thread = None
_stop_event = threading.Event()
_account_mgr = AccountManager()
_pickup_store = PickupLinkStore(RESULTS_DIR / "pickup_links.json")
PICKUP_BASE_URL = os.environ.get("PICKUP_BASE_URL", "").rstrip("/")
_pickup_refresh_lock = threading.Lock()
_pickup_refreshing = set()
_pickup_last_refresh = {}
_pickup_refreshing_accounts = set()
_pickup_last_account_refresh = {}
_pickup_executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="pickup")
_pickup_pending = 0
_PICKUP_MAX_PENDING = 32
_pickup_body_cache = {}
_pickup_body_refreshing = set()

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
                _time_offset = (net_time - datetime.now()).total_seconds()
                return _time_offset
        except: continue
    return 0.0

def _now() -> datetime: return datetime.now() + timedelta(seconds=_time_offset)

def _emit_log(level, msg): _log_queue.put({"time":_now().strftime("%H:%M:%S"),"level":level,"msg":msg})

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
    def _bj_hour() -> int: return (_now().hour + 8) % 24
    while not _stop_event.is_set():
        h = _bj_hour()
        if h < 7 or h >= 20: _update_state(round_status=f"非窗口时段 (BJ {h}:00)，等待..."); _stop_event.wait(1800); continue
        active_accounts = [a for a in _account_mgr.list_accounts() if a.get("status") == "active"]
        if not active_accounts: _update_state(creating=False, round_status="无活跃账号，跳过"); _stop_event.wait(1800); continue
        round_total = 0
        for i, account in enumerate(active_accounts):
            if _stop_event.is_set(): break
            acc_id = account["id"]; acc_name = account.get("name", acc_id)
            target_count = _random.randint(3, 5)
            _emit_log("info", f"[{acc_name}] 本轮目标 {target_count} 个")
            client = ICloudHME(account["cookies"], host=account.get("host","icloud.com"), verbose=False)
            created = 0; errors = 0
            while created < target_count and errors < 3 and not _stop_event.is_set():
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
        _stop_event.wait(interval_sec)
    _update_state(running=False, next_trigger=None, round_status="已停止")
    _emit_log("info", "调度器已停止")

def _health_loop():
    _error_reported = set()
    while not _stop_event.is_set():
        if _stop_event.wait(300): break
        for account in _account_mgr.list_accounts():
            if account.get("status") != "active": continue
            try: _account_mgr.validate_account(account["id"]); _error_reported.discard(account["id"])
            except Exception as e:
                if account["id"] not in _error_reported: _emit_log("warn",f"健康检查失败 [{account.get('name','?')}]: {str(e)[:100]}"); _error_reported.add(account["id"])

# ----- HTML -----
UI_HTML = r"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0"><title>iCloud HME — 多账号管理</title><style>:root{--paper:#f3efe4;--paper-dim:#e8e2d4;--ink:#0f0e0c;--ink-soft:#5c564e;--ink-faint:#9a938a;--rule:rgba(15,14,12,.12);--rule-strong:rgba(15,14,12,.22);--red:#b7392d;--green:#1f8b4c;--mono:"SF Mono","Fira Code","Cascadia Code",Consolas,monospace;--sans:"PingFang SC","Microsoft YaHei","Noto Sans SC",system-ui,sans-serif}*{margin:0;padding:0;box-sizing:border-box}html{min-width:1040px;background:var(--paper);font-size:16px}body{color:var(--ink);font-family:var(--sans);min-height:100vh;display:flex;background:radial-gradient(circle at 10% 8%,rgba(183,57,45,.03),transparent 26%),radial-gradient(circle at 78% 42%,rgba(15,14,12,.025),transparent 30%),linear-gradient(90deg,rgba(15,14,12,.018) 1px,transparent 1px),linear-gradient(rgba(15,14,12,.018) 1px,transparent 1px),var(--paper);background-size:auto,auto,64px 64px,64px 64px,auto}.sidebar{width:260px;background:var(--paper);border-right:1px solid var(--rule-strong);padding:28px 22px;display:flex;flex-direction:column;gap:3px;flex-shrink:0;overflow-y:auto}.sidebar .logo{font-family:var(--mono);font-size:15px;letter-spacing:.28em;text-transform:uppercase;color:var(--ink-faint);margin-bottom:24px;display:flex;align-items:center;gap:14px}.sidebar .logo .icon{width:16px;height:16px;background:var(--red);transform:rotate(45deg);flex-shrink:0}.sidebar .nav-item{padding:10px 0;color:var(--ink-soft);font-size:15px;cursor:pointer;user-select:none;display:flex;align-items:center;gap:10px;border-bottom:1px solid transparent;transition:border-color .2s,color .2s;font-family:var(--mono);letter-spacing:.03em}.sidebar .nav-item:hover{color:var(--ink);border-bottom-color:var(--rule)}.sidebar .nav-item.active{color:var(--ink);border-bottom-color:var(--red);font-weight:600}.sidebar .section-label{font-family:var(--mono);font-size:11px;color:var(--ink-faint);text-transform:uppercase;letter-spacing:.3em;padding:22px 0 10px}.sidebar .account-item{padding:9px 0;font-size:13px;cursor:pointer;display:flex;align-items:center;gap:10px;border-left:2px solid transparent;padding-left:10px;transition:all .15s;font-family:var(--mono)}.sidebar .account-item:hover{color:var(--ink)}.sidebar .account-item.selected{border-left-color:var(--red);font-weight:600}.sidebar .account-item .acc-dot{width:7px;height:7px;transform:rotate(45deg);flex-shrink:0}.sidebar .account-item .acc-dot.active{background:var(--green)}.sidebar .account-item .acc-dot.error{background:var(--red)}.sidebar .account-item .acc-name{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.sidebar .account-item .acc-del{opacity:0;color:var(--red);cursor:pointer;font-size:16px;line-height:1}.sidebar .account-item:hover .acc-del{opacity:.5}.sidebar .account-item .acc-del:hover{opacity:1}#sidebarAccounts{max-height:340px;overflow-y:auto}.status-dot{display:inline-block;width:7px;height:7px;transform:rotate(45deg);margin-right:8px;vertical-align:middle}.status-dot.online{background:var(--green)}.status-dot.offline{background:var(--ink-faint)}.main{flex:1;padding:32px 44px;overflow-y:auto;display:flex;flex-direction:column;gap:24px}.header{display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:16px}.header h1{font-family:var(--mono);font-size:14px;color:var(--ink-faint);letter-spacing:.28em;text-transform:uppercase;font-weight:400}.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:1px;background:var(--rule-strong);border:1px solid var(--rule-strong)}.card{background:var(--paper);padding:22px 24px;transition:background .15s}.card:hover{background:var(--paper-dim)}.card .label{font-family:var(--mono);font-size:11px;color:var(--ink-faint);text-transform:uppercase;letter-spacing:.3em;margin-bottom:10px}.card .value{font-size:38px;font-weight:800;letter-spacing:-1px;font-family:var(--mono)}.card .value.accent{color:var(--red)}.card .value.green{color:var(--green)}.card .value.orange{color:var(--ink-soft)}.card .value.blue{color:var(--ink)}.card .sub{font-size:13px;color:var(--ink-faint);margin-top:6px;font-family:var(--mono)}.acc-cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:1px;background:var(--rule-strong);border:1px solid var(--rule-strong);margin-top:2px}.acc-card{background:var(--paper);padding:22px 24px;transition:background .15s}.acc-card:hover{background:var(--paper-dim)}.acc-card .acc-header{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:14px}.acc-card .acc-title{font-weight:700;font-size:16px;font-family:var(--mono)}.acc-card .acc-email{font-size:13px;color:var(--ink-faint);font-family:var(--mono);margin-top:4px}.acc-card .acc-stats{display:flex;gap:24px;margin-top:12px}.acc-card .acc-stat{font-size:13px;font-family:var(--mono);color:var(--ink-soft)}.acc-card .acc-stat .n{font-weight:700;color:var(--ink)}.acc-card .acc-actions{margin-top:14px;display:flex;gap:8px}.acc-card .status-badge{font-family:var(--mono);font-size:11px;padding:2px 0;letter-spacing:.08em;text-transform:uppercase}.acc-card .status-badge.ok{color:var(--green);border-bottom:1px solid var(--green)}.acc-card .status-badge.err{color:var(--red);border-bottom:1px solid var(--red)}.panel{background:var(--paper);border:1px solid var(--rule-strong);overflow:hidden}.panel-header{padding:14px 20px;border-bottom:1px solid var(--rule);display:flex;justify-content:space-between;align-items:center;font-family:var(--mono);font-size:12px;color:var(--ink-faint);text-transform:uppercase;letter-spacing:.16em}.panel-body{padding:0}.btn{padding:9px 22px;font-size:13px;cursor:pointer;border:none;font-family:var(--mono);transition:all .15s;letter-spacing:.03em;background:var(--ink);color:var(--paper)}.btn:hover{opacity:.78}.btn:disabled{opacity:.28;cursor:not-allowed}.btn-primary{background:var(--ink);color:var(--paper)}.btn-outline{background:transparent;border:1px solid var(--rule-strong);color:var(--ink)}.btn-outline:hover{background:var(--ink);color:var(--paper);border-color:var(--ink);opacity:1}.btn-danger{background:transparent;color:var(--red);border:1px solid var(--red)}.btn-danger:hover{background:var(--red);color:var(--paper);opacity:1}.btn-sm{padding:5px 14px;font-size:12px}.btn-xs{padding:3px 10px;font-size:11px}.btn-group{display:flex;gap:10px}.chk-group{display:flex;flex-wrap:wrap;gap:10px;padding:10px 0}.chk-item{display:flex;align-items:center;gap:8px;font-size:14px;cursor:pointer;font-family:var(--mono)}.chk-item input{margin:0;accent-color:var(--red);width:16px;height:16px}.email-table{width:100%;border-collapse:collapse;font-family:var(--mono)}.email-table th{text-align:left;padding:10px 18px;font-size:11px;color:var(--ink-faint);text-transform:uppercase;letter-spacing:.3em;border-bottom:1px solid var(--rule-strong);font-weight:400}.email-table td{padding:12px 18px;font-size:14px;border-bottom:1px solid var(--rule)}.email-table tr:hover td{background:var(--paper-dim)}.email-item:hover{background:var(--paper-dim)}.email-table .copy-btn{background:none;border:none;color:var(--ink-faint);cursor:pointer;font-size:15px;padding:3px 8px}.email-table .copy-btn:hover{color:var(--red)}.filter-bar{display:flex;gap:12px;align-items:center;padding:10px 18px;border-bottom:1px solid var(--rule)}.filter-bar select{padding:6px 10px;border:1px solid var(--rule-strong);font-family:var(--mono);font-size:13px;background:var(--paper);color:var(--ink)}.filter-bar select:focus{outline:none;border-color:var(--red)}.copy-toast{position:fixed;top:24px;right:24px;background:var(--ink);color:var(--paper);padding:12px 24px;font-family:var(--mono);font-size:13px;letter-spacing:.03em;opacity:0;transform:translateY(-8px);transition:all .2s;pointer-events:none;z-index:999}.copy-toast.show{opacity:1;transform:translateY(0)}.log-feed{max-height:320px;overflow-y:auto;padding:14px 20px;font-family:var(--mono);font-size:13px;line-height:1.8}.log-feed .log-line{white-space:pre-wrap;word-break:break-all}.log-line.info{color:var(--ink-soft)}.log-line.success{color:var(--green)}.log-line.warn{color:var(--red)}.log-line.error{color:var(--red);font-weight:600}.log-time{color:var(--ink-faint);margin-right:10px}.empty{text-align:center;padding:56px 20px;color:var(--ink-faint);font-family:var(--mono);font-size:13px;letter-spacing:.03em}.empty .icon{font-size:42px;margin-bottom:14px;opacity:.5}.modal-overlay{position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(15,14,12,.7);z-index:999;display:flex;align-items:center;justify-content:center}.modal-box{background:var(--paper);border:1px solid var(--ink);padding:32px;width:90%;max-width:560px;box-shadow:8px 8px 0 rgba(15,14,12,.12)}.modal-box h3{font-family:var(--mono);font-size:15px;letter-spacing:.16em;text-transform:uppercase;margin-bottom:10px;font-weight:400}.modal-box p{font-size:14px;color:var(--ink-soft);margin-bottom:16px;line-height:1.6}.modal-box input,.modal-box textarea{width:100%;background:var(--paper);color:var(--ink);border:1px solid var(--rule-strong);padding:12px 14px;font-family:var(--mono);font-size:14px;margin-bottom:14px}.modal-box textarea{height:130px;font-size:13px;resize:vertical}.modal-box input:focus,.modal-box textarea:focus{outline:none;border-color:var(--ink)}.modal-actions{display:flex;gap:12px;margin-top:16px;justify-content:flex-end}.modal-msg{margin-top:12px;font-family:var(--mono);font-size:13px}.diamond{display:inline-block;width:12px;height:12px;background:var(--red);transform:rotate(45deg);vertical-align:-2px;margin-right:4px}code{font-family:var(--mono);font-size:12px;background:var(--paper-dim);padding:1px 6px}.progress-bar{height:3px;background:var(--rule);margin-top:10px;overflow:hidden}.progress-bar .fill{height:100%;background:var(--ink);transition:width .3s}select,input[type=text],input[type=number],input[type=password]{font-family:var(--mono);font-size:13px;padding:6px 10px;border:1px solid var(--rule-strong);background:var(--paper);color:var(--ink)}select:focus,input:focus{outline:none;border-color:var(--ink)}@media(max-width:768px){body{flex-direction:column}.sidebar{width:100%;flex-direction:row;flex-wrap:wrap;padding:14px 18px;gap:6px}.sidebar .logo{margin-bottom:0;margin-right:auto}.main{padding:16px}.cards{grid-template-columns:repeat(2,1fr)}.acc-cards{grid-template-columns:1fr}}</style></head><body><aside class="sidebar"><div class="logo"><div class="icon"></div>iCloud HME</div><a class="nav-item active" data-tab="dashboard">仪表盘</a><a class="nav-item" data-tab="emails">邮箱列表</a><a class="nav-item" data-tab="batch">批量创建</a><a class="nav-item" data-tab="inbox">收件箱</a><a class="nav-item" data-tab="docs">API 文档</a><a class="nav-item" data-tab="logs">运行日志</a><div class="section-label">账号列表</div><div id="sidebarAccounts"></div><button class="btn btn-outline btn-sm" onclick="showAddAccountModal()" style="margin:8px 0">+ 添加账号</button><div style="margin-top:auto;padding-top:14px;border-top:1px solid var(--rule-strong);font-family:var(--mono);font-size:12px;color:var(--ink-faint)"><div style="margin-bottom:6px"><span class="status-dot" id="schedDot"></span><span id="schedLabel">调度器: 就绪</span></div><button class="btn btn-sm" id="btnSched" onclick="toggleScheduler()" style="width:100%;margin-top:6px">启动调度器</button></div></aside><main class="main"><div class="header"><h1 id="tabTitle">仪表盘</h1><div class="btn-group"><button class="btn btn-outline btn-sm" onclick="refreshAll()">刷新</button><button class="btn btn-primary btn-sm" onclick="showAddAccountModal()">+ 添加账号</button></div></div><div id="view-dashboard"><div class="cards" id="summaryCards"></div><div class="acc-cards" id="accCards"></div></div><div id="view-emails" style="display:none"><div class="panel"><div class="panel-header"><span>隐私邮箱列表</span><div style="display:flex;gap:8px;align-items:center"><span style="font-size:11px;color:var(--ink-faint)" id="emailCount">0</span><button class="btn btn-outline btn-sm" onclick="refreshEmails().then(renderAliasTable)">刷新</button><button class="btn btn-outline btn-sm" onclick="refreshAliases()" title="从 iCloud 云端同步标签和状态">云端同步</button><button class="btn btn-outline btn-sm" onclick="copyAll()">复制全部</button><button class="btn btn-outline btn-sm" onclick="exportCSV()">CSV</button><button class="btn btn-outline btn-sm" onclick="exportPickupTxt()">导出取件链接 TXT</button></div></div><div class="filter-bar"><span style="font-size:11px;color:var(--ink-faint)">筛选账号:</span><select id="aliasFilter" onchange="renderAliasTable()"><option value="all">全部账号</option></select></div><div class="panel-body"><div id="aliasTableContainer" class="empty"><div class="icon"></div>暂无创建记录 — 请先通过仪表盘或批量创建生成邮箱</div></div></div></div><div id="view-batch" style="display:none"><div class="panel"><div class="panel-header"><span>跨账号批量创建</span><span style="font-size:11px;color:var(--ink-faint)" id="batchAccCount">0 个可用账号</span></div><div class="panel-body" style="padding:14px"><p style="font-size:12px;color:var(--ink-faint);margin-bottom:10px">勾选目标账号，设置每个账号的创建数量，点击执行将依次为每个账号创建（账号间间隔 3 秒防限流）。</p><div class="chk-group" id="batchChkGroup"></div><div style="display:flex;gap:10px;align-items:center;margin-top:12px;flex-wrap:wrap"><label style="font-size:12px">每账号创建数量:</label><input type="number" id="batchCount" value="5" min="1" max="50" style="width:70px"><label style="font-size:13px;font-family:var(--mono)">标签前缀:</label><input type="text" id="batchLabel" placeholder="可选" style="width:150px"><button class="btn btn-primary" id="btnBatchExec" onclick="execBatchCreate()">开始创建</button></div><div id="batchProgress" style="margin-top:14px"></div></div></div></div><div id="view-inbox" style="display:none"><div class="panel"><div class="panel-header"><span>收件箱检查</span><div style="display:flex;gap:8px;align-items:center"><select id="inboxAccount" onchange="refreshInbox()"></select><input type="number" id="inboxLimit" value="20" min="1" max="100" style="width:60px" title="邮件数量"><input type="text" id="aliasSearchInput" placeholder="指定邮箱查件..." style="width:200px" title="输入隐私邮箱地址查件"><button class="btn btn-outline btn-sm" onclick="refreshInbox()">刷新</button><button class="btn btn-outline btn-sm" onclick="refreshInbox(true)" title="跳过缓存，从 iCloud 重新拉取">强制刷新</button><button class="btn btn-outline btn-sm" onclick="searchAliasMail()" title="查询指定邮箱的收件">查件</button><button class="btn btn-outline btn-sm" onclick="checkAliasMail()" title="检查所有隐私别名的收件">全部</button><button class="btn btn-outline btn-sm" id="btnInboxSettings" onclick="openInboxSettings()" title="修改 iCloud 邮箱或应用密码">设置</button><span style="font-size:10px;color:var(--ink-faint);font-family:var(--mono)" id="cacheStatus"></span></div></div><div class="panel-body"><div id="inboxMsgs" class="empty"><div class="icon"></div>选择账号后点击刷新查看收件箱</div></div></div></div><div id="view-docs" style="display:none"><div class="panel" style="font-family:var(--mono);font-size:13px;line-height:1.8"><div class="panel-header"><span>API 文档</span></div><div class="panel-body" style="padding:20px 24px" id="docsContent"></div></div></div><div id="view-logs" style="display:none"><div class="panel"><div class="panel-header"><span>实时日志</span><button class="btn btn-outline btn-sm" onclick="clearLogs()">清屏</button></div><div class="panel-body"><div class="log-feed" id="logFeed"></div></div></div></div></main><div class="copy-toast" id="toast"></div><script>var E=function(id){return document.getElementById(id)};var state={running:false,creating:false,round_status:'',total_created:0,today_created:0,current_round_created:0,next_trigger:null};var accounts=[],emails=[],logs=[];var curTab='dashboard',sseConn=null;document.querySelectorAll('.nav-item').forEach(function(el){el.addEventListener('click',function(){curTab=this.dataset.tab;document.querySelectorAll('.nav-item').forEach(function(n){n.classList.remove('active')});this.classList.add('active');E('view-dashboard').style.display=curTab==='dashboard'?'block':'none';E('view-emails').style.display=curTab==='emails'?'block':'none';E('view-batch').style.display=curTab==='batch'?'block':'none';E('view-inbox').style.display=curTab==='inbox'?'block':'none';E('view-docs').style.display=curTab==='docs'?'block':'none';E('view-logs').style.display=curTab==='logs'?'block':'none';var titles={dashboard:'仪表盘',emails:'邮箱列表',batch:'批量创建',inbox:'收件箱',docs:'API 文档',logs:'运行日志'};E('tabTitle').textContent=titles[curTab]||curTab;if(curTab==='emails'){refreshEmails();renderAliasTable();}if(curTab==='batch')renderBatchPanel();if(curTab==='inbox')updateInboxAccountSelect();if(curTab==='docs')renderDocs();if(curTab==='logs')renderLogs();});});async function api(path,opts){var timeout=(opts||{}).timeout||60000;if(opts)delete opts.timeout;var ctrl=new AbortController();var t=setTimeout(function(){ctrl.abort()},timeout);try{var r=await fetch(path,Object.assign({signal:ctrl.signal},opts||{}));clearTimeout(t);return r.json();}catch(e){clearTimeout(t);var msg=(e.name==='AbortError')?('请求超时 ('+(timeout/1000)+'s)'):(e.message||'网络错误');return{ok:false,error:msg};}}async function apiSlow(path,opts){return api(path,Object.assign({timeout:60000},opts||{}));}var _refreshBusy=false;async function refreshAll(){if(_refreshBusy)return;_refreshBusy=true;try{var _a=api('/api/accounts'),_s=api('/api/state');var a=await _a,s=await _s;accounts=a.accounts||[];state=s;renderSidebar();renderDashboard();if(curTab==='emails'){await refreshEmails();renderAliasTable();}if(curTab==='batch')renderBatchPanel();updateInboxAccountSelect();}finally{_refreshBusy=false;}}async function refreshLight(){if(_refreshBusy)return;var s=await api('/api/state');state=s;var sd=E('schedDot');var running=state.running;sd.className='status-dot '+(running?'online':'offline');E('schedLabel').textContent='调度器: '+(running?(state.creating?'创建中...':'等待下轮'):'已停止');E('btnSched').textContent=running?'停止调度器':'启动调度器';E('btnSched').className='btn btn-sm '+(running?'btn-danger':'btn-primary');}async function refreshEmails(){var d=await api('/api/emails');emails=d.emails||[];emails.forEach(function(e){var acc=accounts.find(function(a){return a.id===e.account_id});e.account_name=acc?(acc.name||acc.real_email||''):(e.account_id||'');e.account_email=acc?(acc.real_email||''):'';});E('emailCount').textContent=emails.length;updateEmailFilter();}async function refreshAliases(){var d=await api('/api/aliases');var apiAliases=d.aliases||[];if(apiAliases.length){var apiMap={};apiAliases.forEach(function(a){apiMap[a.email]=a;});emails.forEach(function(e){var apiData=apiMap[e.email];if(apiData){e.label=apiData.label||'';e.active=apiData.active;e.anonymousId=apiData.anonymousId;e.account_name=apiData.account_name||e.account_name;e.account_email=apiData.account_email||e.account_email;}});}E('emailCount').textContent=emails.length;updateEmailFilter();renderAliasTable();}function renderSidebar(){var c=E('sidebarAccounts');if(!accounts.length){c.innerHTML='<div style="padding:8px 14px;font-size:11px;color:var(--ink-faint)">暂无账号</div>';}else{c.innerHTML=accounts.map(function(a,i){var cls=a.status==='active'?'active':'error';var nm=esc(a.name||'未命名');return'<div class="account-item" data-accid="'+escAttr(a.id)+'"><span class="acc-dot '+cls+'"></span><span class="acc-name" title="'+(escAttr(a.real_email)||'')+'">'+nm+'</span><span class="acc-del" title="删除" onclick="event.stopPropagation();removeAccount(\''+escAttr(a.id)+'\')">&times;</span></div>';}).join('');}var sd=E('schedDot');sd.className='status-dot '+(state.running?'online':'offline');var sm=state.running?(state.creating?'创建中...':'等待下轮'):'已停止';E('schedLabel').textContent='调度器: '+sm;var bs=E('btnSched');bs.textContent=state.running?'停止调度器':'启动调度器';bs.className='btn btn-sm '+(state.running?'btn-danger':'btn-primary');}function renderDashboard(){var summary={account_count:accounts.length,active_accounts:0,error_accounts:0,total_aliases:0,total_active_aliases:0};accounts.forEach(function(a){if(a.status==='active')summary.active_accounts++;else if(a.status==='error')summary.error_accounts++;summary.total_aliases+=(a.alias_total||0);summary.total_active_aliases+=(a.alias_active||0);});E('summaryCards').innerHTML='<div class="card"><div class="label">账号总数</div><div class="value blue">'+summary.account_count+'</div><div class="sub">活跃 '+summary.active_accounts+' / 异常 '+summary.error_accounts+'</div></div><div class="card"><div class="label">隐私邮箱总数</div><div class="value accent">'+summary.total_aliases+'</div><div class="sub">活跃 '+summary.total_active_aliases+'</div></div><div class="card"><div class="label">累计创建</div><div class="value">'+(state.total_created||0)+'</div><div class="sub">历史总计</div></div><div class="card"><div class="label">今日创建</div><div class="value green">'+(state.today_created||0)+'</div><div class="sub" id="schedInfo">'+esc(state.round_status||'--')+'</div></div>';if(!accounts.length){E('accCards').innerHTML='<div class="empty"><div class="icon"></div>还没有添加账号<br><span style="font-size:12px">点击右上角 "+ 添加账号" 开始</span></div>';}else{E('accCards').innerHTML=accounts.map(function(a){var stCls=a.status==='active'?'ok':'err';var stText=a.status==='active'?'正常':(a.last_error||'异常');var email=a.real_email||'?';return'<div class="acc-card"><div class="acc-header"><div><div class="acc-title">'+esc(a.name||'未命名')+'</div><div class="acc-email">'+esc(email)+'</div></div><span class="status-badge '+stCls+'">'+esc(stText.substring(0,20))+'</span></div><div class="acc-stats"><div class="acc-stat">别名: <span class="n">'+(a.alias_total||0)+'</span></div><div class="acc-stat">活跃: <span class="n" style="color:var(--green)">'+(a.alias_active||0)+'</span></div></div><div class="acc-actions"><button class="btn btn-outline btn-xs" onclick="createForAccount(\''+escAttr(a.id)+'\',1)">创建 1 个</button><button class="btn btn-outline btn-xs" onclick="createForAccount(\''+escAttr(a.id)+'\',5)">创建 5 个</button><button class="btn btn-outline btn-xs" onclick="validateAccount(\''+escAttr(a.id)+'\')">校验</button></div></div>';}).join('');}}function updateEmailFilter(){var sel=E('aliasFilter'),old=sel.value;sel.innerHTML='<option value="all">全部账号 ('+emails.length+')</option>';var byAcc={};emails.forEach(function(e){var ak=e.account_id||'?';byAcc[ak]=(byAcc[ak]||0)+1;});Object.keys(byAcc).forEach(function(ak){var acc=accounts.find(function(x){return x.id===ak});var label=acc?(acc.name||acc.real_email||ak):ak;sel.innerHTML+='<option value="'+escAttr(ak)+'">'+esc(label)+' ('+byAcc[ak]+')</option>';});sel.value=old||'all';}var pickupLinksByEmail={};var pickupLinksLoaded=false;var pickupSelected={};
async function loadPickupLinks(){var d=await apiSlow('/api/pickup-links');if(d.error){toast('取件链接生成失败: '+d.error,true);return}pickupLinksByEmail={};(d.links||[]).forEach(function(x){pickupLinksByEmail[String(x.email||'').toLowerCase()]=x.url});pickupLinksLoaded=true;}
function togglePickupSelected(email,checked){var key=String(email||'').toLowerCase();if(checked)pickupSelected[key]=true;else delete pickupSelected[key];}
function toggleAllPickup(){var checks=document.querySelectorAll('#aliasTableContainer input.pickup-check');var shouldCheck=false;checks.forEach(function(c){if(!c.checked)shouldCheck=true});checks.forEach(function(c){c.checked=shouldCheck;togglePickupSelected(c.dataset.email,shouldCheck);});}
function copyPickup(url){if(!url){toast('取件链接尚未生成',true);return}navigator.clipboard.writeText(url).then(function(){toast('取件链接已复制')});}
function exportSelectedPickupTxt(){var filter=E('aliasFilter').value;var filtered=filter==='all'?emails:emails.filter(function(e){return e.account_id===filter});var lines=filtered.filter(function(e){return pickupSelected[String(e.email||'').toLowerCase()]&&pickupLinksByEmail[String(e.email||'').toLowerCase()]}).map(function(e){return e.email+'----'+pickupLinksByEmail[String(e.email||'').toLowerCase()]});if(!lines.length){toast('请先勾选要导出的邮箱',true);return}var b=new Blob(['\uFEFF'+lines.join('\n')],{type:'text/plain;charset=utf-8'}),a=document.createElement('a');a.href=URL.createObjectURL(b);a.download='icloud_pickup_links_selected.txt';a.click();setTimeout(function(){URL.revokeObjectURL(a.href)},1000);toast('已导出 '+lines.length+' 条取件链接');}
function renderAliasTable(){updateEmailFilter();var filter=E('aliasFilter').value;var filtered=filter==='all'?emails:emails.filter(function(e){return e.account_id===filter});E('emailCount').textContent=filtered.length+' / '+emails.length;var c=E('aliasTableContainer');if(!filtered.length){c.innerHTML='<div class="empty"><div class="icon"></div>暂无创建记录 — 请先生成邮箱</div>';return;}if(!pickupLinksLoaded){c.innerHTML='<div class="empty">正在生成取件链接...</div>';loadPickupLinks().then(renderAliasTable);return;}var h='<table class="email-table"><thead><tr><th style="width:42px"><input type="checkbox" title="全选/取消全选" onclick="toggleAllPickup()"></th><th>#</th><th>邮箱地址</th><th>取件链接</th><th>所属账号</th><th>标签</th><th>状态</th></tr></thead><tbody>';filtered.forEach(function(e,i){var key=String(e.email||'').toLowerCase();var url=pickupLinksByEmail[key]||'';var checked=pickupSelected[key]?' checked':'';var accName=e.account_name||e.account_email||e.account_id||'--';var statusHtml=e.hasOwnProperty('active')?(e.active?'<span style="color:var(--green)">活跃</span>':'<span style="color:var(--red)">停用</span>'):'<span style="color:var(--ink-faint)">--</span>';h+='<tr><td><input class="pickup-check" type="checkbox" data-email="'+escAttr(e.email||'')+'"'+checked+' onchange="togglePickupSelected(this.dataset.email,this.checked)"></td><td style="color:var(--ink-faint);width:40px">'+(i+1)+'</td><td class="mono">'+esc(e.email||'')+'</td><td style="max-width:420px"><span style="font-size:11px;word-break:break-all">'+esc(url||'生成失败')+'</span> '+(url?'<button class="copy-btn" onclick="copyPickup(\''+escAttr(url)+'\')" title="复制取件链接">复制</button>':'')+'</td><td style="font-size:11px">'+esc(accName)+'</td><td style="font-size:11px;color:var(--ink-faint)">'+esc((e.label||'').substring(0,30))+'</td><td>'+statusHtml+'</td></tr>';});h+='</tbody></table>';c.innerHTML=h;}
function renderBatchPanel(){var activeAccs=accounts.filter(function(a){return a.status==='active'});E('batchAccCount').textContent=activeAccs.length+' 个可用账号';var g=E('batchChkGroup');if(!activeAccs.length){g.innerHTML='<span style="font-size:12px;color:var(--ink-faint)">没有活跃账号，请先添加</span>';E('btnBatchExec').disabled=true;}else{g.innerHTML=activeAccs.map(function(a){var email=a.real_email||a.name||a.id;return'<label class="chk-item"><input type="checkbox" value="'+escAttr(a.id)+'" checked> <span title="'+escAttr(email)+'">'+esc(a.name||email.substring(0,20))+'</span></label>';}).join('');E('btnBatchExec').disabled=false;}}async function execBatchCreate(){var checks=document.querySelectorAll('#batchChkGroup input:checked');var ids=[];checks.forEach(function(c){ids.push(c.value)});if(!ids.length){toast('请勾选至少一个账号',true);return}var count=parseInt(E('batchCount').value)||5;var label=E('batchLabel').value.trim();var btn=E('btnBatchExec');btn.disabled=true;btn.textContent='创建中...';var d=await apiSlow('/api/create-batch',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({account_ids:ids,count_per_account:count,label:label})});btn.disabled=false;btn.textContent='开始创建';if(d.ok){toast('创建完成: '+d.total_created+' 个成功');}else{toast('失败: '+(d.error||'?'),true);}refreshAll();}var _inboxBusy=false;var _inboxSse=null;var _inboxStreamMsgs=[];function refreshInbox(force){if(_inboxBusy)return;var accId=E('inboxAccount').value;if(!accId){E('inboxMsgs').innerHTML='<div class="empty"><div class="icon"></div>请先选择账号</div>';return}if(force){_inboxBusy=true;var limit=parseInt(E('inboxLimit').value)||20;E('inboxMsgs').innerHTML='<div class="empty"><div class="icon"></div>强制刷新中...</div>';apiSlow('/api/accounts/'+encodeURIComponent(accId)+'/inbox?limit='+limit+'&force=1').then(function(d){_inboxBusy=false;if(d.error){E('inboxMsgs').innerHTML='<div class="empty"><div class="icon"></div>'+esc(d.error||'连接失败')+'</div>';return;}renderInboxMsgs(d.emails||[],'收件箱 ('+(d.count||0)+' 封)');updateCacheStatus(d.cached);});return;}startInboxStream(accId);}function startInboxStream(accId){if(_inboxSse){_inboxSse.close();_inboxSse=null}_inboxBusy=true;_inboxStreamMsgs=[];E('inboxMsgs').innerHTML='<div class="empty"><div class="icon"></div>正在逐条拉取邮件...</div>';var limit=parseInt(E('inboxLimit').value)||20;_inboxSse=new EventSource('/api/accounts/'+encodeURIComponent(accId)+'/inbox-stream?limit='+limit);_inboxSse.onmessage=function(e){try{var d=JSON.parse(e.data);if(d.type==='start'){}else if(d.type==='email'){_inboxStreamMsgs.push(d.email);renderInboxMsgs(_inboxStreamMsgs,'收件箱 ('+d.count+' 封, 加载中...)');}else if(d.type==='done'){_inboxSse.close();_inboxSse=null;_inboxBusy=false;renderInboxMsgs(_inboxStreamMsgs,'收件箱 ('+d.count+' 封)');}else if(d.type==='error'){_inboxSse.close();_inboxSse=null;_inboxBusy=false;E('inboxMsgs').innerHTML='<div class="empty"><div class="icon"></div>'+esc(d.error||'连接失败')+'</div>';}}catch(_){}};_inboxSse.onerror=function(){if(_inboxSse){_inboxSse.close();_inboxSse=null;}_inboxBusy=false;if(_inboxStreamMsgs.length){renderInboxMsgs(_inboxStreamMsgs,'收件箱 ('+_inboxStreamMsgs.length+' 封, 连接中断)');}else{E('inboxMsgs').innerHTML='<div class="empty"><div class="icon"></div>连接失败</div>';}};}async function searchAliasMail(){if(_inboxBusy)return;_inboxBusy=true;try{var accId=E('inboxAccount').value;var alias=E('aliasSearchInput').value.trim();if(!accId){toast('请先选择账号',true);return}if(!alias){toast('请输入隐私邮箱地址',true);return}E('inboxMsgs').innerHTML='<div class="empty"><div class="icon"></div>查询 '+esc(alias)+' ...</div>';var d=await apiSlow('/api/accounts/'+encodeURIComponent(accId)+'/mail/'+encodeURIComponent(alias)+'?limit=30');if(d.error){E('inboxMsgs').innerHTML='<div class="empty"><div class="icon"></div>'+esc(d.error)+'</div>';return;}renderInboxMsgs(d.emails||[],esc(alias)+' ('+(d.count||0)+' 封)');}finally{_inboxBusy=false;}}async function checkAliasMail(){if(_inboxBusy)return;_inboxBusy=true;try{var accId=E('inboxAccount').value;if(!accId){_inboxBusy=false;E('inboxMsgs').innerHTML='<div class="empty"><div class="icon"></div>请先选择账号</div>';return}E('inboxMsgs').innerHTML='<div class="empty"><div class="icon"></div>正在检查各别名的收件...</div>';var d=await apiSlow('/api/accounts/'+encodeURIComponent(accId)+'/alias-mail');if(d.error){E('inboxMsgs').innerHTML='<div class="empty"><div class="icon"></div>'+esc(d.error||'查询失败')+'</div>';return;}var byAlias=d.by_alias||{};var total=0;var aliasKeys=Object.keys(byAlias);if(!aliasKeys.length){E('inboxMsgs').innerHTML='<div class="empty"><div class="icon"></div>所有隐私邮箱暂无收件</div>';return;}var h='';aliasKeys.forEach(function(alias){var msgs=byAlias[alias]||[];total+=msgs.length;h+='<div style="padding:8px 14px;border-bottom:1px solid var(--rule);font-weight:600;font-size:13px;background:var(--paper-dim)">'+esc(alias)+' ('+msgs.length+' 封)</div>';msgs.forEach(function(m){h+='<div style="padding:6px 20px;border-bottom:1px solid var(--rule);font-size:12px;display:flex;justify-content:space-between;flex-wrap:wrap;gap:4px"><span><strong>'+esc(m.subject||'(无主题)')+'</strong></span><span style="color:var(--ink-soft)">'+esc(m.from||'').substring(0,30)+'</span><span style="color:var(--ink-faint);font-size:11px">'+(m.date||'').substring(0,19)+'</span></div>';});});E('inboxMsgs').innerHTML='<div style="font-size:11px;color:var(--ink-faint);padding:8px 14px;border-bottom:1px solid var(--rule)">共 '+aliasKeys.length+' 个别名收到 '+total+' 封邮件</div>'+h;}finally{_inboxBusy=false;}}function renderInboxMsgs(msgs,title){if(!msgs.length){E('inboxMsgs').innerHTML='<div class="empty"><div class="icon"></div>收件箱为空</div>';return;}var h='<div style="font-size:11px;color:var(--ink-faint);padding:8px 16px;border-bottom:1px solid var(--rule)">'+esc(title)+'</div>';msgs.forEach(function(m,i){var mid=m.id||'m'+i;h+='<div class="email-item" style="border-bottom:1px solid var(--rule);cursor:pointer" onclick="toggleEmail(\''+escAttr(mid)+'\',\''+escAttr(m.id||'')+'\')"><div style="padding:12px 16px;display:flex;justify-content:space-between;align-items:flex-start;gap:12px"><div style="flex:1;min-width:0"><div style="font-weight:600;font-size:14px;margin-bottom:4px">'+esc(m.subject||'(无主题)')+'</div><div style="font-size:12px;color:var(--ink-soft)">'+esc(m.from||'')+'</div><div style="font-size:11px;color:var(--ink-faint);margin-top:2px">To: '+esc((m.to||'').substring(0,50))+'</div></div><div style="font-size:11px;color:var(--ink-faint);white-space:nowrap;text-align:right">'+(m.date||'').substring(0,19)+'</div></div><div id="'+escAttr(mid)+'_body" style="display:none;padding:0 16px 16px;font-size:13px;line-height:1.7;color:var(--ink-soft);white-space:pre-wrap;word-break:break-word;max-height:400px;overflow-y:auto;border-top:1px solid var(--rule)"></div></div>';});E('inboxMsgs').innerHTML=h;}var _expandedEmail=null;async function toggleEmail(domId,msgId){var bodyEl=E(domId+'_body');if(!bodyEl)return;if(_expandedEmail&&_expandedEmail!==domId){var prev=E(_expandedEmail+'_body');if(prev)prev.style.display='none';}if(bodyEl.style.display==='block'){bodyEl.style.display='none';_expandedEmail=null;return;}bodyEl.style.display='block';_expandedEmail=domId;if(bodyEl.textContent.trim()&&bodyEl.textContent!=='加载中...')return;bodyEl.textContent='加载中...';if(!msgId){bodyEl.textContent='(无法获取邮件正文)';return;}var accId=E('inboxAccount').value;if(!accId){bodyEl.textContent='(请先选择账号)';return;}var d=await apiSlow('/api/accounts/'+encodeURIComponent(accId)+'/message/'+encodeURIComponent(msgId));if(!d.ok||!d.message){bodyEl.textContent='(获取失败: '+(d.error||'未知')+')';return;}bodyEl.textContent=d.message.body||'(无正文内容)';}function updateCacheStatus(cached){if(!cached)return;var age=cached.cache_age_sec||0;var txt=age<300?'缓存 '+(age<60?Math.round(age)+'s':Math.round(age/60)+'m')+' 前':'';E('cacheStatus').textContent=cached.inbox_cached?' | '+cached.inbox_cached+' 封已缓存 '+txt:'';}function openInboxSettings(){var accId=E('inboxAccount').value;if(!accId){toast('请先选择账号',true);return}showAppPwdModal(accId);}function showAppPwdModal(accId){var acc=accounts.find(function(a){return a.id===accId});var name=acc?(acc.name||acc.real_email||accId):accId;var icloudEmail='';if(acc&&acc.icloud_email&&(acc.icloud_email.indexOf('@icloud.com')>=0||acc.icloud_email.indexOf('@me.com')>=0||acc.icloud_email.indexOf('@mac.com')>=0)){icloudEmail=acc.icloud_email;}else if(acc&&acc.real_email&&(acc.real_email.indexOf('@icloud.com')>=0||acc.real_email.indexOf('@me.com')>=0)){icloudEmail=acc.real_email;}var hasPwd=acc&&acc.has_app_password;var h='<div class="modal-overlay" id="appPwdModal" onclick="if(event.target===this)closeAppPwdModal()"><div class="modal-box"><h3><i class="diamond"></i> '+(hasPwd?'修改':'设置')+' iCloud 邮箱和应用密码</h3><p>账号: <b>'+esc(name)+'</b> (Apple ID: '+esc(acc?acc.real_email:'')+')<br>在 <a href="appleid.apple.com">appleid.apple.com</a> → 登录与安全 → App 专用密码 生成。</p><label style="font-family:var(--mono);font-size:11px;color:var(--ink-faint);letter-spacing:.2em;text-transform:uppercase">iCloud 邮箱 (IMAP 登录用)</label><input type="text" id="icloudEmailInput" value="'+escAttr(icloudEmail)+'" placeholder="xxx@icloud.com"><label style="font-family:var(--mono);font-size:11px;color:var(--ink-faint);letter-spacing:.2em;text-transform:uppercase">App 专用密码'+ (hasPwd?' (重新输入以更新)':'') +'</label><input type="password" id="appPwdInput" placeholder="xxxx-xxxx-xxxx-xxxx"><div class="modal-actions"><button class="btn btn-outline" onclick="closeAppPwdModal()">取消</button><button class="btn btn-primary" id="btnSetPwd" onclick="setAppPassword(\''+escAttr(accId)+'\')">保存并测试</button></div><div class="modal-msg" id="appPwdMsg"></div></div></div>';document.body.insertAdjacentHTML('beforeend',h);}function closeAppPwdModal(){var m=E('appPwdModal');if(m)m.remove()}async function setAppPassword(accId){var pwd=E('appPwdInput').value.trim();var email=E('icloudEmailInput').value.trim();if(!email){E('appPwdMsg').innerHTML='<span style="color:var(--red)">请输入 iCloud 邮箱</span>';return}if(!pwd){E('appPwdMsg').innerHTML='<span style="color:var(--red)">请输入密码</span>';return}var btn=E('btnSetPwd');btn.disabled=true;btn.textContent='测试中...';var d=await api('/api/accounts/'+encodeURIComponent(accId)+'/app-password',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({app_password:pwd,icloud_email:email})});btn.disabled=false;btn.textContent='保存并测试';if(d.ok){E('appPwdMsg').innerHTML='<span style="color:var(--green)">连接成功! 收件箱 '+d.inbox_count+' 封</span>';var acc=accounts.find(function(a){return a.id===accId});if(acc){acc.has_app_password=true;acc.icloud_email=email;}setTimeout(closeAppPwdModal,1500);updateInboxAccountSelect();}else{E('appPwdMsg').innerHTML='<span style="color:var(--red)">'+esc(d.error||'连接失败')+'</span>';}}async function createForAccount(accId,count){var d=await api('/api/accounts/'+encodeURIComponent(accId)+'/create',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({count:count})});if(d.ok)toast('成功创建 '+d.created+' 个');else toast('失败: '+(d.error||'?'),true);refreshAll();}async function validateAccount(accId){var d=await api('/api/accounts/'+encodeURIComponent(accId)+'/validate',{method:'POST'});if(d.ok)toast('校验通过: '+d.real_email);else toast('校验失败: '+(d.error||'?'),true);refreshAll();}async function removeAccount(accId){if(!confirm('确认删除该账号？'))return;var d=await api('/api/accounts/'+encodeURIComponent(accId)+'/remove',{method:'POST'});if(d.ok)toast('已删除');refreshAll();}async function toggleScheduler(){var act=state.running?'stop':'start';var d=await api('/api/scheduler/'+act,{method:'POST'});if(d.ok)toast(state.running?'调度器已停止':'调度器已启动');refreshAll();}function copyOne(email){navigator.clipboard.writeText(email).then(function(){toast('已复制: '+email)});}function copyAll(){var filter=E('aliasFilter').value;var filtered=filter==='all'?emails:emails.filter(function(e){return e.account_id===filter});navigator.clipboard.writeText(filtered.map(function(e){return e.email}).join('\n')).then(function(){toast('已复制 '+filtered.length+' 个')});}function exportCSV(){var filter=E('aliasFilter').value;var filtered=filter==='all'?emails:emails.filter(function(e){return e.account_id===filter});var csv='email,account,label,active\n'+filtered.map(function(e){return e.email+','+(e.account_name||e.account_id||'')+','+(e.label||'')+','+(e.hasOwnProperty('active')?(e.active?'yes':'no'):'');}).join('\n');var b=new Blob(['\uFEFF'+csv],{type:'text/csv'}),a=document.createElement('a');a.href=URL.createObjectURL(b);a.download='icloud_aliases.csv';a.click();}async function exportPickupTxt(){var d=await apiSlow('/api/pickup-links');if(d.error){toast('生成失败: '+d.error,true);return}var lines=(d.links||[]).map(function(x){return x.email+'----'+x.url});if(!lines.length){toast('没有可导出的隐私邮箱',true);return}var b=new Blob(['\uFEFF'+lines.join('\n')],{type:'text/plain;charset=utf-8'}),a=document.createElement('a');a.href=URL.createObjectURL(b);a.download='icloud_pickup_links.txt';a.click();setTimeout(function(){URL.revokeObjectURL(a.href)},1000);toast('已导出 '+lines.length+' 条取件链接');}function clearLogs(){logs=[];E('logFeed').innerHTML=''}function toast(msg,isErr){var t=E('toast');t.textContent=msg;t.style.background=isErr?'var(--red)':'var(--ink)';t.style.color='var(--paper)';t.classList.add('show');setTimeout(function(){t.classList.remove('show')},2200);}function connectSSE(){if(sseConn){sseConn.close();sseConn=null}sseConn=new EventSource('/api/log-stream');sseConn.onmessage=function(e){try{var entry=JSON.parse(e.data);logs.push(entry);if(logs.length>500)logs=logs.slice(-500);if(curTab==='logs')renderLogs();if(entry.msg&&entry.msg.indexOf('创建')>=0)refreshLight();}catch(_){}};sseConn.onerror=function(){sseConn.close();sseConn=null;setTimeout(connectSSE,5000)};}function renderLogs(){var f=E('logFeed');f.innerHTML=logs.map(function(l){return'<div class="log-line '+l.level+'"><span class="log-time">'+esc(l.time)+'</span>'+esc(l.msg)+'</div>';}).join('\n');f.scrollTop=f.scrollHeight;}function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;')}function escAttr(s){return String(s).replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/'/g,'&#39;')}function showAddAccountModal(){var h='<div class="modal-overlay" id="addAccModal" onclick="if(event.target===this)closeAddAccModal()"><div class="modal-box"><h3><i class="diamond"></i> 导入 iCloud Cookie</h3><p>Chrome 安装 <b>Cookie Editor</b> 扩展 → 登录 icloud.com → 导出 <b>Header String</b> 粘贴即可。<br>也支持 JSON 格式: <code>{"name1":"value1"}</code></p><input type="text" id="accNameInput" placeholder="账号名称 (如: 主号)"><textarea id="cookieInput" placeholder="粘贴 Cookie，支持 Header String 或 JSON 格式"></textarea><div class="modal-actions"><button class="btn btn-outline" onclick="closeAddAccModal()">取消</button><button class="btn btn-primary" id="btnAddAccount" onclick="addAccount()">添加并校验</button></div><div class="modal-msg" id="addAccMsg"></div></div></div>';document.body.insertAdjacentHTML('beforeend',h);}function closeAddAccModal(){var m=E('addAccModal');if(m)m.remove()}async function addAccount(){var name=E('accNameInput').value.trim()||'未命名账号';var cookies=E('cookieInput').value.trim();if(!cookies){E('addAccMsg').innerHTML='<span style="color:var(--red)">请粘贴 Cookie</span>';return}var btn=E('btnAddAccount');btn.disabled=true;btn.textContent='校验中...';var d=await api('/api/accounts/add',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:name,cookie_input:cookies})});btn.disabled=false;btn.textContent='添加并校验';if(d.ok){E('addAccMsg').innerHTML='<span style="color:var(--green)">添加成功! '+esc(d.real_email||'')+' ('+(d.alias_total||0)+' 别名)</span>';setTimeout(closeAddAccModal,1500);refreshAll();}else{E('addAccMsg').innerHTML='<span style="color:var(--red)">'+esc(d.error||'失败')+'</span>';}}function renderDocs(){var h='<div style="max-width:900px"><p style="color:var(--ink-soft);margin-bottom:18px">所有接口返回 JSON。Base URL: <code>http://127.0.0.1:5050</code></p>';var sections=[{title:'账号管理',items:[{method:'GET',path:'/api/accounts',desc:'列出所有账号（脱敏，不含 cookie）'},{method:'POST',path:'/api/accounts/add',desc:'添加账号',body:'{"name":"账号名","cookie_input":"name1=value1; name2=value2"}'},{method:'POST',path:'/api/accounts/{id}/remove',desc:'删除账号'},{method:'POST',path:'/api/accounts/{id}/validate',desc:'重新校验账号会话'}]},{title:'状态',items:[{method:'GET',path:'/api/state',desc:'全局状态 + 账号汇总'}]},{title:'别名 / 邮箱',items:[{method:'GET',path:'/api/aliases',desc:'所有账号的别名列表（iCloud API 实时拉取）'},{method:'GET',path:'/api/emails',desc:'本地创建记录（latest_emails.txt，永远可用）'},{method:'POST',path:'/api/accounts/{id}/create',desc:'为指定账号创建别名',body:'{"count":5,"label":"可选标签"}'},{method:'POST',path:'/api/create-batch',desc:'跨账号批量创建',body:'{"account_ids":["id1","id2"],"count_per_account":5}'}]},{title:'收件箱 (IMAP)',items:[{method:'GET',path:'/api/accounts/{id}/inbox?limit=20&force=1',desc:'查收件箱。force=1 跳过缓存强制从 IMAP 拉取'},{method:'GET',path:'/api/accounts/{id}/alias-mail?force=1',desc:'查所有隐私别名的收件情况'},{method:'GET',path:'/api/accounts/{id}/mail/{别名邮箱}',desc:'查指定隐私邮箱的收件'},{method:'POST',path:'/api/accounts/{id}/app-password',desc:'设置 App 专用密码并测试 IMAP',body:'{"app_password":"xxxx-xxxx-xxxx-xxxx","icloud_email":"xxx@icloud.com"}'}]},{title:'快捷入口',items:[{method:'GET',path:'/api/mail?email=user@icloud.com',desc:'按主邮箱查所有别名收件'},{method:'GET',path:'/api/mail?email=...&alias=xxx@icloud.com',desc:'按主邮箱查指定别名收件'}]},{title:'调度器',items:[{method:'POST',path:'/api/scheduler/start',desc:'启动定时调度器'},{method:'POST',path:'/api/scheduler/stop',desc:'停止调度器'}]},{title:'实时日志',items:[{method:'GET',path:'/api/log-stream',desc:'SSE 实时日志流（EventSource）'}]}];sections.forEach(function(sec){h+='<div style="margin-bottom:24px"><div style="font-size:12px;color:var(--ink-faint);letter-spacing:.2em;text-transform:uppercase;margin-bottom:10px;border-bottom:1px solid var(--rule);padding-bottom:4px">'+esc(sec.title)+'</div>';sec.items.forEach(function(item){var methodColor=item.method==='GET'?'var(--green)':item.method==='POST'?'var(--red)':'var(--ink-soft)';h+='<div style="margin-bottom:10px;padding:10px 14px;background:var(--paper-dim)"><span style="font-weight:700;color:'+methodColor+';margin-right:12px;font-size:11px">'+item.method+'</span><code style="font-size:12px">'+esc(item.path)+'</code><div style="color:var(--ink-soft);font-size:12px;margin-top:4px">'+esc(item.desc)+'</div>';if(item.body){h+='<div style="margin-top:6px"><code style="font-size:11px;color:var(--ink-faint);background:var(--paper);padding:3px 8px;display:inline-block">'+esc(item.body)+'</code></div>';}h+='</div>';});h+='</div>';});h+='<div style="margin-top:32px;padding-top:16px;border-top:1px solid var(--rule-strong);font-size:12px;color:var(--ink-faint)">缓存策略：收件箱接口默认 5 分钟内读本地缓存 (<code>results/mail_cache.json</code>)，首次拉取后终身存储。传 <code>?force=1</code> 跳过缓存从 IMAP 增量拉取。<br>Cookie 导入：支持 Header String (<code>name=value; ...</code>) 和 JSON (<code>{"name":"value"}</code>) 两种格式。</div></div>';E('docsContent').innerHTML=h;}function updateInboxAccountSelect(){var sel=E('inboxAccount'),old=sel.value;sel.innerHTML='<option value="">-- 选择账号 --</option>';accounts.forEach(function(a){var hasPwd=a.has_app_password?' [已设]':' [未设密码]';var imapEmail=a.icloud_email||a.real_email||'';sel.innerHTML+='<option value="'+escAttr(a.id)+'">'+esc((a.name||a.real_email||a.id).substring(0,20))+' | '+esc(imapEmail.substring(0,25))+' '+hasPwd+'</option>';});sel.value=old||'';}refreshAll();connectSSE();setInterval(refreshLight,10000);setInterval(refreshAll,30000);</script></body></html>"""

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
    if not cookie_input: return jsonify({"ok":False,"error":"请提供 cookie_input"})
    try:
        account = _account_mgr.add_account(name, cookie_input)
        _emit_log("info",f"添加账号: {account.get('name','')} ({account.get('real_email','?')})")
        return jsonify({"ok":True,"id":account["id"],"name":account["name"],"real_email":account.get("real_email",""),"alias_total":account.get("alias_total",0),"alias_active":account.get("alias_active",0),"status":account.get("status","")})
    except ValueError as e: return jsonify({"ok":False,"error":str(e)})
    except Exception as e: return jsonify({"ok":False,"error":str(e)})

@app.route("/api/accounts/<acc_id>/remove", methods=["POST"])
def api_remove_account(acc_id):
    ok = _account_mgr.remove_account(acc_id)
    return jsonify({"ok":ok})

@app.route("/api/accounts/<acc_id>/validate", methods=["POST"])
def api_validate_account(acc_id):
    try:
        account = _account_mgr.validate_account(acc_id)
        return jsonify({"ok":True,"real_email":account.get("real_email",""),"alias_total":account.get("alias_total",0)})
    except Exception as e: return jsonify({"ok":False,"error":str(e)})

@app.route("/api/accounts/<acc_id>/create", methods=["POST"])
def api_create_for_account(acc_id):
    data = request.get_json() or {}
    count = min(int(data.get("count",1)),50)
    label = data.get("label","")
    _update_state(creating=True)
    _emit_log("info",f"手动创建: 账号 {acc_id} x{count}")
    try:
        results = _account_mgr.create_aliases_for_account(acc_id, count, label)
        created = [r["email"] for r in results if r.get("ok")]
        errors = [r["error"] for r in results if not r.get("ok")]
        _update_state(creating=False)
        _increment_state(today_created=len(created), total_created=len(created))
        if created: _emit_log("success",f"创建完成: {len(created)} 个")
        return jsonify({"ok":len(created)>0,"emails":created,"created":len(created),"errors":len(errors),"error":errors[0] if errors else None})
    except Exception as e:
        _update_state(creating=False)
        return jsonify({"ok":False,"error":str(e)})

@app.route("/api/create-batch", methods=["POST"])
def api_create_batch():
    data = request.get_json() or {}
    account_ids = data.get("account_ids",[])
    count = min(int(data.get("count_per_account",5)),20)
    label = data.get("label","")
    interval = float(data.get("interval",3.0))
    if not account_ids: return jsonify({"ok":False,"error":"请选择至少一个账号"})
    _update_state(creating=True)
    _emit_log("info",f"批量创建: {len(account_ids)} 个账号 x{count}")
    try:
        all_results = _account_mgr.create_aliases_batch(account_ids, count, interval, label)
        total_created = sum(sum(1 for r in results if r.get("ok")) for results in all_results.values())
        total_errors = sum(sum(1 for r in results if not r.get("ok")) for results in all_results.values())
        _update_state(creating=False)
        _increment_state(today_created=total_created, total_created=total_created)
        _emit_log("success",f"批量完成: {total_created} 成功 / {total_errors} 失败")
        return jsonify({"ok":True,"total_created":total_created,"total_errors":total_errors,"results":{acc_id:[{"email":r.get("email"),"ok":r.get("ok"),"error":r.get("error")} for r in results] for acc_id,results in all_results.items()}})
    except Exception as e:
        _update_state(creating=False)
        return jsonify({"ok":False,"error":str(e)})

@app.route("/api/accounts/<acc_id>/app-password", methods=["POST"])
def api_set_app_password(acc_id):
    data = request.get_json() or {}
    pwd = data.get("app_password","").strip()
    icloud_email = data.get("icloud_email","").strip()
    if not pwd: return jsonify({"ok":False,"error":"密码不能为空"})
    try:
        _account_mgr.set_app_password(acc_id, pwd)
        if icloud_email: _account_mgr.update_account(acc_id, icloud_email=icloud_email)
        result = _account_mgr.test_imap_connection(acc_id)
        return jsonify(result)
    except Exception as e:
        # Credentials are persisted before the network test. Keep them saved when
        # IMAP is temporarily slow or unavailable so the user can retry inbox sync.
        return jsonify({"ok":False,"saved":True,"error":str(e)})

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
        aliases = _account_mgr.get_all_aliases()
        return jsonify({"aliases":aliases,"count":len(aliases)})
    except Exception as e: return jsonify({"aliases":[],"count":0,"error":str(e)})

@app.route("/api/pickup-links")
def api_pickup_links():
    """Return opaque pickup URLs; the email address is never embedded in them."""
    try:
        aliases = _account_mgr.get_all_aliases()
        links = _pickup_store.list_for_aliases(aliases)
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
<script>const token=__TOKEN__;let busy=false;let currentId='';const bodies={};const statusEl=document.getElementById('status');function status(text,hold){statusEl.textContent=text;statusEl.classList.add('show');if(!hold)setTimeout(()=>statusEl.classList.remove('show'),1800)}function showEmail(data){const box=document.getElementById('mailbox');box.innerHTML='';if(data.html){const frame=document.createElement('iframe');frame.className='mail-frame';frame.setAttribute('sandbox','allow-same-origin allow-popups');frame.setAttribute('referrerpolicy','no-referrer');const csp=`<meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src https: http: data: cid:; style-src 'unsafe-inline' https: http:; font-src https: http: data:;">`;frame.srcdoc=csp+data.html;frame.onload=function(){try{frame.style.height=Math.max(frame.contentDocument.documentElement.scrollHeight+20,window.innerHeight)+'px'}catch(e){}};box.appendChild(frame)}else{const div=document.createElement('div');div.className='state';div.style.whiteSpace='pre-wrap';div.style.alignItems='flex-start';div.style.justifyContent='flex-start';div.style.padding='32px';div.textContent=data.body||'(无正文内容)';box.appendChild(div)}}async function fetchMessages(force){const r=await fetch('/pickup/'+encodeURIComponent(token)+'/messages'+(force?'?force=1':''),{cache:'no-store'});const d=await r.json();if(!r.ok)throw new Error(d.error||'读取失败');return d}async function openLatest(m){if(!m||!m.id){document.getElementById('mailbox').innerHTML='<div class="state">暂无邮件。</div>';return}if(currentId===String(m.id)&&bodies[m.id])return;currentId=String(m.id);status('正在打开最新邮件...',true);for(let n=0;n<25;n++){try{const r=await fetch('/pickup/'+encodeURIComponent(token)+'/message/'+encodeURIComponent(m.id),{cache:'no-store'});const d=await r.json();if(r.ok&&d.ready){bodies[m.id]=d.message||{};showEmail(bodies[m.id]);status('最新邮件已打开',false);return}}catch(e){}await new Promise(r=>setTimeout(r,800))}status('邮件打开较慢，稍后自动重试',false)}async function sync(){if(busy)return;busy=true;try{status('正在获取最新邮件...',true);let d=await fetchMessages(true);let list=(d.emails||[]).slice().reverse();if(list.length)await openLatest(list[0]);if(d.refreshing){for(let i=0;i<25;i++){await new Promise(r=>setTimeout(r,800));d=await fetchMessages(false);if(!d.refreshing){list=(d.emails||[]).slice().reverse();await openLatest(list[0]);break}}}if(!list.length)document.getElementById('mailbox').innerHTML='<div class="state">暂无邮件。</div>';status('已是最新邮件',false)}catch(e){status('读取失败，稍后自动重试',false)}finally{busy=false}}sync();setInterval(sync,15000);</script></body></html>"""
    html = html.replace("__ALIAS__", alias_html).replace("__TOKEN__", token_js)
    return Response(html, mimetype="text/html", headers={"Cache-Control": "no-store"})

def _refresh_pickup_account(account_id):
    global _pickup_pending
    try:
        # One IMAP sync per iCloud account, then serve all aliases from cache.
        _account_mgr.check_all_aliases_mail(account_id, limit_per=20, days=30, force=True)
    finally:
        with _pickup_refresh_lock:
            _pickup_last_account_refresh[account_id] = time.time()
            _pickup_refreshing_accounts.discard(account_id)
            _pickup_pending = max(0, _pickup_pending - 1)

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

def _refresh_pickup_body(account_id, msg_id):
    global _pickup_pending
    key = (account_id, msg_id)
    try:
        mail = _account_mgr.get_mail_client(account_id)
        try:
            full = mail.fetch_full(msg_id.encode() if isinstance(msg_id, str) else msg_id) or {}
        finally:
            mail.disconnect()
        full["verification_code"] = _pickup_code(full.get("subject", ""), full.get("body", ""))
        full["clean_body"] = _clean_pickup_body(full.get("body", ""))
        with _pickup_refresh_lock:
            _pickup_body_cache[key] = full
    except Exception:
        pass
    finally:
        with _pickup_refresh_lock:
            _pickup_body_refreshing.discard(key)
            _pickup_pending = max(0, _pickup_pending - 1)

@app.route("/pickup/<token>/messages")
def pickup_messages(token):
    global _pickup_pending
    item = _pickup_store.get_by_token(token)
    if not item:
        return jsonify({"error": "取件链接无效或已撤销"}), 404
    cached = _account_mgr._cache.get_alias_mail(item["account_id"], item["alias_email"])[-20:]
    now = time.time()
    force = request.args.get("force", "0") == "1"
    account_id = item["account_id"]
    with _pickup_refresh_lock:
        refreshing = account_id in _pickup_refreshing_accounts
        if not refreshing and (force or now - _pickup_last_account_refresh.get(account_id, 0) >= 12) and _pickup_pending < _PICKUP_MAX_PENDING:
            _pickup_refreshing_accounts.add(account_id)
            _pickup_pending += 1
            refreshing = True
            _pickup_executor.submit(_refresh_pickup_account, account_id)
    return jsonify({"emails": cached, "count": len(cached), "refreshing": refreshing}), 200, {"Cache-Control": "no-store"}

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
        if key in _pickup_body_cache:
            return jsonify({"ready": True, "message": _pickup_body_cache[key]}), 200, {"Cache-Control": "no-store"}
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
    f = RESULTS_DIR / "latest_emails.txt"
    if f.exists():
        lines = f.read_text(encoding="utf-8").strip().split("\n")
        if limit>0 and len(lines)>limit: lines = lines[-limit:]
        for line in lines:
            line = line.strip()
            if line and "@" in line:
                parts = line.split("\t")
                emails.append({"email":parts[0],"account_id":parts[1] if len(parts)>1 else "","created_at":""})
    emails.reverse()
    return jsonify({"emails":emails,"count":len(emails)})

@app.route("/api/scheduler/start", methods=["POST"])
def api_scheduler_start():
    global _scheduler_thread, _stop_event
    _stop_event.clear()
    _scheduler_thread = threading.Thread(target=_scheduler_loop, daemon=True)
    _scheduler_thread.start()
    _update_state(running=True)
    return jsonify({"ok":True})

@app.route("/api/scheduler/stop", methods=["POST"])
def api_scheduler_stop():
    _stop_event.set()
    return jsonify({"ok":True})

@app.route("/api/log-stream")
def api_log_stream():
    def generate():
        while True:
            try: entry = _log_queue.get(timeout=30); yield f"data: {json.dumps(entry,ensure_ascii=False)}\n\n"
            except queue.Empty: yield ": heartbeat\n\n"
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
    if args.scheduler:
        global _scheduler_thread, _stop_event
        _stop_event.clear()
        _scheduler_thread = threading.Thread(target=_scheduler_loop, daemon=True)
        _scheduler_thread.start()
        _update_state(running=True)
        print("[+] Scheduler auto-started")
    def _shutdown(sig,frame): print("\n[*] Shutting down..."); _stop_event.set(); os._exit(0)
    _signal.signal(_signal.SIGINT, _shutdown)
    _signal.signal(_signal.SIGTERM, _shutdown)
    try:
        from waitress import serve
        print(f"\n  Production → http://{args.host}:{args.port}\n")
        serve(app, host=args.host, port=args.port, threads=32)
    except ImportError:
        print(f"\n  Dev server → http://{args.host}:{args.port}\n")
        app.run(host=args.host, port=args.port, debug=False, threaded=True)

if __name__=="__main__": main()
