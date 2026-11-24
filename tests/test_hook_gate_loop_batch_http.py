"""Hook approval on scheduled loop ticks, batch items and battle items, over real HTTP.

Starts the repository's loopback server (tests/fixtures/http_background_harness.py) with the
product hook wiring and its fake provider: no model API is called and nothing leaves
127.0.0.1. Everything is written to a throwaway folder.

The workspace hook is a small script file that is started as ``<python> <script> <marker>``
and appends one line to the marker file. Such a command line needs no POSIX-only quoting,
so these tests also run on Windows. The hook ends by itself; no test depends on stopping a
shell command.
"""
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import warnings
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
HARNESS = ROOT / 'tests' / 'fixtures' / 'http_background_harness.py'
PASSWORD = 'fixture-password-at-least-16-characters'
HOOK_SCRIPT = ("import sys\n"
               "with open(sys.argv[1], 'a', encoding='ascii') as handle:\n"
               "    handle.write('x\\n')\n")
# The fake provider turns this prompt into one harmless tool call and then a final answer.
PLAIN_PROMPT = json.dumps({'name': 'check_background', 'args': {}})
READY_TIMEOUT = 90   # seconds for the server to answer /api/health
TURN_TIMEOUT = 90    # seconds for one request or one loop state
LOOP_INTERVAL = 10   # seconds; the shortest interval that the scheduler accepts
APPROVAL_LIFETIME = 1800  # seconds; the documented lifetime of an approval (docs/guides/SECURITY.md)


def command_line(argv):
    """One shell command line for ``argv``; the paths may contain spaces."""
    if os.name == 'nt':
        return subprocess.list2cmdline([str(a) for a in argv])
    return ' '.join(shlex.quote(str(a)) for a in argv)


def line_count(path):
    try:
        return len(Path(path).read_text(encoding='ascii', errors='replace').splitlines())
    except OSError:
        return 0


def sse_events(text):
    """The events of an SSE answer; a batch wraps the events of an item, so both levels are listed."""
    found = []
    for line in text.splitlines():
        if not line.startswith('data:'):
            continue
        item = json.loads(line[5:].strip())
        found.append(item)
        if isinstance(item, dict) and isinstance(item.get('event'), dict):
            found.append(item['event'])
    return found


def denied(events):
    return any(isinstance(e, dict) and e.get('code') == 'hook_trust_denied' for e in events)


