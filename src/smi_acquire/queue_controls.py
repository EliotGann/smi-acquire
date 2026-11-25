"""Operator actions against an existing queue. Never retry a manager write."""

import asyncio
import time
from uuid import uuid4

from .commissioning import Refused, Rejected, idle_blocker


REQUESTS = {
    'start': ('queue/start', {}), 'stop-after': ('queue/stop', {}),
    'cancel-stop': ('queue/stop/cancel', {}),
    'pause-deferred': ('re/pause', {'option': 'deferred'}),
    'pause-immediate': ('re/pause', {'option': 'immediate'}), 'resume': ('re/resume', {}),
}


def available(status):
    if status.get('worker_environment_exists') is not True or (status.get('lock') or {}).get('environment'):
        return []
    if idle_blocker(status) is None:
        actions = ['start'] if status.get('items_in_queue', 0) > 0 else []
        if status.get('queue_autostart_enabled') is False:
            actions.append('reload')
        return actions
    if not status.get('running_item_uid'):
        return []
    if status.get('manager_state') == 'paused' and status.get('re_state') == 'paused':
        return ['resume']
    if status.get('manager_state') != 'executing_queue':
        return []
    actions = ['cancel-stop' if status.get('queue_stop_pending') else 'stop-after']
    if status.get('re_state') == 'running':
        actions.append('pause-immediate')
        if not status.get('pause_pending'):
            actions.append('pause-deferred')
    return actions


class QueueControls:
    def __init__(self, get, post, commissioning):
        self.get, self.post, self.commissioning = get, post, commissioning
        self.message = ''

    async def _wait(self, opened):
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            s = await self.get('status')
            if s.get('manager_state') == 'idle' and s.get('worker_environment_exists') is opened:
                if not opened or idle_blocker(s) is None:
                    return s
            if s.get('worker_environment_state') == 'failed':
                raise Refused('Worker failed during reload; inspect the console')
            await asyncio.sleep(.5)
        raise Refused('Timed out waiting for the worker; inspect status and console')

    async def run(self, action, expected):
        lock = self.commissioning.lock
        if lock.locked():
            raise Refused('Another workstation operation is in progress')
        async with lock:
            status = await self.get('status')
            if action not in available(status):
                raise Refused('Manager state has changed or does not permit this action')
            if action in ('start', 'reload') and self.commissioning.pending:
                raise Refused('Resolve the outstanding immediate command first')
            key = 'plan_queue_uid' if action in ('start', 'reload') else 'running_item_uid'
            if not expected or expected != status.get(key):
                raise Refused('The queue or running item changed; review it again')
            self.message = f'{action}: requested'
            try:
                if action == 'reload':
                    await self.post('environment/close', {})
                    self.message = 'Reload: waiting for worker to close'
                    closed = await self._wait(False)
                    if (closed.get('queue_autostart_enabled') is not False or
                            (closed.get('lock') or {}).get('environment')):
                        raise Refused('Worker closed; autostart or lock changed. Review before opening.')
                    await self.post('environment/open', {})
                    self.message = 'Reload: waiting for worker startup'
                    await self._wait(True)
                    self.commissioning.caps = None
                    self.message = 'Worker reopened; refreshing allowed plans and devices'
                else:
                    await self.post(*REQUESTS[action])
                    self.message = f'{action}: accepted; check manager and RunEngine state'
            except (Refused, Rejected) as exc:
                self.message = f'{action}: {exc}'
                raise
            except Exception:
                self.message = f'{action}: outcome unknown; check status before another action'
                raise
            return self.message


class DemoQueue:
    """Clock-driven, hardware-free manager state for the workstation simulator."""

    def __init__(self, history, log, clock=time.monotonic):
        self.history, self.log, self.clock = history, log, clock
        self.items = [dict(name=f'Demo count {i}', uid=str(uuid4())) for i in (1, 2)]
        self.uid = str(uuid4())
        self.running = None
        self.open = True
        self.stop_pending = self.paused = self.pause_pending = False

    def _next(self):
        self.running = self.items.pop(0)
        self.uid = str(uuid4())
        self.end = self.clock() + 15
        self.log.append(f'Started {self.running["name"]}')

    def status(self):
        if self.running and not self.paused:
            if self.pause_pending:
                self.remaining = max(0, self.end - self.clock())
                self.paused, self.pause_pending = True, False
            elif self.clock() >= self.end:
                self.history.appendleft(dict(self.running, outcome='completed', message='Demo complete'))
                self.log.append(f'Completed {self.running["name"]}')
                self.running = None
                if self.items and not self.stop_pending:
                    self._next()
                self.stop_pending = False
        return dict(worker_environment_exists=self.open, worker_environment_state=(
            'closed' if not self.open else 'executing_plan' if self.running else 'idle'),
            manager_state='paused' if self.paused else 'executing_queue' if self.running else 'idle',
            re_state='paused' if self.paused else 'running' if self.running else 'idle' if self.open else None,
            running_item_uid=self.running['uid'] if self.running else None, items_in_queue=len(self.items),
            plan_queue_uid=self.uid, queue_stop_pending=self.stop_pending, pause_pending=self.pause_pending,
            queue_autostart_enabled=False)

    async def post(self, path, body):
        if path == 'queue/start':
            self._next()
        elif path == 'queue/stop':
            self.stop_pending = True
        elif path == 'queue/stop/cancel':
            self.stop_pending = False
        elif path == 're/pause':
            self.pause_pending = True
        elif path == 're/resume':
            self.paused = False
            self.end = self.clock() + self.remaining
        elif path in ('environment/open', 'environment/close'):
            self.open = path.endswith('open')
        else:
            raise Rejected('Unsupported demo action')
        self.log.append(f'Simulator accepted {path}')
