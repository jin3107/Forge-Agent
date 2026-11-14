"""Textual headless UI test of local hook denial; no real terminal/provider."""
import json
import os
import shlex
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from engine import config, hooks
from engine.session import SessionStore


@unittest.skipUnless(os.name=='posix','marker shell quoting tested on POSIX')
class LocalTuiUI(unittest.IsolatedAsyncioTestCase):
    async def test_real_tui_input_reports_denial_and_no_hook_runs(self):
        try:
            from tui import app as tui
            from textual.widgets import Input
        except ImportError:
            self.skipTest('optional textual dependency not installed')
        with tempfile.TemporaryDirectory(prefix='forge-tui-hook-') as td:
            root=Path(td); sessions=root/'sessions'; workspaces=root/'workspaces'
            sessions.mkdir(); workspaces.mkdir()
            with patch.multiple(config,DATA_DIR=root,SESSIONS_DIR=sessions,WORKSPACES_DIR=workspaces,CONFIG_FILE=root/'config.json'), \
                 patch.object(hooks,'_LOCAL_HOOK_POLICY',True), patch.object(config,'has_llm_credentials',return_value=True), \
                 patch('engine.agent.Agent._stream_chat',side_effect=AssertionError('provider must not run')):
                store=SessionStore(); session=store.create(); ws=store.workspace(session.id)
                (ws/'.forge').mkdir(); marker=ws/'ran'
                script=f"from pathlib import Path; Path({str(marker)!r}).write_text('ran')"
                cmd=f'{shlex.quote(sys.executable)} -c {shlex.quote(script)}'
                (ws/'.forge'/'config.json').write_text(json.dumps({'hooks':{'user_prompt_submit':cmd}}))
                with patch.object(tui,'store',store):
                    app=tui.ForgeTUI(); lines=[]
                    original=app.add_line
                    def capture(text,*args,**kwargs):
                        lines.append((str(text),kwargs.get('cls')))
                        return original(text,*args,**kwargs)
                    with patch.object(app,'add_line',side_effect=capture):
                        async with app.run_test(size=(100,35)) as pilot:
                            await pilot.pause()
                            self.assertEqual(app.sid,session.id)
                            app.query_one('#prompt',Input).value='hello'
                            await pilot.press('enter')
                            await app.workers.wait_for_complete()
                            await pilot.pause()
                            self.assertFalse(app.busy)
                            self.assertFalse(marker.exists())
                            self.assertTrue(any('server-admin review' in text and cls=='tool-bad' for text,cls in lines),lines)
                            self.assertFalse(any('✔' in text for text,_ in lines),lines)
                            # Compact must show the same denial instead of escaping
                            # into Textual's event loop and closing the interface.
                            lines.clear()
                            app.query_one('#prompt',Input).value='/compact'
                            await pilot.press('enter')
                            await pilot.pause()
                            self.assertTrue(any('server-admin review' in text and cls=='tool-bad' for text,cls in lines),lines)
                            self.assertFalse(marker.exists())
                self.assertEqual(store.get(session.id).messages[-1]['content'],'hello')