class FixtureServerCase(unittest.TestCase):
    """One fixture server per test class. Subclasses add switches with ``server_env``."""

    @classmethod
    def server_env(cls):
        return {}

    @classmethod
    def setUpClass(cls):
        cls.root = Path(tempfile.mkdtemp(prefix='forge-gate-'))
        cls.server = cls.log = None
        cls.clients = []
        cls.counted = {}
        try:
            cls.hook_py = cls.root / 'hook_append.py'
            cls.hook_py.write_text(HOOK_SCRIPT, encoding='ascii')
            cls.calls = cls.root / 'fake-provider-calls'
            # A command line that does not work on this machine must not look like a denied hook.
            probe = cls.root / 'marker-preflight'
            done = subprocess.run(cls.hook_command(probe), shell=True, cwd=str(cls.root),
                                  stdin=subprocess.DEVNULL, capture_output=True, timeout=120)
            if done.returncode != 0 or line_count(probe) != 1:
                raise AssertionError('the hook command line does not work on this machine: exit %s, %r'
                                     % (done.returncode, (done.stdout + done.stderr)[-400:]))
            env = {k: v for k, v in os.environ.items() if not k.upper().startswith('FORGE_')}
            env.update(FORGE_DATA_DIR=str(cls.root / 'data'), FORGE_TEST_ADD_MEMBER='1',
                       FORGE_TEST_HOOK_ADAPTER='1', FORGE_TEST_PRODUCTION_RUNNER='1',
                       FORGE_TEST_ALLOW_WRITE='1', FORGE_TEST_ALLOW_SPAWN='1',
                       PYTHONPATH=str(ROOT) + os.pathsep + env.get('PYTHONPATH', ''))
            env.setdefault('PYTHONIOENCODING', 'utf-8')  # as scripts/launch.py sets it
            env.update(cls.server_env())
            cls.log = open(cls.root / 'server.log', 'wb')
            extra = {'start_new_session': True} if os.name == 'posix' else {}
            cls.server = subprocess.Popen([sys.executable, '-u', str(HARNESS), str(cls.root)], cwd=str(ROOT),
                                          env=env, stdin=subprocess.DEVNULL, stdout=cls.log,
                                          stderr=subprocess.STDOUT, **extra)
            cls.base = cls.wait_ready()
            cls.member = cls.login('member@example.test')
            cls.admin = cls.login('alice@example.test')
        except BaseException:
            cls.shutdown()
            raise

    @classmethod
    def tearDownClass(cls):
        # A hook that ran after its test had counted it would be a gate failure too.
        late = {marker.name: '%d -> %d' % (count, line_count(marker))
                for marker, count in cls.counted.items() if line_count(marker) != count}
        cls.shutdown()
        if late:
            raise AssertionError('hook runs changed after they were counted: %r' % late)

    @classmethod
    def shutdown(cls):
        for client in cls.clients:
            client.close()
        proc = cls.server
        if proc is not None:
            if os.name == 'nt':
                # The interpreter of a virtual environment is a launcher with the real interpreter
                # as its child, and a hook runs below a cmd.exe: end the whole tree.
                if proc.poll() is None:
                    try:
                        subprocess.run(['taskkill', '/PID', str(proc.pid), '/T', '/F'],
                                       stdin=subprocess.DEVNULL, capture_output=True, timeout=60)
                    except (OSError, subprocess.TimeoutExpired):
                        pass  # the direct process is ended below
            else:
                if proc.poll() is None:
                    proc.send_signal(signal.SIGTERM)
                    try:
                        proc.wait(timeout=15)
                    except subprocess.TimeoutExpired:
                        pass
                try:
                    os.killpg(proc.pid, signal.SIGKILL)  # the server was started in its own session
                except OSError:
                    pass
            try:
                if proc.poll() is None:
                    proc.kill()
                proc.wait(timeout=30)
            except (OSError, subprocess.TimeoutExpired):
                pass
        if cls.log is not None:
            cls.log.close()
        for _ in range(50):  # on Windows a file stays locked until its process is really gone
            shutil.rmtree(cls.root, ignore_errors=True)
            if not cls.root.exists():
                break
            time.sleep(0.1)
        else:
            warnings.warn('test folder could not be removed: %s' % cls.root, ResourceWarning)

    @classmethod
    def server_log(cls):
        try:
            return (cls.root / 'server.log').read_text(encoding='utf-8', errors='replace')[-3000:]
        except OSError:
            return '<no server log>'

    @classmethod
    def wait_ready(cls):
        limit = time.monotonic() + READY_TIMEOUT
        while time.monotonic() < limit:
            if cls.server.poll() is not None:
                raise AssertionError('the fixture server ended with exit code %s:\n%s'
                                     % (cls.server.returncode, cls.server_log()))
            try:
                port = int((cls.root / 'port').read_text(encoding='ascii').strip())
                url = 'http://127.0.0.1:%d' % port
                if httpx.get(url + '/api/health', trust_env=False, timeout=2).status_code == 200:
                    return url
            except (OSError, ValueError, httpx.HTTPError):
                pass
            time.sleep(0.1)
        raise AssertionError('the fixture server was not ready after %ds:\n%s' % (READY_TIMEOUT, cls.server_log()))

    @classmethod
    def login(cls, email):
        client = httpx.Client(base_url=cls.base, trust_env=False, timeout=TURN_TIMEOUT)
        cls.clients.append(client)
        client.headers['X-CSRF-Token'] = client.get('/api/auth/me').json()['csrf_token']
        answer = client.post('/api/auth/login', json={'email': email, 'password': PASSWORD})
        if answer.status_code != 200:
            raise AssertionError('login of %s answered HTTP %s' % (email, answer.status_code))
        client.headers['X-CSRF-Token'] = answer.json()['csrf_token']
        return client

    @classmethod
    def hook_command(cls, marker):
        return command_line([sys.executable, cls.hook_py, marker])

    # ---- helpers for one test -------------------------------------------------------------
    def marker(self, name):
        return self.root / ('marker-' + name)

    def workspace(self, sid):
        return self.root / 'data' / 'workspaces' / sid

    def new_session(self, client=None):
        answer = (client or self.member).post('/api/sessions', json={})
        self.assertEqual(answer.status_code, 200, answer.text)
        sid = answer.json()['session']['id']
        self.workspace(sid).mkdir(parents=True, exist_ok=True)
        return sid

    def write_hooks(self, sid, hooks):
        folder = self.workspace(sid) / '.forge'
        folder.mkdir(parents=True, exist_ok=True)
        (folder / 'config.json').write_text(json.dumps({'hooks': hooks}), encoding='utf-8')

    def self_written_hook(self, marker):
        """A prompt whose turn writes a hook configuration with a ``stop`` hook into its workspace."""
        config = json.dumps({'hooks': {'stop': self.hook_command(marker)}})
        return json.dumps({'name': 'write_file', 'args': {'path': '.forge/config.json', 'content': config}})

    def approve(self, sid):
        """The member asks for a review of the current configuration and an administrator approves."""
        preview = self.member.get('/api/workspace/hook-review', params={'sid': sid}).json()
        self.assertEqual(preview['state'], 'hooks', preview)
        scope = {'expected_workspace': preview['workspace'], 'expected_digest': preview['digest']}
        sent = self.member.post('/api/workspace/hook-review-requests',
                                json=dict(scope, sid=sid, share_commands=True))
        self.assertEqual(sent.status_code, 200, sent.text)
        request_id = sent.json()['request_id']
        decided = self.admin.post('/api/workspace/hook-review-requests/%s/decision' % request_id,
                                  json=dict(scope, decision='approve'))
        self.assertEqual(decided.status_code, 200, decided.text)
        return request_id

    def withdraw(self, request_id):
        answer = self.admin.delete('/api/workspace/hook-review-requests/%s' % request_id)
        self.assertEqual(answer.status_code, 200, answer.text)

    def provider_calls(self):
        return line_count(self.calls)

    def assert_hook_runs(self, marker, expected):
        self.assertEqual(line_count(marker), expected, 'runs of the hook with %s' % marker.name)
        type(self).counted[marker] = expected  # counted once more when the class is done

    def chat(self, sid, text):
        answer = self.member.post('/api/sessions/%s/chat' % sid, json={'text': text, 'mode': 'agent'})
        self.assertEqual(answer.status_code, 200, answer.text)
        return sse_events(answer.text)

    def stream(self, url, body):
        answer = self.member.post(url, json=body)
        self.assertEqual(answer.status_code, 200, answer.text)
        return sse_events(answer.text)

    def start_loop(self, sid, max_runs, text=PLAIN_PROMPT, client=None):
        """Start a loop with the shortest interval; the first tick runs at once."""
        client = client or self.member
        url = '/api/sessions/%s/loop' % sid
        answer = client.post(url, json={'text': text, 'every': '%ds' % LOOP_INTERVAL, 'mode': 'agent',
                                        'max_runs': max_runs})
        self.assertEqual(answer.status_code, 200, answer.text)
        self.addCleanup(client.delete, url)

    def loop_state(self, sid, client=None):
        answer = (client or self.member).get('/api/sessions/%s/loop' % sid)
        self.assertEqual(answer.status_code, 200, answer.text)
        return answer.json()

    @staticmethod
    def first_tick_over(state):
        return state.get('last_ok') is not None

    def wait_loop(self, sid, reached=lambda state: state.get('finished'), client=None):
        limit = time.monotonic() + TURN_TIMEOUT
        while True:
            state = self.loop_state(sid, client)
            if reached(state):
                return state
            if self.server.poll() is not None:
                self.fail('the fixture server ended:\n' + self.server_log())
            if time.monotonic() >= limit:
                self.fail('loop state not reached after %ds: %r' % (TURN_TIMEOUT, state))
            time.sleep(0.1)

    def one_tick(self, sid, text=PLAIN_PROMPT):
        """Run a loop of one tick; returns its final state and the provider calls it made."""
        before = self.provider_calls()
        self.start_loop(sid, 1, text)
        state = self.wait_loop(sid)
        self.assertEqual(state.get('runs'), 1, state)
        return state, self.provider_calls() - before

    def assert_tick_ran(self, state):
        self.assertIs(state.get('last_ok'), True, state)
        self.assertNotIn('last_error_code', state)

    def assert_tick_denied(self, state):
        self.assertIs(state.get('finished'), True, state)
        self.assertIs(state.get('last_ok'), False, state)
        self.assertEqual(state.get('last_error_code'), 'hook_trust_denied', state)

    def assert_items_denied(self, events, marker):
        created = [e for e in events if e.get('type') == 'task_created']
        self.assertTrue(created, events)
        self.assertTrue(denied(events), events)
        # The item ran until its write: an earlier effect is not undone by the denial.
        self.assertTrue(any((self.workspace(e['session_id']) / '.forge' / 'config.json').is_file()
                            for e in created), events)
        finished = [e for e in events if e.get('type') == 'task_done']
        self.assertEqual(len(finished), len(created), events)
        self.assertTrue(all(e.get('ok') is False for e in finished), finished)
        self.assertEqual([e.get('ok') for e in events if e.get('type') == 'batch_done'], [False], events)
        self.assert_hook_runs(marker, 0)

    def assert_items_ran(self, events, items):
        self.assertFalse(denied(events), events)
        self.assertFalse([e for e in events if e.get('type') == 'error'], events)
        finished = [e for e in events if e.get('type') == 'task_done']
        self.assertEqual([e.get('ok') for e in finished], [True] * items, events)
        self.assertEqual([e.get('ok') for e in events if e.get('type') == 'batch_done'], [True], events)


