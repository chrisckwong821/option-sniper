import asyncio
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import Mock
from scripts.option_feed_pool import OptionFeedPool, QuoteSink, partition_symbols


def symbol(root, strike=100):
    return f'{root:<6}261002C{strike*1000:08d}'


class PartitionTests(unittest.TestCase):
    def test_complete_bounded_stable_groups(self):
        symbols = [symbol(f'T{i}', k) for i in range(400) for k in range(14)]
        bins, roots = partition_symbols(symbols)
        self.assertEqual(set.union(*bins), set(symbols))
        self.assertTrue(all(len(b) <= 1500 for b in bins))
        changed = symbols[1:] + [symbol('T0', 99)]
        new_bins, new_roots = partition_symbols(changed, roots)
        self.assertEqual(roots, new_roots)
        self.assertEqual(set.union(*new_bins), set(changed))

    def test_capacity_failure_is_explicit(self):
        with self.assertRaisesRegex(ValueError, 'capacity'):
            partition_symbols([symbol('A', k) for k in range(1501)])


class PoolTests(unittest.IsolatedAsyncioTestCase):
    def pool(self, desired, **kwargs):
        def factory(**args):
            feed = Mock(**args)
            feed.symbols = set(args['symbols'])
            feed.client = Mock()
            async def run():
                await args['stop'].wait()
            feed.run = run
            return feed
        return OptionFeedPool('test', desired, Mock(), asyncio.Event(), lambda: desired,
                              factory, shard_count=1, capacity=4, start_spacing=0, **kwargs)

    async def install(self, pool):
        pool.active[0] = await pool._start(0, pool.symbols)
        pool.assignments = {s[:6].strip(): 0 for s in pool.symbols}

    async def cleanup(self, pool):
        if pool.rotation_task:
            pool.rotation_task.cancel()
            await asyncio.gather(pool.rotation_task, return_exceptions=True)
        if pool.candidate:
            await pool._close(pool.candidate)
        for entry in pool.active.values():
            await pool._close(entry)

    async def test_minute_change_adds_and_filters_without_reconnect(self):
        a,b,c = symbol('A'),symbol('B'),symbol('C')
        desired=[a,b];p=self.pool(desired);await self.install(p)
        old=p.active[0];desired[:]=[b,c]
        await p.replan()
        old['feed'].client.terminate.assert_not_called()
        old['feed']._subscribe.assert_called_once_with(old['feed'].client,[c],replay_start=None)
        self.assertEqual(old['feed'].symbols,{a,b,c})
        self.assertEqual(old['sink'].desired,{b,c})
        old['sink'].update_option_quote(raw_symbol=a,bid=1,ask=2,event_time=datetime.now(timezone.utc))
        p.store.update_option_quote.assert_not_called()
        await self.cleanup(p)

    async def test_warm_replacement_preserves_old_until_actual_quote(self):
        desired=[symbol('A')];p=self.pool(desired,replacement_timeout=1)
        await self.install(p);old=p.active[0]
        task=asyncio.create_task(p._replace(0,set(desired)))
        await asyncio.sleep(0)
        candidate=p.candidate
        candidate['sink'].touch_feed('OPRA.PILLAR')
        old['feed'].client.terminate.assert_not_called()
        self.assertFalse(candidate['sink'].ready.is_set())
        candidate['sink'].update_option_quote(raw_symbol=desired[0],bid=1,ask=2,event_time=datetime.now(timezone.utc))
        self.assertTrue(await task)
        old['feed'].client.terminate.assert_called_once()
        self.assertIs(p.active[0],candidate)
        await self.cleanup(p)

    async def test_replacement_timeout_retains_old(self):
        desired=[symbol('A')];p=self.pool(desired,replacement_timeout=.01)
        await self.install(p);old=p.active[0]
        self.assertFalse(await p._replace(0,set(desired)))
        self.assertIs(p.active[0],old)
        old['feed'].client.terminate.assert_not_called()
        self.assertEqual(p.rotation_failures,1)
        await self.cleanup(p)

    async def test_capacity_rotation_is_single_background_task(self):
        desired=[symbol('A'),symbol('B'),symbol('C'),symbol('D')]
        p=self.pool(desired,replacement_timeout=1);await self.install(p)
        old=p.active[0];desired[:]=[symbol('A'),symbol('E'),symbol('F'),symbol('G')]
        await p.replan();task=p.rotation_task
        await asyncio.sleep(0)
        await p.replan()
        self.assertIs(p.rotation_task,task)
        old['feed'].client.terminate.assert_not_called()
        self.assertLessEqual(len(p.candidate['feed'].symbols),4)
        await self.cleanup(p)

    async def test_old_or_invalid_quotes_do_not_mark_ready(self):
        desired=[symbol('A')];p=self.pool(desired);sink=QuoteSink(p,0,desired)
        for bid,ask,age in [(2,1,0),(1,float('inf'),0),(1,2,300)]:
            sink.update_option_quote(raw_symbol=desired[0],bid=bid,ask=ask,event_time=datetime.now(timezone.utc)-timedelta(seconds=age))
        self.assertFalse(sink.ready.is_set())
        p.store.update_option_quote.assert_not_called()
