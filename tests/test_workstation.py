"""Exercise the workstation data boundary without a service, PV, or workstation key."""

import asyncio
from io import BytesIO
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from PIL import Image
import pytest
from tornado.testing import AsyncHTTPTestCase
from tornado.web import Application

from smi_acquire.workstation import API, QueueMonitor, Simulator, live_transport, update_frame


def test_monitor_caches_revisions_and_distinguishes_console_loss_from_queue_loss():
    calls = []
    responses = {'status': {'manager_state': 'idle', 'plan_queue_uid': 'q1',
                            'plan_history_uid': 'h1', 'items_in_queue': 1},
                 'queue/get': {'items': [{'item_uid': 'pending', 'name': 'scan'}],
                               'plan_queue_uid': 'q1'},
                 'history/get': {'items': [{'item_uid': 'ran', 'result': {'exit_status': 'failed'}}],
                                 'plan_history_uid': 'h1'},
                 'console_output': {'text': 'worker output'}}

    async def get(path):
        calls.append(path)
        if path not in responses:
            raise OSError('disconnected')
        return responses[path]

    monitor = QueueMonitor(get)
    asyncio.run(monitor.refresh())
    assert monitor.state['history'][0]['outcome'] == 'failed'
    assert monitor.state['queue'][0]['uid'] == 'pending'
    assert monitor.state['console'] == 'worker output'
    del responses['console_output']
    calls.clear()
    asyncio.run(monitor.refresh())
    assert calls == ['status', 'console_output']
    assert monitor.state['connected'] and 'console' in monitor.state['errors']
    del responses['status']
    asyncio.run(monitor.refresh())
    assert not monitor.state['connected']


def test_simulated_plans_affect_readbacks_images_and_history():
    sim = Simulator()
    before = sim.image().tobytes()
    asyncio.run(sim.controls.submit('jog', .1))
    after_move = sim.image().tobytes()
    asyncio.run(sim.controls.submit('exposure', .2, exposure_rbv=.1))
    assert sim.x.get() == .1 and sim.exposure.get() == .2
    assert before != after_move != sim.image().tobytes()
    assert [i['name'] for i in sim.state['history']] == ['mv', 'mvr']
    assert len({i['uid'] for i in sim.state['history']}) == 2

    async def check_frame():
        cache = {}
        task = asyncio.create_task(update_frame(sim, cache))
        try:
            async with asyncio.timeout(3):
                while 'frame' not in cache:
                    await asyncio.sleep(.01)
            assert Image.open(BytesIO(cache['frame'])).size == (480, 320)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(check_frame())


def test_reject_cleartext_upstream_before_using_key():
    with pytest.raises(ValueError):
        live_transport('http://localhost', 'unused')


def test_click_preview_uses_legacy_display_pixels_and_stack_chi():
    import numpy as np
    from smi_acquire.microscope.config import AppConfig
    from smi_acquire.workstation import LiveCamera
    cam = LiveCamera.__new__(LiveCamera)
    cam.config = AppConfig.model_validate(dict(epics={'camera_prefix':'TEST:', 'motors':{}},
        ui={'display_max_dim':1280, 'motor_units':'um'}, beam={'center_px':[525,333]},
        calibration={'matrix':[[-.138,-.0044],[.0043,-.1384]],
                     'huber_matrix':[[-138,-4.9],[4.2,-138.5]]}))
    cam.stream = Mock()
    cam.stream.grab_rgb.return_value = np.zeros((2472,3296,3), dtype=np.uint8)
    cam.pva = False
    assert cam.image().size == (1099,824)
    cam.read_chi = lambda stack: 90 if stack=='piezo' else 0
    piezo = cam.preview('piezo',535,333,1099,824)
    assert (piezo['dx'],piezo['dy']) == pytest.approx((2.249175,-72.392055))
    huber = cam.preview('huber',535,333,1099,824)
    assert huber['units']=='mm' and (huber['dx'],huber['dy']) == pytest.approx((.07238583,.00219509), abs=1e-8)
    with pytest.raises(ValueError, match='dimensions'):
        cam.preview('piezo',535,333,824,618)


