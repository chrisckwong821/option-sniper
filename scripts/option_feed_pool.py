"""Bounded live option connections with incremental additions and warm replacement."""
import asyncio
import math
from datetime import datetime, timezone
from time import monotonic


def partition_symbols(symbols, previous=None, shard_count=6, capacity=1000):
    """Keep a ticker's calls/puts together and preserve its connection assignment."""
    groups = {}
    for symbol in sorted(set(symbols)):
        groups.setdefault(symbol[:6].strip(), set()).add(symbol)
    bins = [set() for _ in range(shard_count)]
    assignments = {}
    pending = []
    for root, group in sorted(groups.items(), key=lambda item: (-len(item[1]), item[0])):
        old = (previous or {}).get(root)
        if old is not None and old < shard_count and len(bins[old]) + len(group) <= capacity:
            bins[old].update(group)
            assignments[root] = old
        else:
            pending.append((root, group))
    for root, group in pending:
        index = min(range(shard_count), key=lambda i: len(bins[i]))
        if len(bins[index]) + len(group) > capacity:
            raise ValueError('Option connection capacity exceeded; retaining previous subscriptions')
        bins[index].update(group)
        assignments[root] = index
    return bins, assignments


class QuoteSink:
    def __init__(self, pool, index, desired):
        self.pool, self.index = pool, index
        self.desired = set(desired)
        self.ready = asyncio.Event()
        self.quote_count = 0
        self.last_quote_at = None
        self.last_event_at = None
        self.status = 'connecting'
        self.error = None
        self.provider_error_count = 0
        self.last_provider_error = None
        self.started = monotonic()

    def set_feed_status(self, dataset, state, detail=''):
        self.status = state
        if state in ('degraded', 'retrying'):
            self.error = detail
            self.provider_error_count += 1
            self.last_provider_error = detail

    def touch_feed(self, dataset):
        # Quote updates below own freshness. Protocol messages are not quotes.
        pass

    def update_option_quote(self, **quote):
        if quote['raw_symbol'] not in self.desired or quote['raw_symbol'] not in self.pool.symbols:
            return False
        bid, ask = quote['bid'], quote['ask']
        if not (math.isfinite(bid) and math.isfinite(ask) and 0 <= bid <= ask and ask > 0):
            return False
        now = datetime.now(timezone.utc)
        age = (now - quote['event_time']).total_seconds()
        if not -10 <= age <= 120:
            return False
        self.quote_count += 1
        self.last_quote_at = now.isoformat()
        self.last_event_at = quote['event_time'].isoformat()
        self.status, self.error = 'live', None
        self.ready.set()
        self.pool.store.touch_feed('OPRA.PILLAR')
        return self.pool.store.update_option_quote(**quote)

    def health(self):
        age = ((datetime.now(timezone.utc) - datetime.fromisoformat(self.last_quote_at)).total_seconds()
               if self.last_quote_at else None)
        state = 'stale' if age is not None and age > 180 else self.status
        return dict(state=state, quote_count=self.quote_count,
                    last_quote_at=self.last_quote_at, last_quote_event_at=self.last_event_at,
                    error=self.error, provider_error_count=self.provider_error_count,
                    last_provider_error=self.last_provider_error)