class LoopBatchBattleGateHTTP(FixtureServerCase):
    """Non-demo loop ticks, batch items and battle items check hook approval like normal chat."""

    def test_loop_tick_runs_a_workspace_hook_only_while_it_is_approved(self):
        first, second = self.marker('loop-first'), self.marker('loop-second')
        sid = self.new_session()
        self.write_hooks(sid, {'user_prompt_submit': self.hook_command(first)})

        state, calls = self.one_tick(sid)  # nobody approved the hook
        self.assert_tick_denied(state)
        self.assertEqual(calls, 0)
        self.assert_hook_runs(first, 0)

        self.approve(sid)
        state, calls = self.one_tick(sid)
        self.assert_tick_ran(state)
        self.assertGreater(calls, 0)
        self.assert_hook_runs(first, 1)

        # A changed configuration is a new, unapproved one.
        self.write_hooks(sid, {'user_prompt_submit': self.hook_command(second)})
        state, calls = self.one_tick(sid)
        self.assert_tick_denied(state)
        self.assertEqual(calls, 0)
        self.assert_hook_runs(first, 1)
        self.assert_hook_runs(second, 0)

        request_id = self.approve(sid)
        state, calls = self.one_tick(sid)
        self.assert_tick_ran(state)
        self.assert_hook_runs(second, 1)

        self.withdraw(request_id)
        state, calls = self.one_tick(sid)
        self.assert_tick_denied(state)
        self.assertEqual(calls, 0)
        self.assert_hook_runs(second, 1)
        self.assert_hook_runs(first, 1)

    def test_subagent_of_a_loop_tick_is_gated_with_its_parent(self):
        marker = self.marker('loop-subagent')
        sid = self.new_session()
        self.write_hooks(sid, {'user_prompt_submit': self.hook_command(marker)})
        spawn = json.dumps({'name': 'spawn_subagent', 'args': {'task': PLAIN_PROMPT, 'isolation': 'shared'}})

        state, calls = self.one_tick(sid, spawn)
        self.assert_tick_denied(state)
        self.assertEqual(calls, 0)
        self.assert_hook_runs(marker, 0)

        self.approve(sid)
        state, calls = self.one_tick(sid, spawn)
        self.assert_tick_ran(state)
        # Once for the tick and once for its subagent, which shares the workspace and inherits
        # the approval of its parent while it is active.
        self.assert_hook_runs(marker, 2)

    def test_loop_started_from_chat_is_gated_and_reports_the_denial(self):
        marker = self.marker('loop-from-chat')
        sid = self.new_session()
        self.write_hooks(sid, {'user_prompt_submit': self.hook_command(marker)})
        before = self.provider_calls()
        self.addCleanup(self.member.delete, '/api/sessions/%s/loop' % sid)
        started = self.chat(sid, '/loop %ds %s' % (LOOP_INTERVAL, PLAIN_PROMPT))  # answered without a model turn
        self.assertFalse(denied(started), started)
        state = self.wait_loop(sid, self.first_tick_over)
        self.assert_tick_denied(state)  # an unlimited loop: it ended only because the tick was denied
        self.assertEqual(state.get('runs'), 1, state)
        self.assertEqual(self.provider_calls() - before, 0)
        self.assert_hook_runs(marker, 0)
        shown = self.chat(sid, '/loop')
        self.assertTrue(any('hook_trust_denied' in str(e.get('content')) for e in shown), shown)

    def test_loop_tick_without_a_hook_configuration_still_runs(self):
        state, calls = self.one_tick(self.new_session())
        self.assert_tick_ran(state)
        self.assertGreater(calls, 0)

    def test_loop_of_a_disabled_owner_stops_at_its_next_tick(self):
        owner = self.login('member2@example.test')  # an account of its own; the other tests keep theirs
        account = '/api/auth/users/%s' % owner.get('/api/auth/me').json()['user']['id']
        sid = self.new_session(owner)  # no hook configuration: nothing here needs an approval
        url = '/api/sessions/%s/loop' % sid

        def tidy():
            self.admin.patch(account, json={'enabled': True})
            self.login('member2@example.test').delete(url)
        self.addCleanup(tidy)

        before = self.provider_calls()
        self.start_loop(sid, 0, client=owner)  # unlimited
        state = self.wait_loop(sid, self.first_tick_over, owner)
        self.assert_tick_ran(state)
        per_tick = self.provider_calls() - before
        self.assertGreater(per_tick, 0)

        self.assertEqual(self.admin.patch(account, json={'enabled': False}).status_code, 200)
        self.assertEqual(owner.get(url).status_code, 401)  # so the owner cannot ask for the loop state
        time.sleep(LOOP_INTERVAL + 5)  # the next tick would be due within the interval
        self.assertEqual(self.provider_calls() - before, per_tick)  # no turn ran for the disabled owner

        self.assertEqual(self.admin.patch(account, json={'enabled': True}).status_code, 200)
        state = self.loop_state(sid, self.login('member2@example.test'))
        # Disabling the account ended the loop at once, before its next tick, with a reason that
        # names the account; it did not go on after the account was enabled again.
        self.assertIs(state.get('finished'), True, state)
        self.assertIs(state.get('last_ok'), False, state)
        self.assertEqual(state.get('last_error_code'), 'owner_disabled', state)
        self.assertEqual(state.get('runs'), 1, state)
        self.assertEqual(self.provider_calls() - before, per_tick)

    def test_batch_item_cannot_run_a_hook_that_its_own_turn_wrote(self):
        marker = self.marker('batch')
        events = self.stream('/api/batch', {'prompts': [self.self_written_hook(marker)]})
        self.assert_items_denied(events, marker)

    def test_battle_items_cannot_run_a_hook_that_their_own_turn_wrote(self):
        marker = self.marker('battle')
        for models in ([], ['openrouter:vendor/one', 'openrouter:vendor/two']):
            with self.subTest(models=models):
                events = self.stream('/api/battle', {'prompt': self.self_written_hook(marker), 'models': models})
                self.assertEqual(len([e for e in events if e.get('type') == 'task_created']), len(models) or 1)
                self.assert_items_denied(events, marker)

    def test_batch_and_battle_items_without_hooks_still_run(self):
        before = self.provider_calls()
        self.assert_items_ran(self.stream('/api/batch', {'prompts': [PLAIN_PROMPT, PLAIN_PROMPT]}), 2)
        self.assert_items_ran(self.stream('/api/battle', {'prompt': PLAIN_PROMPT, 'models': []}), 1)
        self.assertGreater(self.provider_calls(), before)


