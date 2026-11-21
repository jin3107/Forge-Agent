"""Loopback Uvicorn fixture; real auth/HTTP/agent, deterministic fake LLM only."""
import asyncio
import json
import os
import socket
import sys
from pathlib import Path
from types import SimpleNamespace

import uvicorn
from engine.agent import Agent
from server.main import app, config
from server.auth import auth_store

root = Path(sys.argv[1])
PASSWORD = 'fixture-password-at-least-16-characters'
for email in ('alice@example.test', 'bob@example.test'):
    auth_store.create_user(email, PASSWORD, role='admin')
if os.environ.get('FORGE_TEST_ADD_MEMBER') == '1':
    for email in ('member@example.test', 'member2@example.test'):
        auth_store.create_user(email, PASSWORD, role='member')

# This is a test-only fake provider. No model API/network calls are made.
config.has_provider_credentials = lambda provider: os.environ.get('FORGE_TEST_DEMO') != '1'

# Fixture-only global legacy hook, supplied by the test process, not workspace config.
if os.environ.get('FORGE_TEST_HOOK_ADAPTER') == '1' and os.environ.get('FORGE_TEST_GLOBAL_LEGACY'):
    _original_load_config = config.load_config
    def _fixture_load_config():
        return {**_original_load_config(), 'on_file_change': os.environ['FORGE_TEST_GLOBAL_LEGACY']}
    config.load_config = _fixture_load_config


# Fixture-only clock for the process-local review registry. A test moves that clock forward
# by writing a number of seconds to this file, instead of waiting for the real approval
# lifetime. The registry, with its lifetime, stays the one that the server constructed.
if os.environ.get('FORGE_TEST_HOOK_REVIEW_CLOCK_FILE'):
    import time
    from server import main as _review_main
    _clock_file = Path(os.environ['FORGE_TEST_HOOK_REVIEW_CLOCK_FILE'])
    _clock_offset = [0.0]
    def _shifted_clock():
        try:
            _clock_offset[0] = float(_clock_file.read_text(encoding='ascii'))
        except (OSError, ValueError):
            pass  # no file yet, or read while it is being written: keep the last offset
        return time.monotonic() + _clock_offset[0]
    _review_main._hook_review_requests._clock = _shifted_clock


async def fake_stream(self, messages, tools=None, model=None, temperature=0.3):
    if os.environ.get('FORGE_TEST_HOOK_ADAPTER') == '1':
        with (root/'fake-provider-calls').open('a', encoding='utf-8') as f:
            f.write('called\n')
    last = messages[-1]
    if (os.environ.get('FORGE_TEST_ALLOW_COMPACT') == '1' and last['role'] == 'user' and
            isinstance(last.get('content'), str) and last['content'].startswith('Summarize the following agent conversation')):
        yield {'type': 'delta', 'text': 'fixture compact summary'}
        yield {'type': 'finish', 'reason': 'stop'}
        return
    if last['role'] == 'user':
        request = json.loads(last['content'])
        name = request['name']
        allowed = ('run_background', 'check_background', 'kill_background')
        if os.environ.get('FORGE_TEST_HOOK_ADAPTER') == '1' and os.environ.get('FORGE_TEST_ALLOW_WRITE') == '1':
            allowed += ('write_file',)
        if os.environ.get('FORGE_TEST_HOOK_ADAPTER') == '1' and os.environ.get('FORGE_TEST_ALLOW_SPAWN') == '1':
            allowed += ('spawn_subagent',)
        if os.environ.get('FORGE_TEST_HOOK_ADAPTER') == '1' and os.environ.get('FORGE_TEST_ALLOW_BACKGROUND_AGENT') == '1':
            allowed += ('spawn_background_agent',)
        if name not in allowed:
            raise ValueError('unsupported fixture tool')
        yield {'type': 'tool_calls', 'tool_calls': [{'id': 'fixture-call', 'type': 'function',
               'function': {'name': name, 'arguments': json.dumps(request.get('args', {}))}}]}
    else:
        yield {'type': 'delta', 'text': 'fixture response'}
    yield {'type': 'finish', 'reason': 'stop'}


Agent._stream_chat = fake_stream

