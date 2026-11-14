"""Subprocess-only fixture for the real Uvicorn SIGTERM integration test."""
import asyncio
import json
import os
import socket
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from engine import background
from server.main import app

root = Path(sys.argv[1])
original_lifespan = app.router.lifespan_context


@asynccontextmanager
async def fixture_lifespan(app):
    async with original_lifespan(app):
        original_pump = background._pump
        async def gated_pump(task):
            await original_pump(task)
            (root / 'pump-waiting').write_text('waiting')
            while not (root / 'release-pump').exists():
                await asyncio.sleep(0.01)
            (root / 'pump-done').write_text('done')
        if os.environ.get('FORGE_TEST_PUMP_GATE') == '1':
            background._pump = gated_pump
            background.SHUTDOWN_PUMP_WAIT = 5.0
        try:
            tid, message = await background.start(
                'integration fixture', root, owner_id='fixture-owner', session_id='fixture-session',
                exec_args=[sys.executable, '-u', str(root / 'worker.py'), str(root)])
        finally:
            background._pump = original_pump
        if not tid:
            raise RuntimeError(message)
        task = background.get(tid)
        yield
    (root / 'shutdown.json').write_text(json.dumps({
        'status': task['status'], 'returncode': task['proc'].returncode,
    }))


app.router.lifespan_context = fixture_lifespan


async def main():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(('127.0.0.1', 0))
    sock.listen(128)
    (root / 'port').write_text(str(sock.getsockname()[1]))
    server = uvicorn.Server(uvicorn.Config(app, log_level='warning', lifespan='on'))
    await server.serve(sockets=[sock])


asyncio.run(main())
