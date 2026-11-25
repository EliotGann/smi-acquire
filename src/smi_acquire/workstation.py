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
    def __init__(self, *, piezo_jogs=False, angular_step=None, step=None):
        from bluesky import RunEngine
        from ophyd import Signal
        self.engine = RunEngine({}, context_managers=[])
        self.motors = {name: Signal(name=name, value=0.) for name in (
            [f'sample_piezo_{a}' for a in ('x','y','z','th','chi')] if piezo_jogs else ['sim_x'])}
        self.x = next(iter(self.motors.values()))
        self.exposure = Signal(name='sim_exposure', value=0.1)
        self.history = deque(maxlen=100)
        self.log = deque(['Simulated worker ready.'], maxlen=100)
        jogs = (dict(piezo_jogs=True, step=step, angular_step=angular_step) if piezo_jogs
                else dict(axis='sim_x', step=.1, units='sim units'))
        self.controls = Commissioning(self.get, self.immediate, exposure='sim_exposure', **jogs)
        self.demo = DemoQueue(self.history, self.log)
        self.queue_controls = QueueControls(self.get, self.demo.post, self.controls)
        self.records = {}
        self.bookmarks = BookmarkView(lambda keys: [self.records.get(k) for k in keys])
        from .bookmark_edit import editor
        from smi_plans import SampleStore
        self.bookmarks.edit = editor(lambda keys, update: update(SampleStore(self.records)),
                                     lambda: ({k.removeprefix('sample_'): m.get() for k,m in self.motors.items()}
                                              if piezo_jogs else dict(piezo_x=self.x.get(), piezo_y=0., piezo_z=0.)))

    async def get(self, path):
        return {'plans/allowed': {'plans_allowed': {'mv': {}, 'mvr': {}}},
                'devices/allowed': {'devices_allowed': {s.name: {'is_movable': True}
                                                        for s in (*self.motors.values(), self.exposure)}},
                'status': self.demo.status()}[path]

    async def immediate(self, item):
        import bluesky.plan_stubs as bps
        name = item['name']
        if name == 'mvr' and item['args'][0] in self.motors:
            motor = self.motors[item['args'][0]]
            value = item['args'][1]
            self.engine(bps.mvr(motor, value))
            message = f'{motor.name} = {motor.get():g}'
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
        self.config = load_config(config)
        cfg = self.config.epics
        self.epics_cfg = cfg
        self.pose_signals = {}
        self.chi_signals = {}
        self.xy_signals = {}
        self.display_size = None
        self.stream = CameraStream(Camera.from_config(cfg), None, epics_cfg=cfg,
                                   display_max_dim=self.config.ui.display_max_dim)
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
        from .microscope.camera_stream import stride_for_max_dim
        rgb = self.stream.grab_rgb()
        if rgb is None or (self.pva and time.monotonic() - self.stream.frame_stamp() > 5):
            self.display_size = None
            return None
        stride = stride_for_max_dim(rgb.shape[1], rgb.shape[0], self.config.ui.display_max_dim)
        image = Image.fromarray(rgb[::stride, ::stride])
        self.display_size = image.size
        return image

    def read_chi(self, stack):
        from ophyd import EpicsSignalRO
        motors = self.epics_cfg.piezo_motors if stack == 'piezo' else self.epics_cfg.stage_motors
        if not motors.get('chi'):
            raise ValueError(f'No {stack} chi readback configured')
        if stack not in self.chi_signals:
            self.chi_signals[stack] = EpicsSignalRO(motors['chi'] + '.RBV', name=f'{stack}_chi')
        angle = float(self.chi_signals[stack].get(use_monitor=False, timeout=1, connection_timeout=1))
        if not math.isfinite(angle):
            raise ValueError(f'Invalid {stack} chi readback')
        return angle

    def read_xy(self):
        from ophyd import EpicsSignalRO
        for a in ('x', 'y'):
            if a not in self.xy_signals:
                pv = self.epics_cfg.piezo_motors.get(a)
                if not pv:
                    raise ValueError(f'No piezo {a} readback configured')
                self.xy_signals[a] = EpicsSignalRO(pv + '.RBV', name='piezo_' + a)
        xy = [float(self.xy_signals[a].get(use_monitor=False, timeout=1, connection_timeout=1))
              for a in ('x', 'y')]
        if not all(map(math.isfinite, xy)):
            raise ValueError('Invalid piezo XY readback')
        return xy

    def preview(self, stack, x, y, width, height):
        from .microscope.calibration import CalibrationModel
        from numpy.linalg import LinAlgError
        if self.display_size != (width, height):
            raise ValueError('Image dimensions changed or frame unavailable; click the current frame')
        if stack not in ('piezo', 'huber') or not (0 <= x < width and 0 <= y < height):
            raise ValueError('Invalid stack or image coordinates')
        cfg = self.config
        field = 'matrix' if stack == 'piezo' else 'huber_matrix'
        if field not in cfg.calibration.model_fields_set or 'center_px' not in cfg.beam.model_fields_set:
            raise ValueError('No saved calibration matrix or beam center in the selected YAML')
        center = cfg.beam.center_px
        if not (0 <= center[0] < width and 0 <= center[1] < height):
            raise ValueError('Configured beam center is outside this image')
        chi = self.read_chi('huber')
        if stack == 'piezo':
            chi += self.read_chi('piezo')
        try:
            model = CalibrationModel(getattr(cfg.calibration, field))
            dx, dy = model.click_to_motor_delta((x, y), center, chi_deg=chi)
        except LinAlgError as exc:
            raise ValueError('Saved matrix is singular') from exc
        if not all(map(math.isfinite, (dx, dy))):
            raise ValueError('Invalid calibration result')
        return dict(stack=stack, x=x, y=y, center=center, chi=chi, dx=float(dx), dy=float(dy),
                    units=cfg.ui.motor_units if stack == 'piezo' else 'mm')


