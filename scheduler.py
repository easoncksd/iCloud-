#!/usr/bin/env python3
"""
iCloud Hide My Email — 定时自动创建调度器
============================================
挂在服务器上持续运行。

用法:
    python scheduler.py
    python scheduler.py -d                       # 后台运行
    python scheduler.py --interval 5

信号:
    Ctrl+C  优雅退出
    SIGTERM 同上
"""

import sys, os, json, time, signal, logging, argparse
import random
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path
from typing import Optional, Dict, List

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path: sys.path.insert(0, str(HERE))
from account_manager import AccountManager

LOG_DIR = HERE / "logs"
RESULT_DIR = HERE / "results"
STATE_FILE = HERE / "scheduler_state.json"

def setup_logging(verbose: bool = True) -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True); RESULT_DIR.mkdir(parents=True, exist_ok=True)
    log_date = datetime.now().strftime("%Y%m%d"); log_file = LOG_DIR / f"scheduler_{log_date}.log"
    logger = logging.getLogger("icloud_scheduler"); logger.setLevel(logging.DEBUG)
    fh = logging.FileHandler(str(log_file), encoding="utf-8"); fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s | %(levelname)-7s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
    ch = logging.StreamHandler(sys.stdout); ch.setLevel(logging.INFO if verbose else logging.WARNING)
    ch.setFormatter(logging.Formatter("%(asctime)s  %(message)s", datefmt="%H:%M:%S"))
    logger.addHandler(fh); logger.addHandler(ch)
    return logger

def load_state() -> Dict:
    if STATE_FILE.exists():
        try: return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except: pass
    return {"total_created": 0, "rounds": [], "last_error": None}

def save_state(state: Dict):
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")

class CreateRound:
    def __init__(self):
        self.start_time = datetime.now(); self.end_time: Optional[datetime] = None
        self.created: List[str] = []; self.created_by_account: Dict[str, int] = {}
        self.errors: List[Dict] = []; self.hit_limit = False
        self.fatal_error: Optional[str] = None

LIMIT_KEYWORDS = ["limit","exceeded","maximum","too many","quota","cannot create","unavailable","try again later","rate limit","429","已达上限","超过限制"]

def is_limit_error(error: str) -> bool:
    return any(kw in error.lower() for kw in LIMIT_KEYWORDS)

def run_one_round(mgr: AccountManager, logger: logging.Logger, label: str = "", interval_sec: float = 3.0) -> CreateRound:
    import random as _random
    round_result = CreateRound()
    accounts = mgr.list_accounts()
    active_accounts = [a for a in accounts if a.get("status") == "active"]
    if not active_accounts: logger.warning("no active accounts"); round_result.end_time = datetime.now(); return round_result
    logger.info(f"new round ({len(active_accounts)} accounts, 3-5 random/account)")
    for i, account in enumerate(active_accounts):
        acc_id = account["id"]; acc_name = account.get("name", acc_id)
        target_count = _random.randint(3, 5)
        logger.info(f"[{i+1}/{len(active_accounts)}] {acc_name} target {target_count}")
        try: mgr.get_aliases_for_account(acc_id); time.sleep(_random.uniform(2,5))
        except: pass
        created = 0; errors = 0
        while created < target_count and errors < 3:
            try:
                results = mgr.create_aliases_for_account(acc_id, count=1, label=label or f"{acc_name} {datetime.now().strftime('%m%d%H%M')}-{created+1}")
                if results and len(results) > 0:
                    r = results[0]
                    if r.get("ok") and r.get("email"):
                        created += 1; round_result.created.append(r["email"])
                        round_result.created_by_account[acc_id] = round_result.created_by_account.get(acc_id, 0) + 1
                        errors = 0; logger.info(f"created ({created}/{target_count}) {r['email']}")
                        time.sleep(_random.uniform(10, 30))
                    else:
                        errors += 1
                        if is_limit_error(r.get("error","")): logger.info(f"limit hit: {r.get('error','')[:60]}"); break
                else: errors += 1
            except Exception as e:
                err_str = str(e); errors += 1
                if is_limit_error(err_str): logger.info(f"limit hit: {err_str[:60]}"); break
                if any(kw in err_str.lower() for kw in ["401","403","cookie","session","validate"]):
                    logger.error(f"fatal {acc_name}: {err_str[:200]}"); mgr.update_account(acc_id, status="error", last_error=err_str[:300]); round_result.fatal_error = err_str; break
        if i < len(active_accounts) - 1: time.sleep(_random.uniform(120, 300))
    round_result.hit_limit = any(is_limit_error(e.get("error","")) for e in round_result.errors)
    round_result.end_time = datetime.now()
    summary = ", ".join(f"{mgr.accounts[aid].get('name',aid)[:12]}: {n}" for aid, n in round_result.created_by_account.items()) if round_result.created_by_account else "0"
    logger.info(f"round end: {len(round_result.created)} created ({summary}), {len(round_result.errors)} errors")
    return round_result

