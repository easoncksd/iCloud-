"""Durable creation controls. Stores no cookies, passwords or response bodies."""
import json
import os
import threading
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path


def classify(error):
    text = str(error).lower()
    if any(s in text for s in ('http 401', 'http 403', 'http 421', 'trusttokens',
                               'authentication', 'cookie', '会话校验失败', 'account locked', 'account disabled')):
        return 'auth'
    if any(s in text for s in ('429', 'rate limit', 'too many requests', 'throttle',
                               'right now', 'try again later', 'temporarily', '操作频繁', '稍后再试')):
        return 'throttle'
    if any(s in text for s in ('quota', 'address limit', 'maximum number of addresses',
                               'too many addresses', 'reached the limit of addresses', '达到上限', '超过上限')):
        return 'quota'
    if any(s in text for s in ('timeout', 'timed out', 'connection', '超时', '连接失败', 'http 5')):
        return 'network'
    return 'unknown'


class CreationGuard:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.RLock()
        self.active = set()
        self.queued = set()
        if self.path.exists():
            self.data = json.loads(self.path.read_text(encoding='utf-8'))
            if not isinstance(self.data.get('accounts'), dict):
                raise ValueError('创建保护记录损坏，请检查后恢复')
        else:
            self.data = {'accounts': {}, 'events': [], 'global_paused': False,
                         'settings': {'concurrency': 3, 'daily_limit': 50}}
        # Older releases used a cross-account circuit breaker. Accounts now isolate
        # their failures; retain the compatibility field without enforcing it.
        self.data['global_paused'] = False

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix('.tmp')
        with tmp.open('w', encoding='utf-8') as f:
            json.dump(self.data, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)

    def account(self, acc_id):
        entry = self.data['accounts'].setdefault(acc_id, {})
        day = datetime.now(timezone(timedelta(hours=8))).date().isoformat()
        if entry.get('day') != day:
            entry.update(day=day, attempts=0, successes=0)
        return entry

    def result(self, kind, message, **extra):
        return dict(ok=False, error=message, error_kind=kind,
                    retryable=kind == 'throttle', limited=kind in ('throttle', 'quota'), **extra)

    def check(self, acc_id, account):
        with self.lock:
            entry = self.account(acc_id)
            if account.get('status') not in (None, 'active'):
                return self.result('blocked', '登录状态异常，已禁止创建')
            if account.get('mail_status') == 'auth_failed' or account.get('mail_sync_paused'):
                return self.result('blocked', '收信异常账号已禁止创建，请先确认账号恢复')
            if entry.get('blocked'):
                return self.result('blocked', '创建已暂停：' + entry['blocked'] + '，请检查后解除')
            left = entry.get('retry_at', 0) - time.time()
            if left > 0:
                return self.result('throttle', '账号正在冷却，等待后自动继续', retry_after_seconds=left)
            if not entry.get('pending') and entry['attempts'] >= self.data['settings']['daily_limit']:
                return self.result('quota', '已达到项目每日创建尝试上限（北京时间零点重置）')

    def claim(self, acc_id):
        with self.lock:
            if acc_id in self.active:
                return False
            if len(self.active) >= self.data['settings']['concurrency']:
                return False
            self.active.add(acc_id)
            return True

    def release(self, acc_id):
        with self.lock:
            self.active.discard(acc_id)
            self.queued.discard(acc_id)

    def attempt(self, acc_id):
        with self.lock:
            e = self.account(acc_id)
            e['attempts'] += 1
            e['last_attempt_at'] = time.time()
            self.save()

    def pending(self, acc_id, email, task_id=None):
        with self.lock:
            self.account(acc_id).update(pending=email, pending_task=task_id)
            self.save()

    def success(self, acc_id, email=None, task_id=None):
        with self.lock:
            before = json.loads(json.dumps(self.data))
            e = self.account(acc_id)
            task_id = e.get('pending_task') or task_id
            if not email or e.get('last_success_email') != email:
                e['successes'] += 1
                if task_id:
                    counts = self.data.setdefault('task_successes', {}).setdefault(task_id, {})
                    counts[acc_id] = counts.get(acc_id, 0) + 1
            e.update(pending=None, pending_task=None, retry_at=0, blocked=None,
                     last_success_at=time.time(), last_success_email=email)
            try:
                self.save()
            except Exception:
                self.data = before
                raise

    def task_count(self, task_id, acc_id):
        with self.lock:
            return self.data.get('task_successes', {}).get(task_id, {}).get(acc_id, 0)

    def failure(self, acc_id, error):
        kind = classify(error)
        with self.lock:
            e = self.account(acc_id)
            now = time.time()
            e.update(last_error_kind=kind, last_error_at=now)
            e.setdefault('first_error_at', now)
            # An unresolved reserve must never produce a second candidate automatically.
            if e.get('pending') and kind in ('network', 'unknown'):
                kind = 'uncertain'
            else:
                e['pending'] = None
            if kind == 'throttle':
                e['retry_at'] = now + 1800
            else:
                e['blocked'] = kind
            events = self.data['events']
            events.append({'account_id': acc_id, 'kind': kind, 'at': now})
            self.data['events'] = events[-500:]
            recent = {x['account_id'] for x in events if x['kind'] == kind
                      and x['at'] >= now - 600 and x['at'] > self.data.get('circuit_reset_at', 0)}
            if kind in ('auth', 'network', 'uncertain', 'unknown') and len(recent) >= 3:
                self.data['multi_account_alert'] = {'kind': kind, 'accounts': len(recent), 'at': now}
            self.save()
            messages = {'throttle': 'Apple 临时限流，固定等待 30 分钟',
                        'auth': '登录或权限异常，已停止创建', 'quota': '邮箱额度限制，已停止创建',
                        'network': '网络异常，已停止创建，请检查后恢复',
                        'uncertain': '保留结果不明确，已停止；恢复时先核对云端地址',
                        'unknown': '未识别的创建异常，已停止，请检查'}
            return self.result(kind, messages[kind], **({'retry_after_seconds': 1800} if kind == 'throttle' else {}))

    def snapshot(self):
        with self.lock:
            return json.loads(json.dumps(self.data))

    def configure(self, concurrency, daily_limit):
        if type(concurrency) is not int or not 1 <= concurrency <= 10:
            raise ValueError('并发必须为 1 到 10 的整数')
        if type(daily_limit) is not int or not 1 <= daily_limit <= 750:
            raise ValueError('每日尝试上限必须为 1 到 750 的整数')
        with self.lock:
            self.data['settings'] = dict(concurrency=concurrency, daily_limit=daily_limit)
            self.save()

    def unblock(self, acc_id=None):
        with self.lock:
            if acc_id:
                # Preserve pending reconciliation and the cooldown deadline.
                self.account(acc_id)['blocked'] = None
            else:
                self.data['global_paused'] = False
                self.data['multi_account_alert'] = None
                self.data['circuit_reset_at'] = time.time()
            self.save()
