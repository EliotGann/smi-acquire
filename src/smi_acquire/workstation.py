"""Workstation monitor. Live mode has a GET-only QueueServer transport."""

import argparse
import asyncio
from collections import deque
from io import BytesIO
import json
import logging
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


def summarize(item):
    result = item.get('result') or {}
    return {'uid': item.get('item_uid'), 'name': item.get('name'),
            'outcome': result.get('exit_status'), 'message': result.get('msg', '')[:2000]}


def read_error(exc):
    return f'HTTP {exc.code}' if isinstance(exc, HTTPClientError) else type(exc).__name__


class QueueMonitor:
    """One monitor per workstation process; browser tabs share cached snapshots."""

    def __init__(self, get):
        self.get = get
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
            state.update(connected=False, errors={'status': read_error(exc)})
            return
        state.update(connected=True, manager=status.get('manager_state'),
                     re=status.get('re_state'), environment=status.get('worker_environment_state'),
                     count=status.get('items_in_queue'), updated=time.time())
        for name, path, version in [('queue', 'queue/get', 'plan_queue_uid'),
                                    ('history', 'history/get', 'plan_history_uid')]:
            uid = status.get(version)
            if uid and self.versions.get(name) == uid:
                continue
            try:
                data = await self.read(path)
                items = data['items']
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

    async def get(path):
        request = HTTPRequest(f'{uri.rstrip("/")}/api/{path}',
                              headers={'Authorization': f'ApiKey {key}'},
                              follow_redirects=False, connect_timeout=2, request_timeout=5)
        response = await client.fetch(request)
        data = json.loads(response.body)
        if not isinstance(data, dict) or data.get('success') is False:
            raise ValueError('QueueServer rejected read')
        return data

    return get, client.close


class Simulator:
    def __init__(self):
        from bluesky import RunEngine
        from ophyd import Signal
        self.engine = RunEngine({}, context_managers=[])
        self.x = Signal(name='sim_x', value=0.0)
        self.exposure = Signal(name='sim_exposure', value=0.1)
        self.history = deque(maxlen=100)
        self.log = deque(['Simulated worker ready.'], maxlen=100)

    def execute(self, action):
        import bluesky.plan_stubs as bps
        if action == 'jog':
            name = 'mvr'
            self.engine(bps.mvr(self.x, 0.1))
            message = f'Simulated stage X = {self.x.get():.2f}'
        elif action == 'exposure':
            name = 'mv'
            self.engine(bps.mv(self.exposure, 0.2 if self.exposure.get() == 0.1 else 0.1))
            message = f'Simulated camera exposure = {self.exposure.get():.1f} s'
        else:
            raise ValueError('Unknown simulator action')
        uid = str(uuid4())
        self.history.appendleft(dict(uid=uid, name=name, outcome='completed', message=message))
        self.log.append(message)

    @property
    def state(self):
        return dict(mode='simulation', manager='idle', re='idle', environment='idle',
                    connected=True, queue=[], count=0, history=list(self.history), errors={},
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


class API(RequestHandler):
    def prepare(self):
        key = self.settings['session_key']
        if not secrets.compare_digest(self.request.headers.get('X-Workstation-Session', ''), key):
            raise HTTPError(403)
        self.set_header('Cache-Control', 'no-store')

    def get(self, resource):
        if resource == 'state':
            self.write(self.settings['backend'].state)
        elif resource == 'frame' and (frame := self.settings['cache'].get('frame')):
            self.set_header('Content-Type', 'image/jpeg')
            self.finish(frame)
        else:
            raise HTTPError(503 if resource == 'frame' else 404)

    def post(self, resource):
        backend = self.settings['backend']
        if not isinstance(backend, Simulator) or resource not in {'jog', 'exposure'}:
            raise HTTPError(405)
        backend.execute(resource)
        self.write(backend.state)


async def serve(args):
    root = Path(__file__).resolve().parents[2] / 'workstation' / 'dist'
    if not (root / 'index.html').is_file():
        raise SystemExit('Build the workstation frontend first (see docs/WORKSTATION.md)')
    close = []
    tasks = []
    cache = {}
    try:
        if args.live_camera_config:
            get, finish = live_transport(os.environ['QSERVER_HTTP_SERVER_URI'],
                                        os.environ['QSERVER_HTTP_SERVER_API_KEY'])
            close.append(finish)
            backend = QueueMonitor(get)
            camera = LiveCamera(args.live_camera_config)
            close.append(camera.stream.close)
            tasks.append(asyncio.create_task(poll(backend)))
        else:
            camera = backend = Simulator()
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
    try:
        asyncio.run(serve(parser.parse_args()))
    except KeyboardInterrupt:
        pass
