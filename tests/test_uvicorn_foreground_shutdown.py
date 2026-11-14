"""Real Uvicorn SIGTERM while cancelled foreground spawn is being cleaned."""
import asyncio
import json
import os
import signal
import sys
import tempfile
import unittest
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
HARNESS = ROOT / 'tests' / 'fixtures' / 'uvicorn_foreground_shutdown_harness.py'


@unittest.skipUnless(os.name == 'posix', 'requires POSIX signals and process groups')
class UvicornForegroundShutdown(unittest.IsolatedAsyncioTestCase):
    async def test_sigterm_waits_for_tracked_late_foreground_spawn(self):
        with tempfile.TemporaryDirectory(prefix='forge-uvicorn-fg-stop-') as td:
            root = Path(td)
            env = os.environ.copy()
            env['FORGE_DATA_DIR'] = str(root / 'data')
            env.pop('FORGE_PROFILE', None)
            env['PYTHONPATH'] = str(ROOT) + os.pathsep + env.get('PYTHONPATH', '')
            with (root / 'server.log').open('w+') as log:
                server = await asyncio.create_subprocess_exec(
                    sys.executable, '-u', str(HARNESS), str(root), cwd=str(ROOT), env=env,
                    stdout=log, stderr=log)
                pid = None
                try:
                    async def ready():
                        async with httpx.AsyncClient(trust_env=False, timeout=0.3) as client:
                            while True:
                                if server.returncode is not None:
                                    raise AssertionError(f'server exited early: {(root / "server.log").read_text()}')
                                if (root / 'port').exists() and (root / 'pid').exists():
                                    try:
                                        response = await client.get(f'http://127.0.0.1:{(root / "port").read_text()}/api/health')
                                        if response.status_code == 200:
                                            return
                                    except httpx.HTTPError:
                                        pass
                                await asyncio.sleep(0.03)
                    await asyncio.wait_for(ready(), 12)
                    pid = int((root / 'pid').read_text())
                    os.kill(pid, 0)
                    server.send_signal(signal.SIGTERM)
                    await asyncio.sleep(0.4)
                    self.assertIsNone(server.returncode, 'server stopped without waiting for foreground cleanup')
                    self.assertFalse((root / 'shutdown.json').exists())
                    (root / 'release').write_text('release')
                    await asyncio.wait_for(server.wait(), 6)
                    self.assertTrue((root / 'shutdown.json').exists(), (root / 'server.log').read_text())
                    self.assertIsNotNone(json.loads((root / 'shutdown.json').read_text())['returncode'])
                finally:
                    if server.returncode is None:
                        server.kill()
                        await asyncio.wait_for(server.wait(), 5)
                    if pid is None and (root / 'pid').exists():
                        pid = int((root / 'pid').read_text())
                    if pid is not None:
                        try:
                            os.killpg(pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass


if __name__ == '__main__':
    unittest.main()
