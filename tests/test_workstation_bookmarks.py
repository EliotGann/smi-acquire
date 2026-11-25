"""Shared bookmark contract: same records as smi_plans; only load_sample goes to QS."""

from functools import partial
import json
import shutil
import subprocess
import time
from unittest.mock import AsyncMock

import pytest
from redis import Redis
from redis.exceptions import ConnectionError
from redis_json_dict import RedisJSONDict
from smi_plans import Position, SampleStore
from tornado.testing import AsyncHTTPTestCase
from tornado.web import Application

from smi_acquire.bookmark_edit import editor, redis_transaction
from smi_acquire.bookmark_view import BookmarkView
from smi_acquire.workstation import API, Simulator


@pytest.fixture
def isolated_redis(tmp_path):
    exe = shutil.which('redis-server')
    if not exe:
        pytest.skip('redis-server needed for isolated two-client integration')
    path = str(tmp_path / 'redis.sock')
    proc = subprocess.Popen([exe, '--port', '0', '--unixsocket', path, '--save', '',
                             '--appendonly', 'no'], cwd=tmp_path, stdout=subprocess.DEVNULL)
    client = Redis(unix_socket_path=path, db=2, socket_timeout=1, socket_connect_timeout=1)
    try:
        for _ in range(40):
            try:
                if client.ping():
                    break
            except ConnectionError:
                time.sleep(.05)
        yield client, path
    finally:
        client.close()
        proc.terminate()
        proc.wait(timeout=5)


def test_finch_edits_visible_to_independent_worker_store(isolated_redis):
    client, path = isolated_redis
    worker_client = Redis(unix_socket_path=path, db=2, socket_timeout=1)
    try:
        worker = SampleStore(RedisJSONDict(worker_client, 'swaxssamples'))
        pose = {'piezo_x':1500., 'piezo_y':100., 'piezo_z':60., 'piezo_chi':5., 'stage_phi':2.}
        edit = editor(partial(redis_transaction, client), lambda: dict(pose))
        hid = edit({'op':'holder', 'name':'Test bar'})['holder_id']
        sid = edit({'op':'capture', 'holder_id':hid, 'name':'Sample B'})['sample_id']
        assert worker.get_holder(hid).sample_ids == [sid]
        sample = worker.get_sample(sid)
        assert worker.find_sample('Sample B').id == sid
        assert sample.nominal.frame == 'lab' and sample.nominal.piezo_x == 1500
        assert sample.nominal.stage_phi == 2. and sample.md['capture_piezo_chi'] == 5.
        op = {'holder_id':hid,'sample_id':sid, 'updated':sample.updated}
        edit(dict(op, op='active'))
        assert worker.get_active_sample().id == sid and worker.magazine().measurement_holder_id is None
        edit(dict(op, op='metadata', md={'notes':'from Finch'}))
        assert worker.get_sample(sid).md['notes'] == 'from Finch'
        with pytest.raises(ValueError, match='changed'):
            edit(dict(op, op='metadata', md={'notes':'stale'}))
        fresh = worker.get_sample(sid)
        fresh.refined = Position(frame='lab', piezo_x=1499.)
        worker.put_sample(fresh)
        with pytest.raises(ValueError, match='Refined position'):
            edit(dict(op, op='assign', updated=fresh.updated))
        read = BookmarkView(lambda keys: [json.loads(v) if v else None for v in
                                         client.mget(['swaxssamples'+k for k in keys])])
        assert read.page(hid)['rows'][0]['refined']['piezo_x'] == 1499.

        def intervene(staged):
            staged.set_active_sample(None)
            worker.set_measurement_holder(hid)
        with pytest.raises(ValueError, match='another client'):
            redis_transaction(client, ['magazine'], intervene)
        assert worker.magazine().measurement_holder_id == hid and worker.get_active_sample().id == sid
    finally:
        worker_client.close()


class TestBookmarkHTTP(AsyncHTTPTestCase):
    def get_app(self):
        self.sim = Simulator()
        return Application([(r'/api/([^/]+)', API)], backend=self.sim, session_key='test', cache={})

    def test_metadata_is_not_a_queue_item_and_recall_needs_explicit_binding(self):
        headers = {'X-Workstation-Session':'test'}
        def post(spec):
            return self.fetch('/api/bookmark', method='POST', body=json.dumps(spec), headers=headers)
        hid = json.loads(post({'op':'holder','name':'bar'}).body)['holder_id']
        sid = json.loads(post({'op':'capture','holder_id':hid,'name':'B'}).body)['sample_id']
        record = json.loads(self.fetch('/api/bookmarks?holder='+hid, headers=headers).body)['rows'][0]
        spec = dict(holder_id=hid, sample_id=sid, updated=record['updated'])
        assert post(dict(spec, op='metadata', md={'priority':1})).code == 200
        assert not self.sim.history
        assert post(dict(spec, op='goto')).code == 409
        self.sim.bookmark_goto_max_um = 2.
        self.sim.controls.submit_named = AsyncMock(return_value={'state':'accepted'})
        assert post(dict(spec, op='goto')).code == 200
        self.sim.controls.submit_named.assert_awaited_once_with('load_sample',
            [hid,sid,record['updated'],2.], value='goto piezo XYZ')
