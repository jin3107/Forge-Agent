"""Injected hook denial in batch/battle must be reported without false success."""
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from engine import agent as mod, hooks


class TrustBatch(unittest.IsolatedAsyncioTestCase):
    def store(self):
        count=[0]
        def create(model='',title=''):
            count[0]+=1
            return SimpleNamespace(id=f's{count[0]}',title=title)
        return SimpleNamespace(create=create)

    async def test_denial_cancels_sibling_and_reports_failure(self):
        entered=asyncio.Event(); cancelled=asyncio.Event()
        async def send(sid,prompt):
            if sid=='s1':
                await entered.wait()
                raise hooks.HookTrustDenied()
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
            if False: yield {}
        def make(store,sid,**kwargs):
            return SimpleNamespace(send=lambda prompt:send(sid,prompt))
        with patch.object(mod,'Agent',side_effect=make):
            async def collect():
                return [e async for e in mod.run_batch(self.store(),[{'prompt':'a'},{'prompt':'b'}])]
            events=await asyncio.wait_for(collect(),1)
        self.assertTrue(cancelled.is_set())
        errors=[e['event'] for e in events if e['type']=='task_event' and e['event']['type']=='error']
        self.assertEqual({e['code'] for e in errors},{'hook_trust_denied','batch_interrupted'})
        self.assertTrue(all('may have run' in e['message'] for e in errors))
        done=[e['event'] for e in events if e['type']=='task_event' and e['event']['type']=='task_done']
        self.assertEqual(len(done),2)
        self.assertTrue(all(e['ok'] is False for e in done))
        self.assertFalse(next(e for e in events if e['type']=='batch_done')['ok'])

    async def test_battle_uses_same_denial_behavior(self):
        async def send(prompt):
            if False: yield {}
            raise hooks.HookTrustDenied()
        with patch.object(mod,'Agent',return_value=SimpleNamespace(send=send)):
            events=[e async for e in mod.run_battle(self.store(),'prompt',['a','b'],demo=False)]
        self.assertTrue(any(e['type']=='task_event' and e['event'].get('code')=='hook_trust_denied' for e in events))
        self.assertFalse(next(e for e in events if e['type']=='batch_done')['ok'])

    async def test_ordinary_error_is_not_overwritten_by_done(self):
        async def send(prompt):
            yield {'type':'error','message':'ordinary failure'}
        with patch.object(mod,'Agent',return_value=SimpleNamespace(send=send)):
            events=[e async for e in mod.run_batch(self.store(),[{'prompt':'a'}])]
        done=next(e['event'] for e in events if e['type']=='task_event' and e['event']['type']=='task_done')
        self.assertFalse(done['ok'])
        self.assertFalse(next(e for e in events if e['type']=='batch_done')['ok'])

    async def test_success_keeps_batch_done(self):
        async def send(prompt): yield {'type':'assistant_message','content':'done'}
        with patch.object(mod,'Agent',return_value=SimpleNamespace(send=send)):
            events=[e async for e in mod.run_batch(self.store(),[{'prompt':'a'}])]
        self.assertTrue(next(e for e in events if e['type']=='batch_done')['ok'])
        self.assertTrue(next(e['event'] for e in events if e['type']=='task_event' and e['event']['type']=='task_done')['ok'])

    async def test_battle_close_forwards_cleanup_to_batch(self):
        entered=asyncio.Event(); cancelled=asyncio.Event()
        async def send(prompt):
            entered.set()
            try:
                yield {'type':'assistant_delta','text':'started'}
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        with patch.object(mod,'Agent',return_value=SimpleNamespace(send=send)):
            stream=mod.run_battle(self.store(),'prompt',['one'],demo=False)
            async def consume():
                async for e in stream:
                    if e['type']=='task_event' and e['event']['type']=='assistant_delta':
                        await stream.aclose()
                        return
            await asyncio.wait_for(consume(),1)
        self.assertTrue(entered.is_set()); self.assertTrue(cancelled.is_set())

    async def test_server_disconnect_closes_both_batch_and_battle_sources(self):
        from server import main
        from unittest.mock import AsyncMock
        request=SimpleNamespace(is_disconnected=AsyncMock(return_value=True))
        user=SimpleNamespace(role='admin')
        for endpoint in ('batch','battle'):
            with self.subTest(endpoint=endpoint):
                closed=[]
                async def source(*args,**kwargs):
                    try:
                        yield {'type':'task_created','index':0,'session_id':'s','title':'task'}
                    finally:
                        closed.append(True)
                with patch.object(main,'_db',return_value=self.store()), \
                     patch.object(main.model_registry,'route',return_value=SimpleNamespace(id='test')), \
                     patch.object(main.model_router,'default_model',return_value='test'), \
                     patch.object(main.config,'has_provider_credentials',return_value=True), \
                     patch.object(main,'run_batch' if endpoint=='batch' else 'run_battle',side_effect=source):
                    response=(await main.batch(main.BatchBody(prompts=['x']),request,user=user) if endpoint=='batch'
                              else await main.battle(main.BattleBody(prompt='x',models=['m']),request,user=user))
                    output=[chunk async for chunk in response.body_iterator]
                self.assertEqual(output,[])
                self.assertEqual(closed,[True])

    async def test_closing_batch_generator_cancels_and_joins_children(self):
        entered=asyncio.Event(); cancelled=asyncio.Event()
        async def send(prompt):
            entered.set()
            try:
                yield {'type':'assistant_delta','text':'started'}
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        with patch.object(mod,'Agent',return_value=SimpleNamespace(send=send)):
            stream=mod.run_batch(self.store(),[{'prompt':'a'}])
            async def consume():
                async for e in stream:
                    if e['type']=='task_event' and e['event']['type']=='assistant_delta':
                        await stream.aclose()
                        return
            await asyncio.wait_for(consume(),1)
        self.assertTrue(entered.is_set()); self.assertTrue(cancelled.is_set())
