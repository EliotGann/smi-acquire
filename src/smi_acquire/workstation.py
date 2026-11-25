"""Workstation monitor with explicitly bound immediate QueueServer controls."""

import argparse
import asyncio
from collections import deque
from io import BytesIO
import json
import logging
import math
import os
from pathlib import Path
import secrets
import tempfile
import time
from urllib.parse import urlsplit
from uuid import uuid4
import webbrowser

from PIL import Image, ImageDraw
from tornado.httpclient import AsyncHTTPClient, HTTPRequest, HTTPClientError
from tornado.web import Application, HTTPError, RequestHandler, StaticFileHandler

from .commissioning import Commissioning, Refused, Rejected, idle_blocker
from .bookmark_view import BookmarkView
from .queue_controls import DemoQueue, QueueControls, available


def summarize(item):
    result = item.get('result') or {}
    return {'uid': item.get('item_uid'), 'name': item.get('name'),
            'outcome': result.get('exit_status'), 'message': result.get('msg', '')[:2000]}


def read_error(exc):
    return f'HTTP {exc.code}' if isinstance(exc, HTTPClientError) else type(exc).__name__


class QueueMonitor:
    """One monitor per workstation process; browser tabs share cached snapshots."""

    def __init__(self, get, controls=None):
        self.get = get
        self.controls = controls
        self.queue_controls = None
        self.state = {'mode': 'live monitor', 'connected': False, 'console': '',
                      'queue': [], 'history': [], 'errors': {}}
        self.versions = {}
        self.unavailable = {}

    async def read(self, path):
        if path in self.unavailable:
            raise HTTPClientError(self.unavailable[path])
        try:
            return await self.get(path)
        except HTTPClientError as exc:
            if 400 <= exc.code < 500 and exc.code != 429:
                # Retrying a denied/missing endpoint does not repair credentials or versions.
                self.unavailable[path] = exc.code
            raise

    async def refresh(self):
        state = self.state
        errors = {}
        try:
            status = await self.read('status')
        except (OSError, HTTPClientError, ValueError) as exc:
            state.update(connected=False, idle=False, errors={'status': read_error(exc)})
            return
        blocker = idle_blocker(status)
        state.update(queue_actions=available(status), stop_pending=status.get('queue_stop_pending'),
                     pause_pending=status.get('pause_pending'))
        state.update(connected=True, idle=blocker is None, idle_blocker=blocker, manager=status.get('manager_state'),
                     re=status.get('re_state'), environment=status.get('worker_environment_state'),
                     count=status.get('items_in_queue'), updated=time.time())
        revision = (status.get('plans_allowed_uid'), status.get('devices_allowed_uid'))
        if self.controls and (self.controls.caps is None or self.versions.get('caps') != revision):
            try:
                await self.controls.discover()
                self.versions['caps'] = revision
            except (OSError, HTTPClientError, KeyError, ValueError) as exc:
                self.controls.caps = None
                errors['capabilities'] = read_error(exc)
        for name, path, version in [('queue', 'queue/get', 'plan_queue_uid'),
                                    ('history', 'history/get', 'plan_history_uid')]:
            uid = status.get(version)
            if uid and self.versions.get(name) == uid:
                continue
            try:
                data = await self.read(path)
                items = data['items']
                if name == 'history' and self.controls:
                    self.controls.reconcile(items)
                state[name] = [summarize(i) for i in (items[:100] if name == 'queue'
                                                      else reversed(items[-100:]))]
                if name == 'queue':
                    running = data.get('running_item') or {}
                    # Bind buttons to the list actually displayed, not a newer status poll.
                    state.update(running=running.get('name'), running_uid=running.get('item_uid'),
                                 queue_revision=data.get(version))
                self.versions[name] = data.get(version)
            except (OSError, HTTPClientError, KeyError, ValueError) as exc:
                errors[name] = read_error(exc)
        try:
            data = await self.read('console_output')
            state['console'] = data['text'][-32000:]
        except (OSError, HTTPClientError, KeyError, ValueError) as exc:
            errors['console'] = read_error(exc)
        state['errors'] = errors