def save_round_result(round_result: CreateRound, logger: logging.Logger):
    ts = round_result.start_time.strftime("%Y%m%d_%H%M%S"); result_file = RESULT_DIR / f"round_{ts}.json"
    result_file.write_text(json.dumps({"start_time":round_result.start_time.isoformat(),"end_time":round_result.end_time.isoformat() if round_result.end_time else None,"created_count":len(round_result.created),"created":round_result.created,"errors":round_result.errors,"hit_limit":round_result.hit_limit,"fatal_error":round_result.fatal_error}, indent=2, ensure_ascii=False), encoding="utf-8")

def wait_interval(logger: logging.Logger, seconds: float = 3600):
    target = datetime.now() + timedelta(seconds=seconds)
    logger.info(f"next round: {target.strftime('%H:%M:%S')} ({seconds/60:.0f}min)")
    while True:
        rem = (target - datetime.now()).total_seconds()
        if rem <= 0: break
        time.sleep(min(rem, 30))

class Scheduler:
    def __init__(self, mgr: AccountManager, label_prefix: str = "", interval_sec: float = 3.0, verbose: bool = True):
        self.mgr = mgr; self.label_prefix = label_prefix; self.interval_sec = interval_sec
        self.verbose = verbose; self.logger = setup_logging(verbose); self._running = True; self._state = load_state()
        signal.signal(signal.SIGINT, self._handle_signal); signal.signal(signal.SIGTERM, self._handle_signal)
    def _handle_signal(self, signum, frame): self.logger.info(f"signal {signum}"); self._running = False
    def run(self):
        summary = self.mgr.get_summary()
        self.logger.info(f"scheduler started: {summary['account_count']} accounts ({summary['active_accounts']} active)")
        self.logger.info(f"mode: BJ 7-20h, 60-90min interval, 3-5/account")
        round_num = 0
        while self._running:
            now = datetime.now(ZoneInfo('Asia/Shanghai'))
            if now.hour < 7 or now.hour >= 20:
                self.logger.info(f"outside window BJ {now.hour}:00")
                wait_interval(self.logger, 1800)
                continue
            round_num += 1
            label = f"{self.label_prefix}R{round_num} {now.strftime('%m%d%H%M')}" if self.label_prefix else f"R{round_num} {now.strftime('%m%d%H%M')}"
            round_result = run_one_round(self.mgr, self.logger, label=label, interval_sec=self.interval_sec)
            save_round_result(round_result, self.logger)
            self._state["total_created"] = self._state.get("total_created",0) + len(round_result.created)
            self._state["rounds"].append({"round":round_num,"time":now.isoformat(),"created":len(round_result.created),"by_account":round_result.created_by_account,"hit_limit":round_result.hit_limit})
            if len(self._state["rounds"]) > 200: self._state["rounds"] = self._state["rounds"][-200:]
            self._state["last_error"] = round_result.fatal_error; save_state(self._state)
            if round_result.fatal_error: self.logger.error(f"fatal exit: {round_result.fatal_error[:200]}"); self._running = False; break
            if not self._running: break
            wait_interval(self.logger, random.randint(3600, 5400))
        self.logger.info(f"scheduler stopped. total: {self._state.get('total_created',0)}"); save_state(self._state)

def main():
    parser = argparse.ArgumentParser(description="iCloud HME 多账号定时调度器")
    parser.add_argument("--interval","-i",type=float,default=3.0,help="account interval (default 3s)")
    parser.add_argument("--label",type=str,default="",help="label prefix")
    parser.add_argument("--quiet","-q",action="store_true",help="less output")
    parser.add_argument("--daemon","-d",action="store_true",help="daemon (not Windows)")
    args = parser.parse_args()
    mgr = AccountManager(); summary = mgr.get_summary()
    if summary["active_accounts"] == 0: print("no active accounts"); sys.exit(1)
    print(f"loaded {summary['account_count']} accounts ({summary['active_accounts']} active)")
    if args.daemon:
        if sys.platform == "win32": print("--daemon not supported on Windows"); sys.exit(1)
        pid = os.fork()
        if pid > 0: print(f"daemon PID={pid}"); sys.exit(0)
        os.setsid(); os.umask(0)
    scheduler = Scheduler(mgr=mgr, label_prefix=args.label, interval_sec=args.interval, verbose=not args.quiet)
    try: scheduler.run()
    except KeyboardInterrupt: print("interrupted")

if __name__ == "__main__": main()