class ReviewedXY:
    """At most one current preview per launcher. Only the opaque ID comes back from the UI."""

    def __init__(self, limit):
        if not math.isfinite(limit) or not 0 < limit <= 100:
            raise ValueError('--click-move-max-um must be in (0, 100]')
        self.limit = limit
        self.current = None

    async def offer(self, camera, result, size):
        self.current = None
        if result['stack'] != 'piezo':
            return
        if result['units'] not in ('um', 'µm', 'μm'):
            result['move_note'] = 'Preview only: calibration units must be micrometres'
            return
        dx, dy = result['dx'], result['dy']
        if max(abs(dx), abs(dy)) > self.limit:
            result['move_note'] = f'Preview exceeds the {self.limit:g} µm per-axis limit'
            return
        x0, y0 = await asyncio.to_thread(camera.read_xy)
        token = str(uuid4())
        self.current = (token, time.monotonic(), size, dict(result), [x0, y0, dx, dy])
        result.update(move_id=token, origin=[x0, y0], target=[x0+dx, y0+dy])

    async def commit(self, camera, controls, token):
        entry = self.current
        if not entry or token != entry[0]:
            raise Refused('Preview replaced or already used; click the current frame')
        self.current = None  # consume before any await; never replay on timeout
        _, stamp, size, p, args = entry
        if time.monotonic() - stamp > 30:
            raise Refused('Preview expired; click again')
        latest = await asyncio.to_thread(camera.preview, 'piezo', p['x'], p['y'], *size)
        if not math.isclose(latest['chi'], p['chi'], abs_tol=.001, rel_tol=0):
            raise Refused('Chi changed since preview; click again')
        return await controls.submit_named('move_piezo_xy', args,
            value=f'ΔX {args[2]:+.4f}, ΔY {args[3]:+.4f} µm')


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
                            click_move_enabled=self.settings.get('reviewed_xy') is not None,
                            bookmarks_enabled=getattr(backend, 'bookmarks', None) is not None,
                            bookmark_goto_max_um=getattr(backend, 'bookmark_goto_max_um', None),
                            queue_enabled=bool(backend.queue_controls),
                            queue_message=backend.queue_controls.message if backend.queue_controls else '',
                            command=ctrl.command if ctrl else None, exposure_rbv=rbv,
                            confirmation=ctrl.exposure_confirmation(rbv, stamp) if ctrl else None))
        elif resource == 'click-preview':
            camera = self.settings.get('live_camera')
            if camera is None:
                raise HTTPError(404)
            try:
                result = await asyncio.to_thread(camera.preview, self.get_argument('stack'),
                    float(self.get_argument('x')), float(self.get_argument('y')),
                    int(self.get_argument('width')), int(self.get_argument('height')))
                review = self.settings.get('reviewed_xy')
                if review is not None:
                    await review.offer(camera, result, (int(self.get_argument('width')), int(self.get_argument('height'))))
            except ValueError as exc:
                self.set_status(409)
                self.write({'error': str(exc)})
                return
            except (OSError, RuntimeError, TypeError):
                raise HTTPError(503, reason='Chi readback unavailable')
            self.write(result)
        elif resource == 'piezo-xy' and self.settings.get('reviewed_xy') is not None:
            try:
                self.write({'xy': await asyncio.to_thread(self.settings['live_camera'].read_xy)})
            except (OSError, RuntimeError, TypeError, ValueError):
                raise HTTPError(503, reason='XY readback unavailable')
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
        if resource == 'click-move':
            review = self.settings.get('reviewed_xy')
            if review is None:
                raise HTTPError(405)
            try:
                body = json.loads(self.request.body)
                if not isinstance(body, dict) or set(body) != {'move_id'}:
                    raise HTTPError(400)
                cmd = await review.commit(self.settings['live_camera'], backend.controls, body['move_id'])
            except (ValueError, KeyError) as exc:
                self.set_status(409)
                self.write({'error': str(exc)})
                return
            except (OSError, HTTPClientError, RuntimeError, TypeError):
                raise HTTPError(503)
            self.write(cmd)
            return
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
            body = json.loads(self.request.body)
            cmd = await backend.controls.submit(resource, body['value'], jog_device=body.get('device'),
                                                exposure_rbv=self.readback(backend))
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
    review = ReviewedXY(args.click_move_max_um) if args.click_move_max_um is not None else None
    if review and not args.live_camera_config:
        raise SystemExit('--click-move-max-um requires the calibrated live camera configuration')
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
                                             axis=args.jog_axis, step=args.jog_step, units=args.jog_units,
                                             piezo_jogs=args.piezo_jogs, angular_step=args.angular_step_deg)
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
            camera = backend = Simulator(piezo_jogs=args.piezo_jogs, step=args.jog_step,
                                         angular_step=args.angular_step_deg)
            await backend.controls.discover()
        tasks.append(asyncio.create_task(update_frame(camera, cache)))
        key = secrets.token_urlsafe(32)
        app = Application([(r'/api/([^/]+)', API), (r'/(.*)', StaticFileHandler,
                          {'path': str(root), 'default_filename': 'index.html'})],
                           backend=backend, cache=cache, session_key=key,
                           reviewed_xy=review,
                           live_camera=camera if args.live_camera_config else None)
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
    jog_mode = parser.add_mutually_exclusive_group()
    jog_mode.add_argument('--jog-axis', help='One verified sample-stage worker motor (piezo XYZ: 100 um; theta/chi: 0.1 deg)')
    jog_mode.add_argument('--piezo-jogs', action='store_true', help='Show X/Y/Z and theta/chi in one panel')
    parser.add_argument('--angular-step-deg', type=float, help='Override the 0.1 degree theta/chi jog step')
    parser.add_argument('--click-move-max-um', type=float,
                        help='Enable reviewed piezo XY moves up to this per-axis limit (maximum 100 um)')
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