def live_transport(uri, key):
    parts = urlsplit(uri)
    if parts.scheme != 'https' or not parts.hostname or parts.username or parts.password \
            or parts.query or parts.fragment or not key:
        raise ValueError('Configure an HTTPS QueueServer URI and workstation API key')
    client = AsyncHTTPClient(force_instance=True, max_buffer_size=8 * 1024 * 1024)

    async def request(path, body=None):
        request = HTTPRequest(f'{uri.rstrip("/")}/api/{path}',
                              headers={'Authorization': f'ApiKey {key}', 'Content-Type': 'application/json'},
                              method='GET' if body is None else 'POST',
                              body=None if body is None else json.dumps(body),
                              follow_redirects=False, connect_timeout=2, request_timeout=5)
        response = await client.fetch(request)
        data = json.loads(response.body)
        if not isinstance(data, dict):
            raise ValueError('Malformed response')
        if data.get('success') is False:
            reason = str(data.get('msg') or 'QueueServer rejected request')
            raise Rejected(reason.replace(key, '[redacted]')[:500])
        return data

    async def get(path):
        return await request(path)

    async def post(path, body):
        try:
            result = await request(path, body)
            if result.get('success') is not True:
                raise ValueError('Missing acknowledgement')
            return result
        except HTTPClientError as exc:
            if exc.code in {400, 401, 403, 404, 405, 422}:
                raise Rejected(f'HTTP {exc.code}: request rejected') from exc
            raise OSError('Manager response unavailable') from exc

    async def immediate(item):
        return await post('queue/item/execute', {'item': item})

    return get, immediate, post, client.close


class Simulator:
    def __init__(self):
        from bluesky import RunEngine
        from ophyd import Signal
        self.engine = RunEngine({}, context_managers=[])
        self.x = Signal(name='sim_x', value=0.0)
        self.exposure = Signal(name='sim_exposure', value=0.1)
        self.history = deque(maxlen=100)
        self.log = deque(['Simulated worker ready.'], maxlen=100)
        self.controls = Commissioning(self.get, self.immediate, exposure='sim_exposure',
                                     axis='sim_x', step=.1, units='sim units')
        self.demo = DemoQueue(self.history, self.log)
        self.queue_controls = QueueControls(self.get, self.demo.post, self.controls)
        self.records = {}
        self.bookmarks = BookmarkView(lambda keys: [self.records.get(k) for k in keys])
        from .bookmark_edit import editor
        from smi_plans import SampleStore
        self.bookmarks.edit = editor(lambda keys, update: update(SampleStore(self.records)),
                                     lambda: dict(piezo_x=self.x.get(), piezo_y=0., piezo_z=0.))

    async def get(self, path):
        return {'plans/allowed': {'plans_allowed': {'mv': {}, 'mvr': {}}},
                'devices/allowed': {'devices_allowed': {s.name: {'is_movable': True}
                                                        for s in (self.x, self.exposure)}},
                'status': self.demo.status()}[path]

    async def immediate(self, item):
        import bluesky.plan_stubs as bps
        name = item['name']
        if name == 'mvr' and item['args'][0] == self.x.name:
            value = item['args'][1]
            self.engine(bps.mvr(self.x, value))
            message = f'Simulated stage X = {self.x.get():.2f}'
        elif name == 'mv' and item['args'][0] == self.exposure.name:
            value = item['args'][1]
            self.engine(bps.mv(self.exposure, value))
            message = f'Simulated camera exposure = {self.exposure.get():.1f} s'
        else:
            raise ValueError('Unknown simulator action')
        uid = str(uuid4())
        completed = dict(item, item_uid=uid, result=dict(exit_status='completed', msg=message))
        self.history.appendleft(summarize(completed))
        self.controls.reconcile([completed])
        self.log.append(message)
        return {'item': completed}

    @property
    def state(self):
        s = self.demo.status()
        return dict(mode='simulation', manager=s['manager_state'], re=s['re_state'],
                    environment=s['worker_environment_state'], queue_actions=available(s),
                    queue_revision=s['plan_queue_uid'], running_uid=s['running_item_uid'],
                    running=(self.demo.running or {}).get('name'), stop_pending=s['queue_stop_pending'],
                    pause_pending=s['pause_pending'], connected=True, idle=idle_blocker(s) is None,
                    queue=self.demo.items, count=len(self.demo.items), history=list(self.history), errors={},
                    console='\n'.join(self.log))

    def image(self):
        im = Image.new('RGB', (480, 320), '#253139')
        x = 30 + round(self.x.get() * 100) % 400
        ImageDraw.Draw(im).ellipse((x, 140, x + 40, 180), fill='white' if self.exposure.get() > .1
                                  else 'silver')
        return im


