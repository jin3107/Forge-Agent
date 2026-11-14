"""Uvicorn fixture injecting a canceled delayed checkpoint subprocess spawn."""
import asyncio
import json
import socket
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import patch

import uvicorn
from engine import checkpoints
from server.main import app

root = Path(sys.argv[1])
original_lifespan = app.router.lifespan_context
original_spawn = asyncio.create_subprocess_exec


@asynccontextmanager
async def fixture_lifespan(app):
    created, release = asyncio.Event(), asyncio.Event()
    processes = []

    async def delayed(*args, **kwargs):
        proc = await original_spawn(sys.executable, '-u', '-c', 'import time; time.sleep(30)', **kwargs)
        processes.append(proc)
        (root / 'pid').write_text(str(proc.pid))
        created.set()
        await release.wait()
        return proc

    async def await_gate():
        while not (root / 'release').exists():
            await asyncio.sleep(.02)
        release.set()

    async with original_lifespan(app):
        with patch.object(checkpoints, 'GIT', sys.executable), \
             patch('engine.checkpoints.asyncio.create_subprocess_exec', side_effect=delayed):
            call = asyncio.create_task(checkpoints._run([]))
            await asyncio.wait_for(created.wait(), 3)
            call.cancel()
            try:
                await call
            except asyncio.CancelledError:
                pass
            asyncio.create_task(await_gate())
            yield
    (root / 'shutdown.json').write_text(json.dumps({'returncode': processes[0].returncode}))


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
