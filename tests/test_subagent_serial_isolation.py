"""Serial subagent dispatch must pass the requested isolation to the child runner."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from engine import agent as mod, hooks


class SerialSubagentIsolation(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.session=SimpleNamespace(id='parent',owner_id='owner',title='Existing',messages=[],usage={})
        self.store=SimpleNamespace(allow_mcp=False,save=lambda s:None)
        self.agent=mod.Agent.__new__(mod.Agent)
        self.agent.session=self.session; self.agent.store=self.store; self.agent.workspace=self.root
        self.agent.model='test'; self.agent.router=None; self.agent.depth=0; self.agent._bg_child=False
        self.agent._system_message=lambda extra='':{'role':'system','content':'test'}
        self.agent._resolve_sub_model=lambda *args:None

    async def exercise(self,isolation,child_ok=True):
        args={'task':'test task'}
        if isolation is not None: args['isolation']=isolation
        calls=[{'id':'call','type':'function','function':{'name':'spawn_subagent','arguments':json.dumps(args)}}]
        rounds=[0]
        async def model(*_):
            rounds[0]+=1
            if rounds[0]==1: yield {'type':'tool_calls','tool_calls':calls}
            else: yield {'type':'delta','text':'done'}
            yield {'type':'finish','reason':'stop'}
        captured=[]
        async def child(tc,task,model,isolation='shared'):
            captured.append((tc,task,model,isolation))
            yield {'type':'subagent_output','id':tc,'output':'child result','ok':child_ok}
        with patch.object(self.agent,'_llm_with_fallback',side_effect=model), \
             patch.object(self.agent,'_run_subagent',side_effect=child), \
             patch.object(mod.config,'get_auto_compact_chars',return_value=0), \
             patch.object(hooks,'run_event',new_callable=AsyncMock,return_value=[]), \
             patch.object(hooks,'session_start_cache',new_callable=AsyncMock,return_value=''), \
             patch.object(hooks,'pre_tool_use',new_callable=AsyncMock,return_value=None), \
             patch.object(mod,'log_tool'):
            events=[event async for event in self.agent.send('request')]
        self.assertEqual(len(captured),1)
        self.assertEqual(captured[0][3],isolation or 'shared')
        result=next(e for e in events if e.get('type')=='tool_result' and e.get('name')=='spawn_subagent')
        self.assertEqual(result['ok'],child_ok)

    async def test_failure_status_is_not_reported_as_success(self): await self.exercise('worktree',False)
    async def test_requested_worktree_is_forwarded(self): await self.exercise('worktree')
    async def test_shared_is_forwarded(self): await self.exercise('shared')
    async def test_missing_isolation_defaults_to_shared(self): await self.exercise(None)