class LiveCamera:
    def __init__(self, config):
        from .microscope.camera_stream import CameraStream
        from .microscope.config import load_config
        from .microscope.devices import Camera
        cfg = load_config(config).epics
        self.epics_cfg = cfg
        self.pose_signals = {}
        self.stream = CameraStream(Camera.from_config(cfg), None, epics_cfg=cfg)
        self.pva = cfg.image_protocol.lower() == 'pva'

    def exposure_readback(self):
        sig = self.stream.camera.cam.acquire_time_rbv
        return float(sig.get(use_monitor=False, timeout=1, connection_timeout=1))

    def read_pose(self):
        from ophyd import EpicsSignalRO
        cfg = self.epics_cfg
        if not self.pose_signals:
            signals = {}
            for stack, motors in [('piezo', cfg.piezo_motors or cfg.motors), ('stage', cfg.stage_motors)]:
                for axis, record in motors.items():
                    field = f'{stack}_{axis}'
                    signals[field] = EpicsSignalRO(record + '.RBV', name=field, auto_monitor=False)
            self.pose_signals = signals
        return {k: float(s.get(use_monitor=False, timeout=1, connection_timeout=1))
                for k, s in self.pose_signals.items()}

    def image(self):
        rgb = self.stream.grab_rgb()
        if rgb is None or (self.pva and time.monotonic() - self.stream.frame_stamp() > 5):
            return None
        stride = max(1, (max(rgb.shape[:2]) + 959) // 960)
        return Image.fromarray(rgb[::stride, ::stride])


async def update_frame(camera, cache):
    def encode():
        image = camera.image()
        if image is None:
            return None
        output = BytesIO()
        image.save(output, format='JPEG', quality=80)
        return output.getvalue()
    while True:
        try:
            cache['frame'] = await asyncio.to_thread(encode)
        except (OSError, ValueError, RuntimeError):
            cache['frame'] = None
        await asyncio.sleep(.25)


async def poll(monitor):
    delay = 1
    while True:
        await monitor.refresh()
        delay = min(delay * 2, 30) if any(not v.startswith('HTTP 4') or v == 'HTTP 429'
                                          for v in monitor.state['errors'].values()) else 1
        await asyncio.sleep(delay)


async def monitor_exposure(camera, cache):
    while True:
        try:
            started = time.monotonic()
            value = await asyncio.to_thread(camera.exposure_readback)
            cache['rbv'] = (value, started) if math.isfinite(value) else None
        except (OSError, RuntimeError, ValueError, TypeError):
            cache['rbv'] = None
        await asyncio.sleep(1 if cache['rbv'] else 5)


class API(RequestHandler):
    def readback(self, backend):
        if isinstance(backend, Simulator):
            return backend.exposure.get()
        rbv = self.settings['cache'].get('rbv')
        return rbv[0] if rbv and time.monotonic() - rbv[1] < 4 else None

    def prepare(self):
        key = self.settings['session_key']
        if not secrets.compare_digest(self.request.headers.get('X-Workstation-Session', ''), key):
            raise HTTPError(403)
        self.set_header('Cache-Control', 'no-store')

    async def get(self, resource):
        if resource == 'state':
            backend = self.settings['backend']
            ctrl = backend.controls
            rbv = self.readback(backend)
            stamp = (time.monotonic() if isinstance(backend, Simulator) else
                     (self.settings['cache'].get('rbv') or (None, 0))[1])
            self.write(dict(backend.state, capabilities=ctrl.caps if ctrl else None,
                            bookmarks_enabled=getattr(backend, 'bookmarks', None) is not None,
                            bookmark_goto_max_um=getattr(backend, 'bookmark_goto_max_um', None),
                            queue_enabled=bool(backend.queue_controls),
                            queue_message=backend.queue_controls.message if backend.queue_controls else '',
                            command=ctrl.command if ctrl else None, exposure_rbv=rbv,
                            confirmation=ctrl.exposure_confirmation(rbv, stamp) if ctrl else None))
        elif resource == 'bookmarks':
            view = getattr(self.settings['backend'], 'bookmarks', None)
            if view is None:
                raise HTTPError(405)
            try:
                page = await asyncio.to_thread(view.page, self.get_argument('holder', None),
                                               int(self.get_argument('offset', '0')))
            except ValueError as exc:
                raise HTTPError(400) from exc
            except Exception as exc:
                raise HTTPError(503, reason='Shared sample store unavailable') from exc
            self.write(page)
        elif resource == 'frame' and (frame := self.settings['cache'].get('frame')):
            self.set_header('Content-Type', 'image/jpeg')
            self.finish(frame)
        else:
            raise HTTPError(503 if resource == 'frame' else 404)

    async def post(self, resource):
        backend = self.settings['backend']
        if resource == 'bookmark':
            if getattr(backend, 'bookmarks', None) is None:
                raise HTTPError(405)
            try:
                from .bookmark_view import validate_request
                spec = validate_request(json.loads(self.request.body))
                if spec['op'] == 'goto':
                    cap = getattr(backend, 'bookmark_goto_max_um', None)
                    if not cap:
                        raise Refused('Bookmark motion was not enabled at launch')
                    cmd = await backend.controls.submit_named('load_sample',
                        [spec['holder_id'], spec['sample_id'], spec['updated'], cap], value='goto piezo XYZ')
                else:
                    if spec['op'] in ('capture', 'assign'):
                        if backend.controls.pending or backend.controls.lock.locked():
                            raise Refused('Wait for the worker and motors to be idle before capture')
                        async with backend.controls.lock:
                            if idle_blocker(await backend.controls.get('status')):
                                raise Refused('Wait for the worker to be idle before capture')
                            cmd = await asyncio.to_thread(backend.bookmarks.edit, spec)
                    else:
                        cmd = await asyncio.to_thread(backend.bookmarks.edit, spec)
            except (ValueError, TypeError, KeyError) as exc:
                self.set_status(409)
                self.write({'error': str(exc)[:500]})
                return
            except Exception as exc:
                raise HTTPError(503, reason='Bookmark operation unavailable; refresh before retry') from exc
            self.write(cmd)
            return
        if resource == 'queue-action':
            if not backend.queue_controls:
                raise HTTPError(405)
            try:
                body = json.loads(self.request.body)
                message = await backend.queue_controls.run(body['action'], body['expected'])
                if isinstance(backend, Simulator) and backend.controls.caps is None:
                    await backend.controls.discover()
            except (Refused, Rejected) as exc:
                self.set_status(409)
                self.write({'error': str(exc)})
                return
            except (KeyError, TypeError, json.JSONDecodeError):
                raise HTTPError(400)
            except (OSError, ValueError, HTTPClientError):
                self.set_status(503)
                self.write({'error': 'Manager response unavailable; check current status'})
                return
            self.write({'message': message})
            return
        if not backend.controls or resource not in {'jog', 'exposure'}:
            raise HTTPError(405)
        try:
            value = json.loads(self.request.body)['value']
            cmd = await backend.controls.submit(resource, value, exposure_rbv=self.readback(backend))
        except Refused as exc:
            self.set_status(409)
            self.write({'error': str(exc)})
            return
        except (ValueError, KeyError, TypeError, OverflowError):
            raise HTTPError(400)
        except (HTTPClientError, OSError):
            raise HTTPError(503)
        self.write(cmd)


async def serve(args):
    root = Path(__file__).resolve().parents[2] / 'workstation' / 'dist'
    if not (root / 'index.html').is_file():
        raise SystemExit('Build the workstation frontend first (see docs/WORKSTATION.md)')
    close = []
    tasks = []
    cache = {}
    try:
        if args.live_camera_config:
            get, immediate, post, finish = live_transport(os.environ['QSERVER_HTTP_SERVER_URI'],
                                        os.environ['QSERVER_HTTP_SERVER_API_KEY'])
            close.append(finish)
            backend = QueueMonitor(get)
            if args.bookmark_goto_max_um is not None:
                if not args.bookmarks or not math.isfinite(args.bookmark_goto_max_um) or args.bookmark_goto_max_um <= 0:
                    raise ValueError('--bookmark-goto-max-um requires --bookmarks and a positive travel cap')
                backend.bookmark_goto_max_um = args.bookmark_goto_max_um
            backend.controls = Commissioning(backend.read, immediate, exposure=args.exposure_device,
                                             axis=args.jog_axis, step=args.jog_step, units=args.jog_units)
            if args.queue_controls:
                backend.queue_controls = QueueControls(backend.read, post, backend.controls)
            camera = LiveCamera(args.live_camera_config)
            if args.bookmarks:
                backend.bookmarks, finish_bookmarks = BookmarkView.live(camera.read_pose)
                close.append(finish_bookmarks)
            close.append(camera.stream.close)
            tasks.append(asyncio.create_task(poll(backend)))
            tasks.append(asyncio.create_task(monitor_exposure(camera, cache)))
        else:
            camera = backend = Simulator()
            await backend.controls.discover()
        tasks.append(asyncio.create_task(update_frame(camera, cache)))
        key = secrets.token_urlsafe(32)
        app = Application([(r'/api/([^/]+)', API), (r'/(.*)', StaticFileHandler,
                          {'path': str(root), 'default_filename': 'index.html'})],
                          backend=backend, cache=cache, session_key=key)
        server = app.listen(5097, address='127.0.0.1')
        close.append(server.stop)
        logging.getLogger(__name__).info('Workstation started: %s', backend.state['mode'])
        # The per-launch session token is local, short lived, and never sent in a URL to HTTP.
        url = 'http://127.0.0.1:5097/#' + key
        with tempfile.TemporaryDirectory(prefix='smi-workstation-') as directory:
            launch = Path(directory) / 'open.html'
            launch.write_text(f'<meta http-equiv="refresh" content="0;url={url}">')
            if not webbrowser.open(launch.as_uri()):
                raise SystemExit('No workstation browser found; set BROWSER and launch again')
            await asyncio.Event().wait()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for cleanup in reversed(close):
            cleanup()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live-camera-config', type=Path, metavar='PATH',
                        help='Opt into live reads using this microscope config; default: simulation')
    parser.add_argument('--exposure-device', help='Verified worker setpoint for the configured camera (seconds)')
    parser.add_argument('--jog-axis', help='One verified sample-stage worker motor')
    parser.add_argument('--jog-step', type=float, help='Fixed relative step in installed motor units')
    parser.add_argument('--jog-units', help='Verified units of the configured motor')
    parser.add_argument('--queue-controls', action='store_true',
                        help='Enable queue start/stop, RunEngine pause/resume and idle worker reload')
    parser.add_argument('--bookmarks', action='store_true',
                        help='Browse, capture and edit shared db=2 sample records')
    parser.add_argument('--bookmark-goto-max-um', type=float,
                        help='Opt into piezo XYZ bookmark recall with this per-axis travel cap (um)')
    try:
        asyncio.run(serve(parser.parse_args()))
    except KeyboardInterrupt:
        pass
