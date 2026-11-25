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


def test_live_submission_uses_only_immediate_endpoint_and_preserves_metadata(monkeypatch):
    client = Mock()
    client.fetch = AsyncMock(return_value=SimpleNamespace(body=b'{"success":true,"item":{"item_uid":"received"}}'))
    monkeypatch.setattr('smi_acquire.workstation.AsyncHTTPClient', lambda **kw: client)
    _, immediate, close = live_transport('https://example.invalid', 'fixture-key')
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
