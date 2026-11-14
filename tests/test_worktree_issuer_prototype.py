"""Real Git integration for a test-only, not-product-wired worktree issuer."""
import asyncio
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from engine import checkpoints, config, tools, worktree, worktree_base
from engine.session import SessionStore, ScopedSessionStore
from engine.worktree_binding import validate
from engine.worktree_binding_registry import BindingRegistry

@unittest.skipUnless(shutil.which('git'),'Git required')
class IssuerPrototype(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        root=Path(self.tmp.name); self.wss=root/'workspaces'; self.wss.mkdir(); sessions=root/'sessions'; sessions.mkdir()
        p=patch.multiple(config,DATA_DIR=root,WORKSPACES_DIR=self.wss,SESSIONS_DIR=sessions,CONFIG_FILE=root/'config.json'); p.start(); self.addCleanup(p.stop)
        self.base=SessionStore(); self.store=ScopedSessionStore(self.base,'owner'); self.other=ScopedSessionStore(self.base,'other')
        self.parent=self.store.create(model='test'); self.ws=self.store.workspace(self.parent.id); (self.ws/'file').write_text('before\n')
        self.db=root/'binding.sqlite3'; self.registry=BindingRegistry(self.db)

    async def issue(self,store=None,parent=None,registry=None):
        from engine.worktree_issuer import issue
        return await issue(store or self.store,parent or self.parent.id,registry or self.registry,model='test')

    async def test_issue_uses_post_write_state_and_registers_actual_parent(self):
        with patch('engine.lsp.available_for',return_value=False):
            write=await tools.execute_tool('write_file',{'path':'file','content':'after\n'},self.ws,owner_id='owner',session_id=self.parent.id)
        self.assertTrue(write['ok'],write)
        result=await self.issue(); self.assertTrue(result['ok'],result)
        child=result['child_sid']; status,binding=BindingRegistry(self.db).lookup('owner',self.parent.id,child)
        self.assertEqual(status,'found'); self.assertTrue(await validate(self.store,binding))
        wt=self.store.workspace(child); self.assertEqual((wt/'file').read_text(),'after\n')
        (wt/'file').write_text('child\n'); self.assertEqual((self.ws/'file').read_text(),'after\n')
        self.assertTrue((await checkpoints.undo(checkpoints.key_for(self.ws),self.ws))['ok'])
        self.assertEqual((self.ws/'file').read_text(),'before\n')

    async def test_direct_experimental_lifecycle_diff_merge_undo_discard(self):
        result=await self.issue(); self.assertTrue(result['ok'],result)
        child=result['child_sid']; status,binding=self.registry.lookup('owner',self.parent.id,child)
        self.assertEqual(status,'found'); self.assertTrue(await validate(self.store,binding))
        wt=self.store.workspace(child); (wt/'file').write_text('merged\n')
        self.assertIn('file',await worktree.diff_stat(child,binding.repo_key,wt))
        self.assertTrue(await validate(self.store,binding))
        merge=await worktree.merge(child,binding.repo_key,self.parent.id,self.ws)
        self.assertTrue(merge['ok'],merge); self.assertEqual((self.ws/'file').read_text(),'merged\n')
        self.assertTrue((await checkpoints.undo(checkpoints.key_for(self.ws),self.ws))['ok'])
        self.assertEqual((self.ws/'file').read_text(),'before\n')
        self.assertTrue(await validate(self.store,binding))
        self.assertTrue((await worktree.discard(child,binding.repo_key))['ok'])
        self.assertFalse(await validate(self.store,binding))
        self.assertEqual(self.registry.lookup('owner',self.parent.id,child)[0],'found')
        self.assertTrue(worktree._repo(binding.repo_key).exists())

    async def test_cancellation_propagates_and_does_not_publish_record(self):
        started=asyncio.Event()
        async def capture(*args):
            started.set(); await asyncio.Event().wait()
        with patch.object(worktree_base,'capture',side_effect=capture):
            task=asyncio.create_task(self.issue()); await asyncio.wait_for(started.wait(),5)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError): await task
        rows=self.store.list(); self.assertEqual(len(rows),2)
        child=next(row['id'] for row in rows if row['id']!=self.parent.id)
        self.assertEqual(self.registry.lookup('owner',self.parent.id,child),('missing',None))

    async def test_change_after_publication_fails_but_record_remains(self):
        original=self.registry.record
        def record(binding):
            status=original(binding)
            (self.wss/f'{binding.child_sid}.path').unlink()
            return status
        with patch.object(self.registry,'record',side_effect=record): result=await self.issue()
        self.assertFalse(result['ok'],result)
        status,binding=self.registry.lookup('owner',self.parent.id,result['child_sid'])
        self.assertEqual(status,'found'); self.assertFalse(await validate(self.store,binding))

    async def test_wrong_owner_or_unscoped_cannot_create_child(self):
        before=len(self.base.list())
        for store in (self.other,self.base):
            result=await self.issue(store=store); self.assertFalse(result['ok'],result)
        self.assertEqual(len(self.base.list()),before)

    async def test_parent_removed_during_capture_leaves_no_issued_binding(self):
        original=worktree_base.capture
        async def capture(*args,**kwargs):
            result=await original(*args,**kwargs); self.store.delete(self.parent.id); return result
        with patch.object(worktree_base,'capture',side_effect=capture): result=await self.issue()
        self.assertFalse(result['ok'],result); self.assertIn('child_sid',result)
        self.assertEqual(self.registry.lookup('owner',self.parent.id,result['child_sid']),('missing',None))

    async def test_parent_workspace_changed_during_capture_fails_closed(self):
        alternate=self.wss/'alternate'; alternate.mkdir(); original=worktree_base.capture
        async def capture(*args,**kwargs):
            result=await original(*args,**kwargs)
            (self.wss/f'{self.parent.id}.path').write_text(str(alternate)); return result
        with patch.object(worktree_base,'capture',side_effect=capture): result=await self.issue()
        self.assertFalse(result['ok'],result)
        self.assertEqual(self.registry.lookup('owner',self.parent.id,result['child_sid']),('missing',None))

    async def test_capture_and_pointer_failure_do_not_issue_record(self):
        with patch.object(worktree_base,'capture',new_callable=AsyncMock,return_value={'ok':False,'message':'secret'}):
            result=await self.issue()
        self.assertFalse(result['ok']); self.assertNotIn('secret',str(result))
        self.assertEqual(self.registry.lookup('owner',self.parent.id,result['child_sid']),('missing',None))
        original=Path.open
        def fail(path,*args,**kwargs):
            if path.suffix=='.path' and args and args[0]=='x': raise OSError('private')
            return original(path,*args,**kwargs)
        with patch.object(Path,'open',fail): result=await self.issue()
        self.assertFalse(result['ok']); self.assertNotIn('private',str(result))
        self.assertEqual(self.registry.lookup('owner',self.parent.id,result['child_sid']),('missing',None))

    async def test_registry_failure_cannot_report_success_and_retains_artifacts(self):
        with patch.object(self.registry,'record',return_value='error'):
            result=await self.issue()
        self.assertFalse(result['ok'],result); child=result['child_sid']
        self.assertIsNotNone(self.store.get(child)); self.assertTrue(worktree.wt_path(child).exists())
        self.assertEqual(self.registry.lookup('owner',self.parent.id,child),('missing',None))

    async def test_existing_worktree_target_is_not_removed(self):
        original=self.store.create
        def create(**kwargs):
            child=original(**kwargs); target=worktree.wt_path(child.id); target.mkdir(); (target/'keep').write_text('untouched'); return child
        with patch.object(self.store,'create',side_effect=create): result=await self.issue()
        self.assertFalse(result['ok'],result); child=result['child_sid']
        self.assertEqual((worktree.wt_path(child)/'keep').read_text(),'untouched')
        self.assertEqual(self.registry.lookup('owner',self.parent.id,child),('missing',None))
