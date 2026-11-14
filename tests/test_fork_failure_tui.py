"""Headless TUI reports fork failure without switching sessions."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from engine import config
from engine.session import ForkError,SessionStore

class ForkFailureTUI(unittest.IsolatedAsyncioTestCase):
    async def test_fork_error_shown_and_current_session_retained(self):
        try:
            from tui import app as tui
            from textual.widgets import Input
        except ImportError:
            self.skipTest('optional textual dependency not installed')
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); (root/'sessions').mkdir(); (root/'workspaces').mkdir()
            with patch.multiple(config,DATA_DIR=root,SESSIONS_DIR=root/'sessions',WORKSPACES_DIR=root/'workspaces',CONFIG_FILE=root/'config.json'):
                store=SessionStore(); session=store.create()
                with patch.object(tui,'store',store),patch.object(store,'fork',side_effect=ForkError('partial-child')):
                    app=tui.ForgeTUI(); lines=[]; original=app.add_line
                    def capture(text,*args,**kwargs):
                        lines.append((str(text),kwargs.get('cls'))); return original(text,*args,**kwargs)
                    with patch.object(app,'add_line',side_effect=capture):
                        async with app.run_test(size=(100,35)) as pilot:
                            await pilot.pause(); self.assertEqual(app.sid,session.id)
                            app.query_one('#prompt',Input).value='/fork'; await pilot.press('enter'); await pilot.pause()
                            self.assertEqual(app.sid,session.id)
                            self.assertTrue(any('partial-child' in text and cls=='tool-bad' for text,cls in lines),lines)
                            self.assertFalse(any('Đã fork' in text for text,_ in lines),lines)