class LoopAfterApprovalLifetimeHTTP(FixtureServerCase):
    """An approval ends 30 minutes after its request was submitted; a loop does not outlive it."""

    @classmethod
    def server_env(cls):
        # Fixture-only switch: the review registry reads its clock offset (seconds) from this file.
        cls.clock = cls.root / 'review-clock-offset'
        return {'FORGE_TEST_HOOK_REVIEW_CLOCK_FILE': str(cls.clock)}

    def later(self, seconds):
        self.clock.write_text(str(seconds), encoding='ascii')

    def test_running_loop_stops_at_its_first_tick_after_the_lifetime(self):
        marker = self.marker('lifetime')
        sid = self.new_session()
        self.write_hooks(sid, {'user_prompt_submit': self.hook_command(marker)})
        self.approve(sid)
        before = self.provider_calls()

        self.later(APPROVAL_LIFETIME - 100)  # well inside the lifetime: the approval still counts
        self.start_loop(sid, 0)              # unlimited; a tick every 10 s
        state = self.wait_loop(sid, self.first_tick_over)
        self.assert_tick_ran(state)
        self.assertEqual(state.get('runs'), 1, state)
        self.assert_hook_runs(marker, 1)
        per_tick = self.provider_calls() - before
        self.assertGreater(per_tick, 0)

        self.later(APPROVAL_LIFETIME)        # 30 minutes after the request was submitted
        begun = self.loop_state(sid)['runs']
        # Nobody stops the loop. A tick that begins from now on must be denied, and that ends
        # the loop; a second tick after this point would mean that one of them was let through.
        state = self.wait_loop(sid, lambda s: s.get('finished') or s.get('runs', 0) > begun + 1)
        self.assert_tick_denied(state)
        self.assertGreaterEqual(state.get('runs'), 2, state)
        # Every tick before the denied one ran the hook once and called the provider; the
        # denied tick did neither. (Normally there are two ticks. The relation also holds if a
        # slow machine let one more tick begin before the clock was moved.)
        self.assert_hook_runs(marker, state['runs'] - 1)
        self.assertEqual(self.provider_calls() - before, per_tick * (state['runs'] - 1))


