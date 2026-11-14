"""Real ownerless CLI local-policy regression; not a server execution gate."""
import asyncio
import json
import os
import shlex
import sys
import tempfile
import unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parent.parent


@unittest.skipUnless(os.name=='posix','fixture hook shell quoting tested on POSIX')
class LocalCliBoundary(unittest.IsolatedAsyncioTestCase):
    async def test_local_cli_denies_workspace_hook_without_server_owner_or_review(self):
        with tempfile.TemporaryDirectory(prefix='forge-local-cli-') as td:
            root=Path(td); env=os.environ.copy()
            for key in ('FORGE_PROFILE','FORGE_RELEASE_MODE','FORGE_PUBLIC_URL','FORGE_COOKIE_SECURE',
                        'FORGE_CUSTOM_API_KEY','GOOGLE_API_KEY','GEMINI_API_KEY','FORGE_PROVIDER','FORGE_MODEL'):
                env.pop(key,None)
            env.update(FORGE_DATA_DIR=str(root/'data'),OPENROUTER_API_KEY='fixture-not-a-real-key',
                       PYTHONPATH=str(ROOT)+os.pathsep+env.get('PYTHONPATH',''))
            # Build the same local SessionStore used by the CLI, in a separate process.
            setup="from engine.session import SessionStore; s=SessionStore().create(); print(s.id)"
            proc=await asyncio.create_subprocess_exec(sys.executable,'-c',setup,cwd=ROOT,env=env,
                    stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
            out,err=await asyncio.wait_for(proc.communicate(),10)
            self.assertEqual(proc.returncode,0,err.decode()); sid=out.decode().strip().splitlines()[-1]
            session=json.loads((root/'data'/'sessions'/f'{sid}.json').read_text())
            self.assertEqual(session.get('owner_id',''),'')
            ws=root/'data'/'workspaces'/sid; ws.mkdir(parents=True,exist_ok=True); (ws/'.forge').mkdir()
            marker=ws/'local-hook-ran'
            script=f"from pathlib import Path; Path({str(marker)!r}).write_text('ran'); raise SystemExit(2)"
            command=f'{shlex.quote(sys.executable)} -c {shlex.quote(script)}'
            (ws/'.forge'/'config.json').write_text(json.dumps({'hooks':{'user_prompt_submit':command}}))
            proc=await asyncio.create_subprocess_exec(sys.executable,str(ROOT/'cli'/'main.py'),'--session',sid,
                    '--once','hello','--json',cwd=ROOT,env=env,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
            try:
                out,err=await asyncio.wait_for(proc.communicate(),15)
            finally:
                if proc.returncode is None:
                    proc.kill(); await proc.wait()
            self.assertEqual(proc.returncode,1,err.decode())
            result=json.loads(out.decode()); self.assertEqual(result['error_code'],'hook_trust_denied')
            self.assertIn('server-admin review',result['error']); self.assertIn('not available yet',result['error']); self.assertFalse(marker.exists())
            # The local guard denies before any model invocation; no provider connection is needed.
            self.assertEqual(result['tools'],[])
            human=await asyncio.create_subprocess_exec(sys.executable,str(ROOT/'cli'/'main.py'),'--session',sid,
                    '--once','hello again',cwd=ROOT,env=env,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
            human_out,human_err=await asyncio.wait_for(human.communicate(),15)
            self.assertEqual(human.returncode,1,human_err.decode())
            self.assertIn('Hook trust denied',human_out.decode())
            self.assertIn('not available yet',human_out.decode())
            self.assertNotIn('Traceback',human_err.decode()); self.assertFalse(marker.exists())
            # Interactive /compact must report denial and return to the prompt,
            # rather than terminating the CLI before /quit can be handled.
            interactive=await asyncio.create_subprocess_exec(sys.executable,str(ROOT/'cli'/'main.py'),'--session',sid,
                    cwd=ROOT,env=env,stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
            try:
                interactive_out,interactive_err=await asyncio.wait_for(interactive.communicate(b'/compact\n/quit\n'),15)
            finally:
                if interactive.returncode is None:
                    interactive.kill(); await interactive.wait()
            self.assertEqual(interactive.returncode,0,interactive_err.decode())
            self.assertIn('Hook trust denied',interactive_out.decode())
            self.assertIn('Bye!',interactive_out.decode())
            self.assertNotIn('Traceback',interactive_err.decode()); self.assertFalse(marker.exists())
            saved=json.loads((root/'data'/'sessions'/f'{sid}.json').read_text())
            self.assertEqual(saved.get('owner_id',''),'')
            # Server account scoping cannot see an ownerless local session by
            # merely knowing its ID; a migration/identity policy is required.
            check=("import sys; from engine.session import SessionStore,ScopedSessionStore; "
                   "print('visible' if ScopedSessionStore(SessionStore(),'account-id').get(sys.argv[1]) else 'hidden')")
            probe=await asyncio.create_subprocess_exec(sys.executable,'-c',check,sid,cwd=ROOT,env=env,
                    stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
            out,err=await asyncio.wait_for(probe.communicate(),10)
            self.assertEqual(probe.returncode,0,err.decode()); self.assertEqual(out.decode().strip(),'hidden')
            # Removing workspace hook config still allows an operator-configured
            # global hook; this is compatibility, not workspace approval.
            (ws/'.forge'/'config.json').unlink()
            global_marker=ws/'global-hook-ran'
            global_script=f"from pathlib import Path; Path({str(global_marker)!r}).write_text('ran'); raise SystemExit(2)"
            global_cmd=f'{shlex.quote(sys.executable)} -c {shlex.quote(global_script)}'
            (root/'data'/'config.json').write_text(json.dumps({'hooks':{'user_prompt_submit':global_cmd}}))
            global_proc=await asyncio.create_subprocess_exec(sys.executable,str(ROOT/'cli'/'main.py'),'--session',sid,
                    '--once','global check','--json',cwd=ROOT,env=env,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
            global_out,global_err=await asyncio.wait_for(global_proc.communicate(),15)
            self.assertEqual(global_proc.returncode,0,global_err.decode())
            self.assertIsNone(json.loads(global_out.decode())['error']); self.assertTrue(global_marker.exists())
            self.assertFalse(marker.exists())
