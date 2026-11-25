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
                    state['running'] = (data.get('running_item') or {}).get('name')
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

    async def immediate(item):
        try:
            return await request('queue/item/execute', {'item': item})
        except HTTPClientError as exc:
            if exc.code in {400, 401, 403, 404, 405, 422}:
                raise Rejected(f'HTTP {exc.code}: immediate plan rejected') from exc
            raise OSError('Immediate-plan response unavailable') from exc

    return get, immediate, client.close


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

    async def get(self, path):
        return {'plans/allowed': {'plans_allowed': {'mv': {}, 'mvr': {}}},
                'devices/allowed': {'devices_allowed': {s.name: {'is_movable': True}
                                                        for s in (self.x, self.exposure)}},
                'status': dict(manager_state='idle', re_state='idle', worker_environment_exists=True,
                               worker_environment_state='idle')}[path]

    async def immediate(self, item):
        import bluesky.plan_stubs as bps
        name, (device, value) = item['name'], item['args']
        if (name, device) == ('mvr', self.x.name):
            self.engine(bps.mvr(self.x, value))
            message = f'Simulated stage X = {self.x.get():.2f}'
        elif (name, device) == ('mv', self.exposure.name):
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
        return dict(mode='simulation', manager='idle', re='idle', environment='idle',
                    connected=True, idle=True, queue=[], count=0, history=list(self.history), errors={},
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
        self.stream = CameraStream(Camera.from_config(cfg), None, epics_cfg=cfg)
        self.pva = cfg.image_protocol.lower() == 'pva'

    def exposure_readback(self):
        sig = self.stream.camera.cam.acquire_time_rbv
        return float(sig.get(use_monitor=False, timeout=1, connection_timeout=1))

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

    def get(self, resource):
        if resource == 'state':
            backend = self.settings['backend']
            ctrl = backend.controls
            rbv = self.readback(backend)
            stamp = (time.monotonic() if isinstance(backend, Simulator) else
                     (self.settings['cache'].get('rbv') or (None, 0))[1])
            self.write(dict(backend.state, capabilities=ctrl.caps if ctrl else None,
                            command=ctrl.command if ctrl else None, exposure_rbv=rbv,
                            confirmation=ctrl.exposure_confirmation(rbv, stamp) if ctrl else None))
        elif resource == 'frame' and (frame := self.settings['cache'].get('frame')):
            self.set_header('Content-Type', 'image/jpeg')
            self.finish(frame)
        else:
            raise HTTPError(503 if resource == 'frame' else 404)

    async def post(self, resource):
        backend = self.settings['backend']
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
            get, immediate, finish = live_transport(os.environ['QSERVER_HTTP_SERVER_URI'],
                                        os.environ['QSERVER_HTTP_SERVER_API_KEY'])
            close.append(finish)
            backend = QueueMonitor(get)
            backend.controls = Commissioning(backend.read, immediate, exposure=args.exposure_device,
                                             axis=args.jog_axis, step=args.jog_step, units=args.jog_units)
            camera = LiveCamera(args.live_camera_config)
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
    try:
        asyncio.run(serve(parser.parse_args()))
    except KeyboardInterrupt:
        pass
