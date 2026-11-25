"""Paginated view of the profile's db=2 swaxssamples records."""

import json
import math
from pathlib import Path
import re


def validate_request(spec):
    fields = {'holder': {'op', 'name'}, 'capture': {'op', 'name', 'holder_id'},
              'assign': {'op', 'holder_id', 'sample_id', 'updated'},
              'goto': {'op', 'holder_id', 'sample_id', 'updated'},
              'metadata': {'op', 'holder_id', 'sample_id', 'updated', 'md'},
              'active': {'op', 'holder_id', 'sample_id', 'updated'}}
    if not isinstance(spec, dict) or fields.get(spec.get('op')) != set(spec):
        raise ValueError('Unknown bookmark operation or fields')
    for k in ('holder_id', 'sample_id'):
        if k in spec and (not isinstance(spec[k], str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,128}', spec[k])):
            raise ValueError('Invalid record ID')
    if 'name' in spec:
        if not isinstance(spec['name'], str) or not 0 < len(spec['name'].strip()) <= 128:
            raise ValueError('Enter a name (1–128 characters)')
        spec = dict(spec, name=spec['name'].strip())
    if 'updated' in spec and (type(spec['updated']) not in (float, int) or not math.isfinite(spec['updated'])):
        raise ValueError('Refresh the sample before editing')
    if 'md' in spec:
        md = spec['md']
        if not isinstance(md, dict) or not md or set(md) - {'project_name', 'notes', 'priority'}:
            raise ValueError('Select an editable metadata field')
        for k, v in md.items():
            if k == 'priority':
                if type(v) is not int or abs(v) > 1000000:
                    raise ValueError('Priority must be an integer between -1000000 and 1000000')
            elif not isinstance(v, str) or len(v) > 2000:
                raise ValueError('Text metadata must be at most 2000 characters')
    return spec


class BookmarkView:
    PAGE = 40

    def __init__(self, read_many):
        self.read_many = read_many

    @classmethod
    def live(cls, read_pose):
        from functools import partial
        from .bookmark_edit import editor, redis_transaction
        from redis import Redis
        from redis.backoff import NoBackoff
        from redis.retry import Retry
        client = Redis(host='xf12id2-smi-redis1.nsls2.bnl.gov', port=6380, db=2, ssl=True,
                       password=Path('/etc/bluesky/redis.secret').read_text().strip(),
                       socket_timeout=2, socket_connect_timeout=2, max_connections=3,
                       retry=Retry(NoBackoff(), 0))

        def read_many(keys):
            return [json.loads(v) if v is not None else None
                    for v in client.mget(['swaxssamples' + key for key in keys])]
        view = cls(read_many)
        view.edit = editor(partial(redis_transaction, client), read_pose)
        return view, client.close

    def page(self, holder=None, offset=0):
        if offset < 0 or (holder and not re.fullmatch(r'[a-zA-Z0-9_-]{1,128}', holder)):
            raise ValueError('Invalid holder or offset')
        version, magazine = self.read_many(['schema_version', 'magazine'])
        if version not in (None, 1):
            raise ValueError('Unsupported sample store schema')
        magazine = magazine or {}
        ids = magazine.get('holder_ids', [])
        kind = 'holder'
        if holder:
            if holder not in ids:
                raise ValueError('Holder is no longer in this magazine')
            record, = self.read_many(['holder:' + holder])
            if not record or record.get('id') != holder:
                raise ValueError('Missing holder record')
            ids, kind = record.get('sample_ids', []), 'sample'
        selected = ids[offset:offset + self.PAGE]
        records = self.read_many([f'{kind}:{i}' for i in selected]) if selected else []
        rows = []
        for uid, row in zip(selected, records):
            if not row or row.get('id') != uid or (holder and row.get('holder_id') != holder):
                continue
            result = {'id': uid, 'name': row.get('name'), 'updated': row.get('updated')}
            if holder:
                result.update(nominal=row.get('nominal'), refined=row.get('refined'),
                              md={k: v for k, v in (row.get('md') or {}).items()
                                  if k in ('notes', 'priority', 'project_name', 'capture_piezo_chi')})
            else:
                result['count'] = len(row.get('sample_ids', []))
            rows.append(result)
        return dict(rows=rows, total=len(ids), page_size=self.PAGE, offset=offset,
                    active=magazine.get('active_sample_id'), mounted=magazine.get('measurement_holder_id'),
                    missing=len(selected)-len(rows))
