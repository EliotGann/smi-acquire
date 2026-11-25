import asyncio
from unittest.mock import AsyncMock

import pytest

from smi_acquire.commissioning import Commissioning, Refused, Rejected


def fixture():
    status = dict(manager_state='idle', re_state='idle', worker_environment_exists=True,
                  worker_environment_state='idle')
    devices = {'cam': {'components': {'exposure': {'is_movable': True}}},
               'axis': {'is_movable': True}}
    replies = {'plans/allowed': {'plans_allowed': {'mv': {}, 'mvr': {}}},
               'devices/allowed': {'devices_allowed': devices}, 'status': status}
    send = AsyncMock(return_value={'item': {'item_uid': 'receipt'}})
    ctl = Commissioning(AsyncMock(side_effect=replies.__getitem__), send,
                        exposure='cam.exposure', axis='axis', step=.01, units='sim')
    return ctl, replies, send


def test_receipt_not_completion_and_fresh_readback_independent():
    ctl, _, send = fixture()
    command = asyncio.run(ctl.submit('exposure', .15, exposure_rbv=.1))
    assert command['state'] == 'accepted'
    assert send.await_args.args[0]['args'] == ['cam.exposure', .15]
    with pytest.raises(Refused):
        asyncio.run(ctl.submit('jog', .01))
    done = {'exit_status': 'completed'}
    ctl.reconcile([{'item_uid': 'unrelated', 'result': done}])
    assert command['state'] == 'accepted'
    ctl.reconcile([{'item_uid': 'receipt', 'result': done}])
    assert command['state'] == 'finished'
    assert ctl.exposure_confirmation(.15, ctl.finished_at - .1) == 'awaiting fresh readback'
    assert ctl.exposure_confirmation(.1, ctl.finished_at + .1) == 'differs from request'
    assert ctl.exposure_confirmation(.15, ctl.finished_at + .1) == 'matches request'


def test_lost_ack_reconciled_by_tag_without_retry():
    ctl, _, send = fixture()
    send.side_effect = TimeoutError()
    cmd = asyncio.run(ctl.submit('jog', -.01))
    assert cmd['state'] == 'unknown'
    with pytest.raises(Refused):
        asyncio.run(ctl.submit('jog', -.01))
    ctl.reconcile([dict(item_uid='worker-uid', meta={'smi_acquire_command': cmd['tag']},
                        result={'exit_status': 'failed', 'msg': 'motor limit'})])
    assert cmd['uid'] == 'worker-uid' and cmd['outcome'] == 'failed'
    assert send.await_count == 1


@pytest.mark.parametrize('failure', ['busy', 'closed', 'locked', 'unknown state',
                                     'missing plan', 'missing component', 'unbound', 'no RBV'])
def test_refuse_before_dispatch(failure):
    ctl, replies, send = fixture()
    status = replies['status']
    if failure == 'busy':
        status['manager_state'] = 'executing_queue'
    elif failure == 'closed':
        status['worker_environment_exists'] = False
    elif failure == 'locked':
        status['lock'] = {'environment': True}
    elif failure == 'unknown state':
        status.pop('re_state')
    elif failure == 'missing plan':
        replies['plans/allowed']['plans_allowed'].pop('mv')
    elif failure == 'missing component':
        replies['devices/allowed']['devices_allowed']['cam'].clear()
    elif failure == 'unbound':
        ctl.exposure = None
    with pytest.raises(Refused):
        asyncio.run(ctl.submit('exposure', .2, exposure_rbv=None if failure == 'no RBV' else .1))
    send.assert_not_awaited()


def test_rejection_and_out_of_bounds_have_distinct_results():
    ctl, _, send = fixture()
    for action, value in [('jog', .011), ('exposure', True), ('exposure', float('nan')),
                          ('exposure', 2.01)]:
        with pytest.raises(Refused):
            asyncio.run(ctl.submit(action, value, exposure_rbv=.1))
    send.assert_not_awaited()
    send.side_effect = Rejected('denied')
    assert asyncio.run(ctl.submit('jog', .01))['state'] == 'rejected'


@pytest.mark.parametrize('kernel', ['disabled', 'busy', 'starting'])
def test_python_worker_ready_but_active_kernel_blocks(kernel):
    ctl, replies, send = fixture()
    replies['status']['ip_kernel_state'] = kernel
    if kernel == 'disabled':
        assert asyncio.run(ctl.submit('exposure', .2, exposure_rbv=1))['state'] == 'accepted'
    else:
        with pytest.raises(Refused, match=kernel):
            asyncio.run(ctl.submit('exposure', .2, exposure_rbv=1))
        send.assert_not_awaited()
