"""Real Uvicorn SIGTERM + production ASGI lifespan + local child processes."""
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
HARNESS = ROOT / 'tests' / 'fixtures' / 'uvicorn_shutdown_harness.py'


@unittest.skipUnless(os.name == 'posix', 'requires POSIX signals and process groups')
class UvicornShutdown(unittest.IsolatedAsyncioTestCase):
    async def test_sigterm_runs_server_lifespan_and_stops_background_group(self):
        with tempfile.TemporaryDirectory(prefix='forge-uvicorn-stop-') as td:
            root = Path(td)
            (root / 'child.py').write_text(
                "import pathlib,sys,time\n"
                "root=pathlib.Path(sys.argv[1])\n"
                "(root/'child-ready').write_text('ready')\n"
                "while not (root/'go').exists(): time.sleep(0.02)\n"
                "(root/'survived').write_text('alive')\n"
            )
            (root / 'worker.py').write_text(
                "import os,pathlib,subprocess,sys,time\n"
                "root=pathlib.Path(sys.argv[1])\n"
                "child=subprocess.Popen([sys.executable,'-u',str(root/'child.py'),str(root)])\n"
                "(root/'pids').write_text(str(os.getpid())+' '+str(child.pid))\n"
                "time.sleep(60)\n"
            )
            env = os.environ.copy()
            env['FORGE_DATA_DIR'] = str(root / 'data')
            env['FORGE_TEST_PUMP_GATE'] = '1'
            env.pop('FORGE_PROFILE', None)
            env['PYTHONPATH'] = str(ROOT) + os.pathsep + env.get('PYTHONPATH', '')
            with (root / 'server.log').open('w+') as log:
                server = await asyncio.create_subprocess_exec(
                    sys.executable, '-u', str(HARNESS), str(root), cwd=str(ROOT), env=env,
                    stdout=log, stderr=log)
                pids = []
                try:
                    async def ready():
                        async with httpx.AsyncClient(trust_env=False, timeout=0.3) as client:
                            while True:
                                if server.returncode is not None:
                                    log.flush()
                                    raise AssertionError(f'server exited early: {(root / "server.log").read_text()}')
                                if (root / 'port').exists() and (root / 'pids').exists() and (root / 'child-ready').exists():
                                    try:
                                        response = await client.get(f'http://127.0.0.1:{(root / "port").read_text()}/api/health')
                                        if response.status_code == 200 and response.json().get('ok'):
                                            return
                                    except (httpx.HTTPError, ValueError):
                                        pass
                                await asyncio.sleep(0.03)
                    await asyncio.wait_for(ready(), 12)
                    pids = [int(x) for x in (root / 'pids').read_text().split()]
                    self.assertEqual(len(pids), 2)
                    os.kill(pids[0], 0)
                    os.kill(pids[1], 0)
                    self.assertEqual(os.getpgid(pids[0]), pids[0])
                    self.assertEqual(os.getpgid(pids[1]), pids[0])
                    server.send_signal(signal.SIGTERM)
                    async def pump_waiting():
                        while not (root / 'pump-waiting').exists():
                            if server.returncode is not None:
                                self.fail('server exited before output pump completed')
                            await asyncio.sleep(0.01)
                    await asyncio.wait_for(pump_waiting(), 5)
                    self.assertIsNone(server.returncode, 'shutdown must wait for the pending pump')
                    self.assertFalse((root / 'shutdown.json').exists())
                    (root / 'release-pump').touch()
                    await asyncio.wait_for(server.wait(), 10)
                    self.assertTrue((root / 'pump-done').exists(), 'pump must complete before lifespan exits')
                    self.assertTrue((root / 'shutdown.json').exists(), (root / 'server.log').read_text())
                    shutdown = json.loads((root / 'shutdown.json').read_text())
                    self.assertEqual(shutdown['status'], 'killed')
                    self.assertIsNotNone(shutdown['returncode'])
                    (root / 'go').write_text('go')
                    await asyncio.sleep(0.5)
                    self.assertFalse((root / 'survived').exists(), 'descendant survived SIGTERM cleanup')
                    # Positive control: the same child program must create the
                    # marker when it is alive and the gate is already open.
                    control = await asyncio.create_subprocess_exec(
                        sys.executable, str(root / 'child.py'), str(root))
                    self.assertEqual(await asyncio.wait_for(control.wait(), 3), 0)
                    self.assertTrue((root / 'survived').exists())
                finally:
                    if server.returncode is None:
                        server.kill()
                        await asyncio.wait_for(server.wait(), 5)
                    if not pids and (root / 'pids').exists():
                        pids = [int(x) for x in (root / 'pids').read_text().split()]
                    if pids:
                        try:
                            os.killpg(pids[0], signal.SIGKILL)
                        except ProcessLookupError:
                            pass


if __name__ == '__main__':
    unittest.main()
