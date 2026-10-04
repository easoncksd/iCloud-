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
    if any(s in text for s in (
        'timeout', 'timed out', 'connection', 'connection reset',
        'connection refused', 'connection aborted', 'reset by peer',
        'dns', 'name or service not known', 'temporary failure in name',
        'ssl error', 'ssl:', 'certificate verify failed', 'eof',
        'broken pipe', 'unreachable', '超时', '连接失败', '网络异常',
        '域名解析', '证书错误', 'http 5',
    )):
        return 'network'
    return 'unknown'


class CreationGuard:
    _MAX_EVENTS = 500
    _MAX_TASK_SUCCESS_JOURNAL = 1000

    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.RLock()
        self.active = set()
        self.queued = set()
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding='utf-8'))
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                raise ValueError('创建保护记录损坏，请检查后恢复') from None
            if not isinstance(raw, dict):
                raise ValueError('创建保护记录损坏，请检查后恢复')
            self.data = self._normalize(raw)
        else:
            self.data = {'accounts': {}, 'events': [], 'global_paused': False,
                         'settings': {'concurrency': 3, 'daily_limit': 50},
                         'task_successes': {}, 'multi_account_alert': None,
                         'circuit_reset_at': 0}
        # Older releases used a cross-account circuit breaker. Accounts now isolate
        # their failures; retain the compatibility field without enforcing it.
        self.data['global_paused'] = False

    @classmethod
    def _normalize(cls, raw):
        """Validate the durable shape while supplying safe defaults.

        Completion journals are intentionally bounded, but task ids referenced
        by an unresolved reserve are retained so crash recovery remains
        idempotent.
        """
        accounts = raw.get('accounts', {})
        if not isinstance(accounts, dict) or any(not isinstance(v, dict) for v in accounts.values()):
            raise ValueError('创建保护记录损坏，请检查后恢复')
        events = raw.get('events', [])
        if not isinstance(events, list):
            raise ValueError('创建保护记录损坏，请检查后恢复')
        settings = raw.get('settings', {})
        if not isinstance(settings, dict):
            raise ValueError('创建保护记录损坏，请检查后恢复')
        try:
            concurrency = int(settings.get('concurrency', 3))
            daily_limit = int(settings.get('daily_limit', 50))
        except (TypeError, ValueError):
            raise ValueError('创建保护参数损坏，请检查后恢复') from None
        if not 1 <= concurrency <= 10 or not 1 <= daily_limit <= 750:
            raise ValueError('创建保护参数超出范围，请检查后恢复')

        normalized_accounts = {}
        for acc_id, value in accounts.items():
            item = dict(value)
            item.setdefault('attempts', 0)
            item.setdefault('successes', 0)
            item.setdefault('pending', None)
            item.setdefault('pending_task', None)
            item.setdefault('retry_at', 0)
            item.setdefault('blocked', None)
            for key in ('attempts', 'successes'):
                try:
                    item[key] = max(0, int(item[key] or 0))
                except (TypeError, ValueError):
                    raise ValueError('创建保护账号计数损坏，请检查后恢复') from None
            normalized_accounts[str(acc_id)] = item

        task_successes = raw.get('task_successes', {})
        if not isinstance(task_successes, dict):
            raise ValueError('创建保护任务记录损坏，请检查后恢复')
        normalized_tasks = {}
        for task_id, counts in task_successes.items():
            if not isinstance(counts, dict):
                raise ValueError('创建保护任务记录损坏，请检查后恢复')
            clean_counts = {}
            for acc_id, count in counts.items():
                try:
                    clean_counts[str(acc_id)] = max(0, int(count or 0))
                except (TypeError, ValueError):
                    raise ValueError('创建保护任务计数损坏，请检查后恢复') from None
            if clean_counts:
                normalized_tasks[str(task_id)] = clean_counts
        active_tasks = {
            str(item.get('pending_task'))
            for item in normalized_accounts.values()
            if item.get('pending_task')
        }
        if len(normalized_tasks) > cls._MAX_TASK_SUCCESS_JOURNAL:
            items = list(normalized_tasks.items())
            keep = dict(items[-cls._MAX_TASK_SUCCESS_JOURNAL:])
            for task_id in active_tasks:
                if task_id in normalized_tasks:
                    keep[task_id] = normalized_tasks[task_id]
            # Drop oldest completed tasks first; active pending tasks are never
            # discarded even when the journal is over its normal bound.
            while len(keep) > cls._MAX_TASK_SUCCESS_JOURNAL:
                candidate = next((key for key in keep if key not in active_tasks), None)
                if candidate is None:
                    break
                keep.pop(candidate, None)
            normalized_tasks = keep

        clean_events = [event for event in events if isinstance(event, dict)]
        return {
            'accounts': normalized_accounts,
            'events': clean_events[-cls._MAX_EVENTS:],
            'global_paused': bool(raw.get('global_paused', False)),
            'settings': {'concurrency': concurrency, 'daily_limit': daily_limit},
            'task_successes': normalized_tasks,
            'multi_account_alert': raw.get('multi_account_alert') if isinstance(raw.get('multi_account_alert'), dict) else None,
            'circuit_reset_at': float(raw.get('circuit_reset_at', 0) or 0),
        }

    def _prune_task_successes_locked(self):
        tasks = self.data.setdefault('task_successes', {})
        if len(tasks) <= self._MAX_TASK_SUCCESS_JOURNAL:
            return
        active = {
            str(item.get('pending_task'))
            for item in self.data.get('accounts', {}).values()
            if item.get('pending_task')
        }
        for task_id in list(tasks):
            if len(tasks) <= self._MAX_TASK_SUCCESS_JOURNAL:
                break
            if task_id not in active:
                tasks.pop(task_id, None)

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

    def remove_account(self, acc_id):
        """Forget durable protection state for an account that was deleted."""
        acc_id = str(acc_id or "")
        if not acc_id:
            return False
        with self.lock:
            removed = self.data['accounts'].pop(acc_id, None) is not None
            changed = self._prune_history(set(self.data['accounts']))
            self.active.discard(acc_id)
            self.queued.discard(acc_id)
            if removed or changed:
                self.save()
            return removed

    def prune_accounts(self, valid_ids):
        """Remove protection records whose accounts no longer exist."""
        valid = {str(acc_id) for acc_id in (valid_ids or ()) if acc_id}
        with self.lock:
            stale = [acc_id for acc_id in self.data['accounts'] if acc_id not in valid]
            for acc_id in stale:
                self.data['accounts'].pop(acc_id, None)
                self.active.discard(acc_id)
                self.queued.discard(acc_id)
            changed = self._prune_history(valid)
            self.active.intersection_update(valid)
            self.queued.intersection_update(valid)
            if stale or changed:
                self.save()
            return len(stale)

    def _prune_history(self, valid):
        before = json.dumps(self.data, sort_keys=True)
        now = time.time()
        self.data['events'] = [e for e in self.data.get('events', [])
                               if e.get('account_id') in valid and e.get('at', 0) >= now - 600]
        for task_id, counts in list(self.data.get('task_successes', {}).items()):
            self.data['task_successes'][task_id] = {k: v for k, v in counts.items() if k in valid}
            if not self.data['task_successes'][task_id]:
                self.data['task_successes'].pop(task_id)
        alert = self.data.get('multi_account_alert')
        if alert:
            recent = {e['account_id'] for e in self.data['events']
                      if e['kind'] == alert['kind'] and e['at'] > self.data.get('circuit_reset_at', 0)}
            self.data['multi_account_alert'] = dict(alert, accounts=len(recent)) if len(recent) >= 3 else None
        return json.dumps(self.data, sort_keys=True) != before

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
                self._prune_task_successes_locked()
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

    def reset_task_account(self, task_id, acc_id):
        """Forget journal completions before deliberately retrying a finished item."""
        task_id = str(task_id or "")
        acc_id = str(acc_id or "")
        if not task_id or not acc_id:
            return False
        with self.lock:
            counts = self.data.get('task_successes', {}).get(task_id)
            if not isinstance(counts, dict) or acc_id not in counts:
                return False
            counts.pop(acc_id, None)
            if not counts:
                self.data.get('task_successes', {}).pop(task_id, None)
            self.save()
            return True

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