# Explicit test-only wiring of the incomplete checked-hook prototypes. This is
# NOT production enforcement, and this harness covers only selected Agent paths.
if (os.environ.get('FORGE_TEST_HOOK_ADAPTER') == '1' and
        os.environ.get('FORGE_TEST_PRODUCTION_RUNNER') != '1'):
    from engine import hooks, hook_checked_runner
    from server import main as server_main
    cache = hook_checked_runner.CheckedSessionStartCache()
    def owner(sid):
        session = server_main.store.get(sid)
        if not session or not session.owner_id:
            raise hooks.HookTrustDenied()
        return session.owner_id
    def resolve(owner_id, sid, ws):
        if os.environ.get('FORGE_TEST_HOOK_INHERIT') == '1':
            return server_main._hook_scope_snapshot_with_inheritance(owner_id, sid, str(ws))
        return server_main._hook_scope_snapshot(owner_id, sid, str(ws))
    async def event(ws, sid, name, extra=None):
        return await hook_checked_runner.run_checked_event(ws, owner(sid), sid, name, extra, resolve, config.load_config())
    async def pre(ws, sid, name, inputs):
        return await hook_checked_runner.checked_pre_tool_use(ws, owner(sid), sid, name, inputs, resolve, config.load_config())
    async def post(ws, sid, name, inputs, output, env_extra=None):
        return await hook_checked_runner.checked_post_tool_use(ws, owner(sid), sid, name, inputs, output, resolve, config.load_config(), env_extra)
    async def session_start(ws, sid):
        return await cache.run(ws, owner(sid), sid, resolve, config.load_config())
    if os.environ.get('FORGE_TEST_INJECTED_HOOK_RUNNER') == '1':
        # Only this fixture injects a per-agent runner; do not monkeypatch the
        # global hooks module. Production server construction is unchanged.
        from engine.session import ScopedSessionStore
        from server.hook_runner import ScopedCheckedHookRunner
        def fixture_agent(store, sid, *args, **kwargs):
            if not isinstance(store, ScopedSessionStore):
                raise ValueError('fixture requires scoped store')
            kwargs.pop('hook_runner', None)  # replace product runner only for this fixture
            runner = ScopedCheckedHookRunner(owner_id=store.user_id, resolve=resolve,
                                             load_config=config.load_config)
            return Agent(store, sid, *args, hook_runner=runner, **kwargs)
        server_main.Agent = fixture_agent
    else:
        hooks.run_event = event
        hooks.pre_tool_use = pre
        hooks.post_tool_use = post
        hooks.session_start_cache = session_start
        # Preserve the legacy test-only adapter for characterization tests;
        # production-runner tests skip this whole fixture block.
        def legacy_fixture_agent(store, sid, *args, **kwargs):
            kwargs.pop('hook_runner', None)
            return Agent(store, sid, *args, **kwargs)
        server_main.Agent = legacy_fixture_agent

# Optional test-only injection: preserve the first real output chunk, then
# simulate a read error without changing the production pump implementation.
if os.environ.get('FORGE_TEST_BACKGROUND_READ_FAILURE') == '1':
    from engine import background
    original_pump = background._pump
    async def injected_pump(task):
        stream = task['proc'].stdout
        count = 0
        async def read(size):
            nonlocal count
            count += 1
            if count == 1:
                return await stream.read(size)
            raise OSError('PRIVATE_INJECTED_READ_ERROR')
        task['proc'].stdout = SimpleNamespace(read=read)
        await original_pump(task)
    background._pump = injected_pump


# Simulate a failed group signal without modifying global os.killpg; the real
# direct-process fallback still terminates the fixture worker.
if os.environ.get('FORGE_TEST_BACKGROUND_GROUP_FAILURE') == '1':
    from engine import background
    def missing_group(pid, sig):
        raise ProcessLookupError('private injected group failure')
    background.os = SimpleNamespace(name=os.name, killpg=missing_group)


async def main():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(('127.0.0.1', 0))
    sock.listen(128)
    (root / 'port').write_text(str(sock.getsockname()[1]))
    server = uvicorn.Server(uvicorn.Config(app, log_level='warning', lifespan='on'))
    await server.serve(sockets=[sock])


asyncio.run(main())
