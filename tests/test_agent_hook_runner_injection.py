"""Per-agent hook runners are isolated; injection is not itself enforcement."""
import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from engine import agent as mod, config, hooks
from engine.session import SessionStore, ScopedSessionStore

class Runner:
    def __init__(self,deny=False): self.events=[]; self.deny=deny
    async def run_event(self,ws,sid,event,extra=None):
        self.events.append(('event',sid,event))
        if self.deny: raise hooks.HookTrustDenied()
        return []
    async def session_start_cache(self,ws,sid): self.events.append(('start',sid)); return ''
    async def pre_tool_use(self,ws,sid,name,args): self.events.append(('pre',sid,name)); return None
    async def post_tool_use(self,ws,sid,name,args,output,env_extra=None): self.events.append(('post',sid,name)); return ''
    async def global_file_change(self,ws,sid,path): self.events.append(('legacy',sid,path)); return ''

class AgentRunnerInjection(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        root=Path(self.tmp.name); (root/'sessions').mkdir(); (root/'workspaces').mkdir()
        p=patch.multiple(config,DATA_DIR=root,SESSIONS_DIR=root/'sessions',WORKSPACES_DIR=root/'workspaces',CONFIG_FILE=root/'config.json'); p.start(); self.addCleanup(p.stop)
        self.store=ScopedSessionStore(SessionStore(),'owner'); self.session=self.store.create(model='test')
        mod._BG_AGENTS.clear(); self.addCleanup(mod._BG_AGENTS.clear)
    async def test_incomplete_runner_rejected_before_any_hooks(self):
        with self.assertRaises(ValueError):
            mod.Agent(self.store,self.session.id,hook_runner=object())

    async def test_injected_runner_failure_is_denial_not_silently_ignored(self):
        runner=Runner(); runner.run_event=AsyncMock(side_effect=OSError('private failure'))
        agent=mod.Agent(self.store,self.session.id,hook_runner=runner)
        with patch.object(agent,'_stream_chat',side_effect=AssertionError('provider must not run')):
            with self.assertRaises(hooks.HookTrustDenied) as caught:
                _=[e async for e in agent.send('hello')]
        self.assertNotIn('private failure',str(caught.exception))

    async def test_all_runner_methods_fail_closed_for_errors_and_bad_results(self):
        for method,args,bad in [('run_event',(self.tmp.name,'sid','stop',{}),[{}]),
                                ('session_start_cache',(self.tmp.name,'sid'),None),
                                ('pre_tool_use',(self.tmp.name,'sid','x',{}),{}),
                                ('post_tool_use',(self.tmp.name,'sid','x',{},'out'),None)]:
            for value in (bad,OSError('private')):
                with self.subTest(method=method,value=type(value).__name__):
                    runner=Runner(); mock=AsyncMock(side_effect=value) if isinstance(value,Exception) else AsyncMock(return_value=value)
                    setattr(runner,method,mock)
                    agent=mod.Agent(self.store,self.session.id,hook_runner=runner)
                    with self.assertRaises(hooks.HookTrustDenied) as caught:
                        await getattr(agent._hook_runner(),method)(*args)
                    self.assertNotIn('private',str(caught.exception))

    async def test_injected_runner_cancellation_propagates(self):
        runner=Runner(); runner.run_event=AsyncMock(side_effect=asyncio.CancelledError())
        agent=mod.Agent(self.store,self.session.id,hook_runner=runner)
        with self.assertRaises(asyncio.CancelledError):
            _=[e async for e in agent.send('hello')]

    async def test_distinct_concurrent_agents_do_not_share_hook_runners(self):
        one=Runner(); two=Runner(deny=True); s2=self.store.create(model='test')
        a=mod.Agent(self.store,self.session.id,hook_runner=one); b=mod.Agent(self.store,s2.id,hook_runner=two)
        async def stream(*args,**kwargs):
            yield {'type':'delta','text':'done'}
            yield {'type':'finish','reason':'stop'}
        async def send(agent): return [e async for e in agent.send('hello')]
        with patch.object(mod.Agent,'_stream_chat',side_effect=stream):
            results=await asyncio.gather(send(a),send(b),return_exceptions=True)
        self.assertIsInstance(results[1],hooks.HookTrustDenied)
        self.assertTrue(any(e.get('type')=='turn_end' for e in results[0]))
        self.assertTrue(any(e[0]=='start' for e in one.events)); self.assertTrue(all(e[1]==self.session.id for e in one.events))
        self.assertTrue(all(e[1]==s2.id for e in two.events))
    async def test_pre_and_post_dispatch_use_injected_runner(self):
        runner=Runner(); agent=mod.Agent(self.store,self.session.id,hook_runner=runner)
        async def stream(messages,**kwargs):
            if messages[-1]['role']=='user':
                yield {'type':'tool_calls','tool_calls':[{'id':'tc','type':'function','function':{'name':'list_dir','arguments':'{}'}}]}
            else: yield {'type':'delta','text':'done'}
            yield {'type':'finish','reason':'stop'}
        with patch.object(agent,'_stream_chat',side_effect=stream), patch.object(mod.tools,'execute_tool',new_callable=AsyncMock,return_value={'ok':True,'output':'ok'}):
            events=[e async for e in agent.send('hello')]
        self.assertTrue(any(e.get('type')=='turn_end' for e in events),events)
        self.assertIn(('pre',self.session.id,'list_dir'),runner.events)
        self.assertIn(('post',self.session.id,'list_dir'),runner.events)
        self.assertIn(('event',self.session.id,'stop'),runner.events)
    async def test_compaction_uses_injected_runner_and_denies_before_provider(self):
        runner=Runner(deny=True); agent=mod.Agent(self.store,self.session.id,hook_runner=runner)
        agent.session.messages=[{'role':'user','content':'text'}]
        with patch.object(agent,'_stream_chat',side_effect=AssertionError('provider must not run')):
            with self.assertRaises(hooks.HookTrustDenied): await mod.compact_session(agent)
        self.assertIn(('event',self.session.id,'pre_compact'),runner.events)
    async def test_child_and_background_receive_same_runner(self):
        runner=Runner(); parent=mod.Agent(self.store,self.session.id,hook_runner=runner); seen=[]; done=asyncio.Event()
        class Child:
            def __init__(self,store,sid,**kwargs): self.workspace=store.workspace(sid); seen.append(kwargs.get('hook_runner'))
            async def send(self,task,**kwargs): yield {'type':'assistant_message','content':'done'}
        original=parent._bg_agent_run
        async def bg(*args):
            try: await original(*args)
            finally: done.set()
        with patch.object(mod,'Agent',Child), patch.object(parent,'_bg_agent_run',side_effect=bg), \
             patch('engine.bus.send'),patch.object(parent,'_notify',new_callable=AsyncMock),patch.object(mod,'log_tool'):
            _=[e async for e in parent._run_subagent('id','task',None,'shared')]
            result=await parent.spawn_background('task',isolation='shared'); self.assertTrue(result['ok'])
            await asyncio.wait_for(done.wait(),2)
        self.assertEqual(seen,[runner,runner])
