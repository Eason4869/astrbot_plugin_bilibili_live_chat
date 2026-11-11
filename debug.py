"""Bounded room-local diagnostics; contains public input and parsed replies only."""

import time
from collections import OrderedDict
from copy import deepcopy

STAGES = ('collection', 'transcription_queue', 'transcription', 'queue', 'decision', 'model', 'rate_limit', 'send', 'total')
PENDING = frozenset(('queued', 'judging', 'generating', 'waiting', 'sending'))


class RoomDebug:
    def __init__(self, clock, preview=False):
        self.clock = clock
        self.preview = preview
        self.entries = OrderedDict()
        self.sequence = 0

    def add(self, message):
        now = self.clock()
        self.sequence += 1
        timing = dict.fromkeys(STAGES)
        timing.update(collection=message.collection_ms, transcription=message.transcription_ms,
                      transcription_queue=message.transcription_queue_ms)
        self.entries[message.key] = {
            'id': self.sequence, 'at': int(time.time() * 1000), 'source': message.source,
            'channel': message.channel,
            'name': message.name[:80], 'text': message.text[:240], 'keyword': '', 'mode': '',
            'status': 'queued', 'reason': '', 'reply': '', 'decision': '', 'preview': self.preview,
            'timings': timing, '_offered': now,
            '_captured': message.captured_at if message.captured_at is not None else now,
        }
        while len(self.entries) > 50:
            self.entries.popitem(last=False)

    def update(self, key, status=None, reason=None, **values):
        entry = self.entries.get(key)
        if entry is None:
            return
        if status is not None:
            if status == 'waiting' and entry['status'] != 'waiting':
                entry['_waiting'] = self.clock()
            elif status != 'waiting' and '_waiting' in entry:
                self.elapsed(key, 'rate_limit', entry.pop('_waiting'))
            entry['status'] = status
        if reason is not None:
            entry['reason'] = reason
        for name in ('mode', 'keyword', 'reply', 'decision'):
            if name in values:
                entry[name] = values[name]
        if status is not None and status not in PENDING:
            entry['timings']['total'] = round(max(0, self.clock() - entry['_captured']) * 1000, 1)

    def elapsed(self, key, stage, started):
        entry = self.entries.get(key)
        if entry:
            entry['timings'][stage] = round(max(0, self.clock() - started) * 1000, 1)

    def snapshot(self):
        events = []
        for entry in self.entries.values():
            row = deepcopy({key: value for key, value in entry.items() if not key.startswith('_')})
            if row['status'] == 'queued':
                row['timings']['queue'] = round(max(0, self.clock() - entry['_offered']) * 1000, 1)
            if '_waiting' in entry:
                row['timings']['rate_limit'] = round(max(0, self.clock() - entry['_waiting']) * 1000, 1)
            events.append(row)
        statistics = {}
        for status in ('sent', 'preview'):
            completed = [row for row in events if row['status'] == status]
            averages = {}
            for stage in STAGES:
                samples = [row['timings'][stage] for row in completed if row['timings'][stage] is not None]
                averages[stage] = round(sum(samples) / len(samples), 1) if samples else None
            statistics[status] = {'count': len(completed), 'averages': averages}
        return {'events': events, 'limit': 50, 'statistics': statistics}

    def cancel_pending(self, reason):
        for key, entry in self.entries.items():
            if entry['status'] in PENDING:
                self.update(key, 'cancelled', reason)