class OptionFeedPool:
    def __init__(self, api_key, symbols, store, stop, symbol_provider, feed_factory,
                 shard_count=6, capacity=1000, refresh_seconds=60,
                 replacement_timeout=120, retirement_seconds=600, start_spacing=1.1):
        self.api_key, self.store, self.stop = api_key, store, stop
        self.symbol_provider, self.feed_factory = symbol_provider, feed_factory
        self.shard_count, self.capacity = shard_count, capacity
        self.refresh_seconds, self.replacement_timeout = refresh_seconds, replacement_timeout
        self.retirement_seconds, self.start_spacing = retirement_seconds, start_spacing
        self.symbols = set(symbols)
        self.assignments = {}
        self.active = {}
        self.candidate = None
        self.rotation_task = None
        self.targets = {}
        self.last_plan = {}
        self.rotations = 0
        self.rotation_failures = 0
        self.last_attempt = {}

    async def _start(self, index, desired):
        sink = QuoteSink(self, index, desired)
        local_stop = asyncio.Event()
        feed = self.feed_factory(api_key=self.api_key, symbols=sorted(desired), store=sink,
                                 replay_minutes=0, stop=local_stop,
                                 subscription_chunk_size=self.capacity)
        task = asyncio.create_task(feed.run(), name=f'option-quote-shard-{index}')
        return dict(feed=feed, sink=sink, stop=local_stop, task=task)

    async def _close(self, entry):
        entry['stop'].set()
        client = entry['feed'].client
        if client is not None:
            try:
                client.terminate()
            except Exception:
                pass
        entry['task'].cancel()
        await asyncio.gather(entry['task'], return_exceptions=True)

    async def _replace(self, index, desired):
        old = self.active[index]
        self.last_attempt[index] = monotonic()
        candidate = await self._start(index, desired)
        self.candidate = candidate
        try:
            await asyncio.wait_for(candidate['sink'].ready.wait(), self.replacement_timeout)
        except asyncio.TimeoutError:
            self.rotation_failures += 1
            await self._close(candidate)
            self.candidate = None
            return False
        # Old remains live through initialization; retire only after a usable quote.
        if self.active.get(index) is not old:
            await self._close(candidate)
            self.candidate = None
            return False
        candidate['sink'].desired = self.targets.get(index, desired)
        await self._close(old)
        self.active[index] = candidate
        self.candidate = None
        self.rotations += 1
        return True

    async def replan(self):
        desired = set(self.symbol_provider())
        if not desired:
            return
        bins, assignments = partition_symbols(desired, self.assignments, self.shard_count, self.capacity)
        self.last_plan = dict(checked_at=datetime.now(timezone.utc).isoformat(),
                              added=len(desired - self.symbols), removed=len(self.symbols - desired),
                              desired_contracts=len(desired))
        self.symbols, self.assignments = desired, assignments
        self.targets = {i: target for i, target in enumerate(bins)}
        rotation = None
        for index, target in enumerate(bins):
            entry = self.active.get(index)
            if not target:
                if entry:
                    await self._close(entry)
                    del self.active[index]
                continue
            if entry is None:
                self.active[index] = await self._start(index, target)
                await asyncio.sleep(self.start_spacing)
                continue
            feed, sink = entry['feed'], entry['sink']
            sink.desired = target  # Ignore retired strikes immediately, without reconnecting.
            additions = target - feed.symbols
            obsolete = feed.symbols - target
            if additions and len(feed.symbols | additions) <= self.capacity:
                # One incremental request; subsequent reconnects replay this bounded set.
                if feed.client is not None:
                    feed._subscribe(feed.client, sorted(additions), replay_start=None)
                    feed.symbols.update(additions)
            needs_rotation = len(feed.symbols | target) > self.capacity or (
                obsolete and monotonic() - sink.started >= self.retirement_seconds)
            if needs_rotation and monotonic() - self.last_attempt.get(index, float('-inf')) >= self.refresh_seconds:
                rotation = rotation if rotation is not None else (index, target)
        # At most one overlap connection. Other groups continue receiving quotes.
        if rotation is not None and (self.rotation_task is None or self.rotation_task.done()):
            self.rotation_task = asyncio.create_task(self._replace(*rotation), name='option-shard-replacement')

    def health(self):
        return dict(shard_limit=self.shard_count, contracts_per_connection_limit=self.capacity,
                    active_connections=len(self.active), replacement_connections=int(self.candidate is not None),
                    rotations=self.rotations, replacement_failures=self.rotation_failures,
                    shards=[dict(index=i, subscribed_contracts=len(e['feed'].symbols),
                                 desired_contracts=len(e['sink'].desired), **e['sink'].health())
                            for i, e in sorted(self.active.items())])

    def feed_health(self):
        shards = self.health()['shards']
        fresh = [s for s in shards if s['state'] == 'live']
        errors = [s['error'] for s in shards if s['error']]
        if self.last_plan.get('error'):
            errors.append(self.last_plan['error'])
        state = ('degraded' if errors or any(s['state'] == 'stale' for s in shards)
                 else 'live' if shards and len(fresh) == len(shards) else 'connecting')
        return dict(state=state, detail=f'{len(fresh)}/{len(shards)} connections receiving fresh quotes',
                    last_error='; '.join(errors) or None,
                    updated_at=datetime.now(timezone.utc).isoformat(),
                    last_record_at=max((s['last_quote_at'] for s in shards if s['last_quote_at']), default=None))

    async def run(self):
        try:
            bins, self.assignments = partition_symbols(self.symbols, shard_count=self.shard_count, capacity=self.capacity)
            self.store.set_feed_status('OPRA.PILLAR', 'connecting', 'Initializing split option subscriptions')
            for index, desired in enumerate(bins):
                if desired:
                    self.active[index] = await self._start(index, desired)
                    await asyncio.sleep(self.start_spacing)
            while not self.stop.is_set():
                try:
                    await asyncio.wait_for(self.stop.wait(), self.refresh_seconds)
                except asyncio.TimeoutError:
                    try:
                        await self.replan()
                    except Exception as exc:
                        self.last_plan['error'] = f'{type(exc).__name__}: {exc}'
        finally:
            if self.rotation_task:
                self.rotation_task.cancel()
                await asyncio.gather(self.rotation_task, return_exceptions=True)
            if self.candidate:
                await self._close(self.candidate)
            for entry in list(self.active.values()):
                await self._close(entry)
