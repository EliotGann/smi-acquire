"""Workstation metadata edits using the same db=2 records as worker plans."""

import json
import math
from copy import deepcopy
from uuid import uuid4

from redis.exceptions import WatchError
from smi_plans import Holder, Position, Sample, SampleStore
from smi_plans._store import SCHEMA_VERSION


from .bookmark_view import validate_request


def redis_transaction(client, keys, edit):
    """Apply a bounded SampleStore edit atomically, including its secondary indexes."""
    keys = ['schema_version', *keys]
    full = ['swaxssamples' + k for k in keys]  # RedisJSONDict concatenates without a separator.
    with client.pipeline() as pipe:
        try:
            pipe.watch(*full)
            before = {k: json.loads(raw) for k, raw in zip(keys, pipe.mget(full)) if raw is not None}
            if before.get('schema_version', SCHEMA_VERSION) != SCHEMA_VERSION:
                raise ValueError('Unsupported sample store schema')
            # Models may mutate nested lists, so isolate the staged copy from the original.
            after = deepcopy(before)
            edit(SampleStore(after))
            if after.keys() - set(keys):
                raise ValueError('Unexpected sample-store key')
            pipe.multi()
            for k in keys:
                if k in after and after[k] != before.get(k):
                    pipe.set('swaxssamples' + k, json.dumps(after[k], allow_nan=False))
            pipe.execute()
        except WatchError as exc:
            raise ValueError('Bookmark changed in another client; refresh before editing') from exc


def editor(transact, read_pose):
    def edit_bookmark(spec):
        spec = validate_request(spec)
        op = spec['op']
        if op == 'goto':
            raise ValueError('Motion is a QueueServer plan, not a Redis edit')
        hid = uuid4().hex if op == 'holder' else spec['holder_id']
        sid = spec['sample_id'] if 'sample_id' in spec else uuid4().hex
        name = spec.get('name')
        md = spec.get('md', {})
        pose = {}
        if op in ('capture', 'assign'):
            pose = read_pose()
            if not {'piezo_x', 'piezo_y', 'piezo_z'} <= pose.keys() or not all(map(math.isfinite, pose.values())):
                raise ValueError('Missing or invalid stage readback; no bookmark saved')
        keys = ['magazine', 'holder:' + hid]
        if op != 'holder':
            keys += ['sample:' + sid, 'index:name']

        def edit(store):
            mag = store.magazine()
            if op == 'holder':
                store.put_holder(Holder(id=hid, name=name))
                mag.holder_ids.append(hid)
                store._put_magazine(mag)
                return
            if hid not in mag.holder_ids:
                raise ValueError('Holder is no longer in this magazine')
            holder = store.get_holder(hid)
            if op == 'capture':
                if name in store._name_index():
                    raise ValueError('Sample name already indexed; choose a distinct name')
                # Position has no piezo_chi field in schema 1. Keep that readback explicitly.
                p = Position(frame='lab', **{k: v for k, v in pose.items() if k != 'piezo_chi'})
                sample = Sample(id=sid, name=name, holder_id=hid, nominal=p,
                                md={'capture_piezo_chi': pose['piezo_chi']} if 'piezo_chi' in pose else {})
                store.put_sample(sample)
                holder.sample_ids.append(sid)
                store.put_holder(holder)
                return
            sample = store.get_sample(sid)
            if (sample.holder_id != hid or sid not in holder.sample_ids
                    or sample.updated != spec['updated']):
                raise ValueError('Sample changed; refresh and reselect it')
            if op == 'metadata':
                sample.md.update(md)
                store.put_sample(sample)
            elif op == 'assign':
                if sample.refined is not None:
                    raise ValueError('Refined position exists; review alignment before replacing nominal')
                sample.nominal = Position(frame='lab', **{k: v for k, v in pose.items() if k != 'piezo_chi'})
                if 'piezo_chi' in pose:
                    sample.md['capture_piezo_chi'] = pose['piezo_chi']
                store.put_sample(sample)
            else:
                store.set_active_sample(sid)  # Intent only; selecting does not move any motor.

        transact(keys, edit)
        return {'state': 'saved', 'holder_id': hid, 'sample_id': sid if op != 'holder' else None}

    return edit_bookmark
