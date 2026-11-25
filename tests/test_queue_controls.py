import asyncio
from unittest.mock import AsyncMock

import pytest

from smi_acquire.commissioning import Refused
from smi_acquire.queue_controls import available
from smi_acquire.workstation import Simulator


def test_queue_stop_finishes_current_without_starting_next():
    async def scenario():
        sim = Simulator()
        now = [0.]
        sim.demo.clock = lambda: now[0]
        run = sim.queue_controls.run
        await run('start', sim.state['queue_revision'])
        running = sim.state['running_uid']
        await run('stop-after', running)
        assert sim.state['stop_pending'] and sim.state['re'] == 'running'
        await run('cancel-stop', running)
        assert not sim.state['stop_pending']
        await run('stop-after', running)
        now[0] += 16
        assert sim.state['manager'] == 'idle' and sim.state['count'] == 1
        assert sim.state['history'][0]['uid'] == running
    asyncio.run(scenario())


@pytest.mark.parametrize('pause', ['pause-deferred', 'pause-immediate'])
def test_pause_resume_preserves_item_and_queue(pause):
    async def scenario():
        sim = Simulator()
        run = sim.queue_controls.run
        await run('start', sim.state['queue_revision'])
        uid = sim.state['running_uid']
        await run(pause, uid)
        assert sim.state['re'] == 'paused' and sim.state['queue_actions'] == ['resume']
        with pytest.raises(Refused):
            await run('resume', 'a-different-plan')
        await run('resume', uid)
        assert sim.state['running_uid'] == uid and sim.state['re'] == 'running'
        assert sim.state['count'] == 1 and not sim.state['history']
    asyncio.run(scenario())


def test_reload_waits_for_close_and_preserves_queue():
    async def scenario():
        sim = Simulator()
        before = list(sim.state['queue'])
        await sim.queue_controls.run('reload', sim.state['queue_revision'])
        assert sim.state['idle'] and sim.state['queue'] == before
        assert list(sim.log)[-2:] == ['Simulator accepted environment/close',
                                     'Simulator accepted environment/open']
    asyncio.run(scenario())


def test_reload_refuses_busy_autostart_and_stale_queue():
    async def scenario():
        sim = Simulator()
        s = await sim.get('status')
        assert 'reload' not in available(dict(s, queue_autostart_enabled=True))
        with pytest.raises(Refused):
            await sim.queue_controls.run('start', 'stale')
        await sim.queue_controls.run('start', sim.state['queue_revision'])
        with pytest.raises(Refused):
            await sim.queue_controls.run('reload', sim.state['queue_revision'])
        assert sim.state['running_uid'] and sim.demo.open
    asyncio.run(scenario())


def test_reload_does_not_open_or_retry_after_lost_close_ack():
    async def scenario():
        sim = Simulator()
        control = sim.queue_controls
        control.post = AsyncMock(side_effect=TimeoutError())
        with pytest.raises(TimeoutError):
            await control.run('reload', sim.state['queue_revision'])
        control.post.assert_awaited_once_with('environment/close', {})
        assert 'unknown' in control.message
    asyncio.run(scenario())
