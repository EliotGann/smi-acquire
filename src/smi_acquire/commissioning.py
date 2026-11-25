"""Two workstation commands. Device bindings and maximum jog come from the launcher."""

import asyncio
import math
import re
import time
from uuid import uuid4


class Refused(ValueError):
    """No plan was submitted."""


class Rejected(ValueError):
    """The manager explicitly rejected a submitted item."""


def idle_blocker(status):
    if status.get('worker_environment_exists') is not True:
        return 'Worker environment is not open'
    for key in ('manager_state', 'worker_environment_state', 're_state'):
        if status.get(key) != 'idle':
            return f'{key}: {status.get(key) or "unknown"}'
    if status.get('running_item_uid'):
        return 'An item is running'
    # In a Python worker QueueServer explicitly reports the optional kernel as disabled.
    if status.get('ip_kernel_state') not in (None, 'idle', 'disabled'):
        return f'IPython kernel: {status["ip_kernel_state"]}'
    if status.get('worker_background_tasks'):
        return 'Worker has background tasks'
    if (status.get('lock') or {}).get('environment'):
        return 'Worker environment is locked'
    return None


def device_names(tree, prefix=''):
    for name, description in tree.items():
        path = prefix + name
        yield path, description.get('is_movable') is True
        yield from device_names(description.get('components', {}), path + '.')


class Commissioning:
    def __init__(self, get, execute_item, *, exposure=None, axis=None, step=None, units=None):
        for name in (exposure, axis):
            if name is not None and not re.fullmatch(r'[A-Za-z]\w*(?:\.[A-Za-z]\w*)*', name):
                raise ValueError('Use a worker device name, not a PV address')
        if any(x is not None for x in (axis, step, units)) and not (
                axis and units and step and math.isfinite(step) and step > 0):
            raise ValueError('A jog requires a verified axis, positive step and motor units')
        self.get, self.execute_item = get, execute_item
        self.exposure, self.axis, self.step, self.units = exposure, axis, step, units
        self.command = None
        self.finished_at = None
        self.caps = None
        self.lock = asyncio.Lock()

    async def discover(self):
        plans, devices = await asyncio.gather(self.get('plans/allowed'), self.get('devices/allowed'))
        names = dict(device_names(devices['devices_allowed']))
        self.caps = dict(plans=sorted(plans['plans_allowed']), devices=sorted(names),
                         movable=[k for k in (self.axis, self.exposure) if names.get(k)],
                         exposure=self.exposure, axis=self.axis, step=self.step, units=self.units)
        return self.caps

    @property
    def pending(self):
        return self.command and self.command['state'] in ('sending', 'accepted', 'unknown')

    async def submit(self, action, value, *, exposure_rbv=None):
        if type(value) not in (float, int) or not math.isfinite(value):
            raise Refused('Enter a finite numeric value')
        if action == 'exposure':
            plan, device = 'mv', self.exposure
            if not .0001 <= value <= 2:
                raise Refused('Exposure must be between 0.0001 and 2 seconds')
            if exposure_rbv is None or not math.isfinite(exposure_rbv):
                raise Refused('Camera exposure readback is unavailable')
        elif action == 'jog':
            plan, device = 'mvr', self.axis
            if not self.step or abs(value) != self.step:
                raise Refused('Only the configured jog step is enabled')
        else:
            raise Refused('Unknown workstation command')
        if not device:
            raise Refused('This control has no commissioned device binding')
        if self.lock.locked() or self.pending:
            raise Refused('A previous command is not yet resolved; check history')
        async with self.lock:
            caps = await self.discover()
            if plan not in caps['plans'] or device not in caps['movable']:
                raise Refused('Required plan or movable device is not allowed for this login')
            blocker = idle_blocker(await self.get('status'))
            if blocker:
                raise Refused(blocker)
            tag = str(uuid4())
            self.finished_at = None
            self.command = dict(state='sending', plan=plan, device=device, value=value,
                                tag=tag, uid=None)
            item = dict(item_type='plan', name=plan, args=[device, value],
                        meta={'smi_acquire_command': tag})
            try:
                receipt = await self.execute_item(item)
                uid = receipt['item']['item_uid']
                if not isinstance(uid, str) or not uid:
                    raise ValueError('Missing item UID')
                if self.command['state'] == 'sending':
                    self.command.update(state='accepted', uid=uid)
            except Rejected as exc:
                self.command.update(state='rejected', message=str(exc))
            except (OSError, ValueError, KeyError, TypeError):
                if self.command['state'] == 'sending':
                    self.command.update(state='unknown')
            return self.command

    def reconcile(self, history):
        if not self.pending:
            return
        for item in reversed(history):
            uid = item.get('item_uid')
            meta = item.get('meta') or {}
            tags = meta if isinstance(meta, list) else [meta]
            matched = (uid == self.command['uid'] if self.command['uid'] else
                       any(isinstance(m, dict) and m.get('smi_acquire_command') == self.command['tag']
                           for m in tags))
            result = item.get('result') or {}
            if uid and matched and result.get('exit_status'):
                self.command.update(uid=uid, state='finished', outcome=result['exit_status'],
                                    message=(result.get('msg') or '')[:2000])
                self.finished_at = time.monotonic()
                return

    def exposure_confirmation(self, value, stamp):
        if (not self.finished_at or self.command['plan'] != 'mv'
                or self.command.get('outcome') != 'completed'):
            return None
        if value is None or stamp <= self.finished_at:
            return 'awaiting fresh readback'
        return ('matches request' if math.isclose(value, self.command['value'], abs_tol=1e-6,
                                                  rel_tol=1e-5) else 'differs from request')