def test_piezo_panel_selects_one_motor_and_checks_its_own_step():
    from smi_acquire.commissioning import Refused

    async def exercise():
        sim = Simulator(piezo_jogs=True, angular_step=.025)
        caps = await sim.controls.discover()
        assert len(caps['jogs']) == 5
        for jog in caps['jogs']:
            name = jog['device']
            step = .025 if name.endswith(('_th', '_chi')) else 100.
            assert jog['step'] == step
            before = {k:m.get() for k,m in sim.motors.items()}
            result = await sim.controls.submit('jog', -step, jog_device=name)
            assert result['outcome'] == 'completed'
            assert {k:m.get() for k,m in sim.motors.items()} == {**before, name:before[name]-step}
        for device in ('sample_piezo_th', 'sample_piezo_chi', 'stage_x', None):
            with pytest.raises(Refused):
                await sim.controls.submit('jog', 100., jog_device=device)
        defaults = Simulator(piezo_jogs=True)
        await defaults.controls.submit('jog', 100, jog_device='sample_piezo_z')
        await defaults.controls.submit('jog', -.1, jog_device='sample_piezo_th')
        assert defaults.motors['sample_piezo_z'].get() == 100
        assert defaults.motors['sample_piezo_th'].get() == -.1
        await defaults.controls.submit('jog', .1, jog_device='sample_piezo_chi')
        assert defaults.motors['sample_piezo_chi'].get() == .1
        assert defaults.motors['sample_piezo_th'].get() == -.1

    asyncio.run(exercise())


def test_live_submission_uses_only_immediate_endpoint_and_preserves_metadata(monkeypatch):
    client = Mock()
    client.fetch = AsyncMock(return_value=SimpleNamespace(body=b'{"success":true,"item":{"item_uid":"received"}}'))
    monkeypatch.setattr('smi_acquire.workstation.AsyncHTTPClient', lambda **kw: client)
    _, immediate, _, close = live_transport('https://example.invalid', 'fixture-key')
    item = dict(item_type='plan', name='mv', args=['camera.exposure', .1],
                meta={'smi_acquire_command': 'fixture'})
    assert asyncio.run(immediate(item))['item']['item_uid'] == 'received'
    req = client.fetch.await_args.args[0]
    assert req.url == 'https://example.invalid/api/queue/item/execute'
    assert req.method == 'POST' and json.loads(req.body) == {'item': item}
    assert req.headers['Authorization'] == 'ApiKey fixture-key' and not req.follow_redirects
    close()


class TestLocalBoundary(AsyncHTTPTestCase):
    def get_app(self):
        return Application([(r'/api/([^/]+)', API)], backend=Simulator(),
                           cache={}, session_key='fixture-session')

    def test_immediate_action_needs_session_and_live_mode_refuses_it(self):
        headers = {'X-Workstation-Session': 'fixture-session'}
        assert self.fetch('/api/jog', method='POST', body='').code == 403
        response = self.fetch('/api/jog', method='POST', body='{"value":0.1}', headers=headers)
        assert json.loads(response.body)['outcome'] == 'completed'
        self._app.settings['backend'] = QueueMonitor(None)
        assert self.fetch('/api/jog', method='POST', body='', headers=headers).code == 405
        assert self.fetch('/api/state', headers=headers).code == 200

    def test_queue_action_requires_session_opt_in_and_displayed_revision(self):
        headers = {'X-Workstation-Session': 'fixture-session'}
        def start(expected, session=headers):
            return self.fetch('/api/queue-action', method='POST', headers=session,
                              body=json.dumps({'action': 'start', 'expected': expected}))
        snap = json.loads(self.fetch('/api/state', headers=headers).body)
        assert start(snap['queue_revision'], {}).code == 403
        assert start('stale').code == 409
        assert start(snap['queue_revision']).code == 200
        active = json.loads(self.fetch('/api/state', headers=headers).body)
        assert active['running_uid'] == snap['queue'][0]['uid']
        self._app.settings['backend'] = QueueMonitor(None)
        assert start(snap['queue_revision']).code == 405