class DemoStaysOutsideTheGateHTTP(FixtureServerCase):
    """Characterization, not enforcement: a scripted demo turn is not covered by the gate."""

    @classmethod
    def server_env(cls):
        return {'FORGE_TEST_DEMO': '1'}

    def test_demo_loop_tick_is_not_denied_and_runs_no_workspace_hook(self):
        marker = self.marker('demo-loop')
        sid = self.new_session()
        command = self.hook_command(marker)
        self.write_hooks(sid, {'user_prompt_submit': command, 'pre_tool_use': command, 'stop': command})
        state, calls = self.one_tick(sid, 'hello')
        self.assert_tick_ran(state)
        self.assertEqual(calls, 0)  # the demo turn is scripted
        self.assertTrue((self.workspace(sid) / 'hello_forge.py').is_file())
        self.assert_hook_runs(marker, 0)

    def test_demo_batch_item_is_not_denied(self):
        before = self.provider_calls()
        events = self.stream('/api/batch', {'prompts': ['hello']})
        self.assert_items_ran(events, 1)
        created = [e for e in events if e.get('type') == 'task_created']
        self.assertTrue((self.workspace(created[0]['session_id']) / 'hello_forge.py').is_file())
        self.assertEqual(self.provider_calls() - before, 0)  # the demo turn is scripted


if __name__ == '__main__':
    unittest.main()
