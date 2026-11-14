"""Loopback Uvicorn: fake LLM and injected checkpoint Git failure."""
import asyncio
import json
import socket
import sys
from pathlib import Path

import uvicorn
from engine.agent import Agent
from engine import checkpoints
from server.main import app, config
from server.auth import auth_store

root = Path(sys.argv[1])
PASSWORD = 'fixture-password-at-least-16-characters'
for email in ('alice@example.test', 'bob@example.test'):
    auth_store.create_user(email, PASSWORD, role='admin')

# This is a test-only fake provider. No model API/network calls are made.
config.has_provider_credentials = lambda provider: True


async def fake_stream(self, messages, tools=None, model=None, temperature=0.3):
    last = messages[-1]
    if last['role'] == 'user':
        request = json.loads(last['content'])
        name = request['name']
        if name not in ('run_command', 'write_file'):
            raise ValueError('unsupported fixture tool')
        yield {'type': 'tool_calls', 'tool_calls': [{'id': 'fixture-call', 'type': 'function',
               'function': {'name': name, 'arguments': json.dumps(request.get('args', {}))}}]}
    else:
        yield {'type': 'delta', 'text': 'fixture response'}
    yield {'type': 'finish', 'reason': 'stop'}


Agent._stream_chat = fake_stream

# Exercise the real snapshot and dispatch paths while simulating Git timeout.
async def failed_git(*args, **kwargs):
    return 124, 'fixture timeout'
checkpoints._run_with_env = failed_git
config.save_config({'checkpoints_enabled': True})


async def main():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(('127.0.0.1', 0))
    sock.listen(128)
    (root / 'port').write_text(str(sock.getsockname()[1]))
    server = uvicorn.Server(uvicorn.Config(app, log_level='warning', lifespan='on'))
    await server.serve(sockets=[sock])


asyncio.run(main())
