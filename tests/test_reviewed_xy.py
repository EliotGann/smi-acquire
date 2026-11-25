import asyncio
import json
import time
from unittest.mock import AsyncMock, Mock

import pytest
from tornado.testing import AsyncHTTPTestCase
from tornado.web import Application

from smi_acquire.commissioning import Refused
from smi_acquire.workstation import API, ReviewedXY


def test_only_server_reviewed_pair_submitted_once():
    async def scenario():
        review = ReviewedXY(10)
        p = dict(stack='piezo', x=25, y=40, chi=2., dx=-3., dy=5., units='um')
        camera = Mock()
        camera.read_xy.return_value = [1500., 200.]
        camera.preview.return_value = dict(p)
        controls = AsyncMock()
        await review.offer(camera, p, (100,80))
        assert p['target'] == [1497.,205.]
        await review.commit(camera, controls, p['move_id'])
        submitted = controls.submit_named.await_args.args
        assert submitted == ('move_piezo_xy', [1500.,200.,-3.,5.])
        with pytest.raises(Refused):
            await review.commit(camera, controls, p['move_id'])
        controls.submit_named.assert_awaited_once()
    asyncio.run(scenario())


class TestReviewHTTP(AsyncHTTPTestCase):
    def get_app(self):
        self.camera = Mock()
        self.camera.preview.side_effect = lambda stack,x,y,w,h: dict(
            stack=stack, x=x, y=y, chi=0., dx=1., dy=-2., units='um')
        self.camera.read_xy.return_value = [100.,200.]
        backend = Mock()
        backend.controls.submit_named = AsyncMock(return_value={'state':'accepted', 'uid':'item'})
        return Application([(r'/api/([^/]+)', API)], backend=backend, session_key='fixture',
                           live_camera=self.camera, reviewed_xy=ReviewedXY(10))

    def test_click_never_submits_and_post_takes_only_single_use_review(self):
        headers = {'X-Workstation-Session':'fixture'}
        invoke = self._app.settings['backend'].controls.submit_named
        preview = self.fetch('/api/click-preview?stack=piezo&x=5&y=4&width=20&height=20', headers=headers)
        move = {'move_id':json.loads(preview.body)['move_id']}
        invoke.assert_not_awaited()
        def submit(body):
            return self.fetch('/api/click-move', method='POST', headers=headers, body=json.dumps(body))
        assert submit(dict(move, dx=75.)).code == 400
        assert submit(move).code == 200
        assert submit(move).code == 409
        assert invoke.await_count == 1 and invoke.await_args.args == ('move_piezo_xy',[100.,200.,1.,-2.])
        self._app.settings['reviewed_xy'] = None
        assert submit(move).code == 405


def test_huber_over_cap_expired_and_changed_geometry_never_submit(monkeypatch):
    async def scenario():
        review = ReviewedXY(10)
        camera, controls = Mock(), AsyncMock()
        camera.read_xy.return_value = [3.,5.]
        base = dict(stack='piezo', units='um', x=0., y=3., dx=1., dy=2., chi=0.)
        for update in [dict(stack='huber'), dict(units='mm'), dict(dx=-11.)]:
            p = dict(base, **update)
            await review.offer(camera, p, (100,80))
            assert 'move_id' not in p
        p = dict(base)
        await review.offer(camera, p, (100,80))
        camera.preview.return_value = dict(base, chi=2.)
        with pytest.raises(Refused, match='Chi changed'):
            await review.commit(camera, controls, p['move_id'])
        await review.offer(camera, p, (100,80))
        with monkeypatch.context() as m:
            now = time.monotonic()
            m.setattr(time, 'monotonic', lambda: now + 31.)
            with pytest.raises(Refused, match='expired'):
                await review.commit(camera, controls, p['move_id'])
        controls.submit_named.assert_not_awaited()
    asyncio.run(scenario())
