"""Scheduler must stop after an injected hook-trust denial, not retry silently."""
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from engine import hooks, scheduler


class TrustScheduler(unittest.IsolatedAsyncioTestCase):
    async def test_denial_stops_unlimited_loop_and_exposes_reason(self):
        manager=scheduler.LoopManager(); runs=[]
        async def run(sid,text,mode):
            runs.append((sid,text,mode))
            raise hooks.HookTrustDenied()
        manager.start('s',0,'work','agent',run)
        task=manager._loops['s']['task']
        await asyncio.wait_for(task,1)
        status=manager.status('s')
        self.assertEqual(len(runs),1)
        self.assertFalse(status['running'])
        self.assertTrue(status['finished'])
        self.assertFalse(status['last_ok'])
        self.assertEqual(status['last_error_code'],'hook_trust_denied')
        self.assertIn('Earlier operations may have run',status['last_error'])

    async def test_generic_error_still_retries_to_limit(self):
        manager=scheduler.LoopManager(); attempts=[]
        async def run(sid,text,mode):
            attempts.append(1)
            if len(attempts)==1: raise RuntimeError('private error')
        manager.start('s',0,'work','agent',run,max_runs=2)
        await asyncio.wait_for(manager._loops['s']['task'],1)
        status=manager.status('s')
        self.assertEqual(len(attempts),2)
        self.assertTrue(status['last_ok'])
        self.assertNotIn('last_error_code',status)
        self.assertNotIn('private error',str(status))

    async def test_real_server_loop_turn_propagates_injected_agent_denial(self):
        from server import main
        manager=scheduler.LoopManager(); invocations=[]
        async def send(text,chat_mode):
            invocations.append((text,chat_mode))
            if False: yield {}
            raise hooks.HookTrustDenied()
        store=SimpleNamespace(get=lambda sid:SimpleNamespace(model='test'))
        agent=SimpleNamespace(send=send)
        with patch.object(main,'ScopedSessionStore',return_value=store), \
             patch.object(main,'Agent',return_value=agent), \
             patch.object(main.config,'has_provider_credentials',return_value=True):
            manager.start('s',0,'work','agent',main._loop_runner('owner',False))
            await asyncio.wait_for(manager._loops['s']['task'],1)
        status=manager.status('s')
        self.assertEqual(invocations,[('work','agent')])
        self.assertEqual(status['last_error_code'],'hook_trust_denied')
        self.assertFalse(status['running'])

    async def test_chat_loop_status_reports_denial_instead_of_no_loop(self):
        from server import main
        from unittest.mock import AsyncMock
        manager=scheduler.LoopManager()
        async def deny(*args): raise hooks.HookTrustDenied()
        manager.start('s',0,'work','agent',deny)
        await asyncio.wait_for(manager._loops['s']['task'],1)
        session=SimpleNamespace(model='test')
        db=SimpleNamespace(get=lambda sid:session,workspace=lambda sid:__import__('pathlib').Path('/tmp'))
        request=SimpleNamespace(is_disconnected=AsyncMock(return_value=False))
        with patch.object(scheduler,'manager',manager), \
             patch.object(main,'_db',return_value=db), \
             patch.object(main.model_registry,'route',return_value=SimpleNamespace(id='test')), \
             patch.object(main.config,'has_provider_credentials',return_value=True):
            response=await main.chat('s',main.ChatBody(text='/loop'),request,user=SimpleNamespace(role='admin'))
            output=''.join([chunk async for chunk in response.body_iterator])
        self.assertIn('hook_trust_denied',output)
        self.assertIn('Earlier operations may have run',output)
        self.assertNotIn('Không có loop nào',output)

    async def test_new_start_clears_old_denial(self):
        manager=scheduler.LoopManager()
        async def deny(*_args): raise hooks.HookTrustDenied()
        async def allow(*_args): return None
        manager.start('s',0,'work','agent',deny)
        await asyncio.wait_for(manager._loops['s']['task'],1)
        self.assertEqual(manager.status('s')['last_error_code'],'hook_trust_denied')
        manager.start('s',0,'new','agent',allow,max_runs=1)
        await asyncio.wait_for(manager._loops['s']['task'],1)
        status=manager.status('s')
        self.assertTrue(status['last_ok'])
        self.assertNotIn('last_error_code',status)
