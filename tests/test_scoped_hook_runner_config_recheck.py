"""Injected checked adapter must reread global hook config on recheck."""
import copy
import json
import os
import shlex
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from engine import hooks
from engine.hook_approvals import Decision, Snapshot
from server.hook_runner import ScopedCheckedHookRunner

class ConfigRecheck(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup); self.ws=Path(self.tmp.name).resolve()
        self.scope=(Decision('absent'),Snapshot('absent',None,None)); self.config={}; self.loads=0
    def load(self):
        self.loads+=1; return copy.deepcopy(self.config)
    def runner(self): return ScopedCheckedHookRunner(owner_id='owner',resolve=lambda *_:self.scope,load_config=self.load)
    async def invoke(self,runner,kind):
        if kind=='event': return await runner.run_event(self.ws,'sid','stop')
        if kind=='start': return await runner.session_start_cache(self.ws,'sid')
        if kind=='pre': return await runner.pre_tool_use(self.ws,'sid','write_file',{})
        return await runner.post_tool_use(self.ws,'sid','write_file',{},'tool already ran')
    async def test_changed_global_hook_during_command_denies_after_effect(self):
        for kind,event in [('event','stop'),('start','session_start'),('pre','pre_tool_use'),('post','post_tool_use')]:
            with self.subTest(kind=kind):
                self.config={'hooks':{event:'first'}}; self.loads=0; calls=[]
                async def command(cmd,*args,**kwargs):
                    calls.append(cmd); self.config={'hooks':{event:'changed'}}
                    return {'code':0,'out':'done','err':''}
                with patch.object(hooks,'_run_cmd',side_effect=command):
                    with self.assertRaises(hooks.HookTrustDenied): await self.invoke(self.runner(),kind)
                self.assertEqual(calls,['first']); self.assertGreaterEqual(self.loads,2)
    @unittest.skipUnless(os.name=='posix','POSIX shell quoting')
    async def test_real_hook_changes_global_config_and_effect_remains(self):
        cfg=self.ws/'global.json'; marker=self.ws/'marker'
        replacement=json.dumps({'hooks':{'stop':'echo changed'}})
        script=(f'from pathlib import Path; Path({str(marker)!r}).write_text("ran"); '
                f'Path({str(cfg)!r}).write_text({replacement!r})')
        command=f'{shlex.quote(sys.executable)} -c {shlex.quote(script)}'
        cfg.write_text(json.dumps({'hooks':{'stop':command}}))
        runner=ScopedCheckedHookRunner(owner_id='owner',resolve=lambda *_:self.scope,
                                       load_config=lambda:json.loads(cfg.read_text()))
        with self.assertRaises(hooks.HookTrustDenied):
            await runner.run_event(self.ws,'sid','stop')
        self.assertEqual(marker.read_text(),'ran')
        self.assertEqual(json.loads(cfg.read_text())['hooks']['stop'],'echo changed')

    @unittest.skipUnless(os.name=='posix','POSIX shell quoting')
    async def test_real_legacy_hook_global_config_change_detected(self):
        cfg=self.ws/'global.json'; marker=self.ws/'legacy-marker'
        replacement=json.dumps({'on_file_change':'echo changed'})
        script=(f'from pathlib import Path; Path({str(marker)!r}).write_text("ran"); '
                f'Path({str(cfg)!r}).write_text({replacement!r})')
        command=f'{shlex.quote(sys.executable)} -c {shlex.quote(script)}'
        cfg.write_text(json.dumps({'on_file_change':command}))
        runner=ScopedCheckedHookRunner(owner_id='owner',resolve=lambda *_:self.scope,
                                       load_config=lambda:json.loads(cfg.read_text()))
        with self.assertRaises(hooks.HookTrustDenied):
            await runner.global_file_change(self.ws,'sid','file')
        self.assertEqual(marker.read_text(),'ran')
        self.assertEqual(json.loads(cfg.read_text())['on_file_change'],'echo changed')

    async def test_loader_failure_on_recheck_fails_closed(self):
        self.config={'hooks':{'stop':'first'}}; count=0
        def fail_second():
            nonlocal count
            count+=1
            if count>1: raise OSError('private config error')
            return copy.deepcopy(self.config)
        runner=ScopedCheckedHookRunner(owner_id='owner',resolve=lambda *_:self.scope,load_config=fail_second)
        async def command(*args,**kwargs): return {'code':0,'out':'effect','err':''}
        with patch.object(hooks,'_run_cmd',side_effect=command):
            with self.assertRaises(hooks.HookTrustDenied) as caught: await runner.run_event(self.ws,'sid','stop')
        self.assertNotIn('private config error',str(caught.exception))
    async def test_irrelevant_global_change_does_not_block_event(self):
        self.config={'hooks':{'stop':'first'},'unrelated':1}; calls=[]
        async def command(cmd,*args,**kwargs):
            calls.append(cmd); self.config['unrelated']=2
            return {'code':0,'out':'done','err':''}
        with patch.object(hooks,'_run_cmd',side_effect=command):
            result=await self.runner().run_event(self.ws,'sid','stop')
        self.assertEqual(calls,['first']); self.assertEqual(result[0]['out'],'done'); self.assertGreaterEqual(self.loads,2)
