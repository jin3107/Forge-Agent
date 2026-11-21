"""Which hook runner a scheduled loop tick, a batch item and a battle item are built with.

In process, with mocks only: no server, no provider, no hook command and no stored data.
The behaviour over real HTTP is in test_hook_gate_loop_batch_http.py.
"""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from engine import agent as agent_module


class BatchRunnerFactory(unittest.IsolatedAsyncioTestCase):
    """run_batch/run_battle ask the caller's factory for one runner per non-demo item."""

    def setUp(self):
        self.built = []      # (session id, keyword arguments) of every Agent that was constructed
        self.runners = []    # what the factory returned, in order
        count = [0]

        def create(model='', title=''):
            count[0] += 1
            return SimpleNamespace(id='s%d' % count[0], title=title, model=model)
        self.store = SimpleNamespace(create=create)

    def factory(self):
        self.runners.append(object())
        return self.runners[-1]

    async def collect(self, stream, credentials=lambda provider: True):
        async def send(prompt):
            yield {'type': 'assistant_message', 'content': 'real'}

        async def demo(agent, prompt, language='vi'):
            yield {'type': 'assistant_message', 'content': 'demo'}

        def build(store, sid, **kwargs):
            self.built.append((sid, kwargs))
            return SimpleNamespace(send=send)

        with patch.object(agent_module, 'Agent', side_effect=build), \
                patch.object(agent_module, 'send_demo', demo), \
                patch.object(agent_module.config, 'has_provider_credentials', side_effect=credentials):
            return [event async for event in stream]

    def answers(self, events):
        return [e['event']['content'] for e in events
                if e['type'] == 'task_event' and e['event']['type'] == 'assistant_message']

    async def test_each_non_demo_item_gets_its_own_runner(self):
        events = await self.collect(agent_module.run_batch(
            self.store, [{'prompt': 'a'}, {'prompt': 'b'}], demo=False, hook_runner_factory=self.factory))
        self.assertEqual(len(self.runners), 2)
        self.assertIsNot(self.runners[0], self.runners[1])
        self.assertEqual(sorted(sid for sid, _ in self.built), ['s1', 's2'])
        self.assertCountEqual([kwargs.get('hook_runner') for _, kwargs in self.built], self.runners)
        self.assertEqual(self.answers(events), ['real', 'real'])
        self.assertTrue(next(e for e in events if e['type'] == 'batch_done')['ok'])

    async def test_demo_items_get_no_runner_and_the_factory_is_not_called(self):
        events = await self.collect(agent_module.run_batch(
            self.store, [{'prompt': 'a'}, {'prompt': 'b'}], demo=True, hook_runner_factory=self.factory))
        self.assertEqual(self.runners, [])
        self.assertEqual([kwargs.get('hook_runner') for _, kwargs in self.built], [None, None])
        self.assertEqual(self.answers(events), ['demo', 'demo'])

    async def test_battle_decides_for_each_model(self):
        events = await self.collect(
            agent_module.run_battle(self.store, 'p', ['openrouter:vendor/with-key', 'google:without-key'],
                                    demo=None, hook_runner_factory=self.factory),
            credentials=lambda provider: provider == 'openrouter')
        runner_of = {kwargs['model']: kwargs.get('hook_runner') for _, kwargs in self.built}
        self.assertEqual(len(self.runners), 1)
        self.assertIs(runner_of['openrouter:vendor/with-key'], self.runners[0])
        self.assertIsNone(runner_of['google:without-key'])
        self.assertCountEqual(self.answers(events), ['real', 'demo'])

    async def test_without_a_factory_no_runner_is_passed(self):
        # Callers outside the server (the CLI batch) keep their own hook policy.
        for call in (agent_module.run_batch(self.store, [{'prompt': 'a'}], demo=False),
                     agent_module.run_battle(self.store, 'p', ['openrouter:vendor/x'], demo=False)):
            await self.collect(call)
        self.assertEqual([kwargs.get('hook_runner') for _, kwargs in self.built], [None, None])

    async def test_a_failing_factory_fails_the_item_before_an_agent_is_built(self):
        def broken():
            raise RuntimeError('no runner')
        events = await self.collect(agent_module.run_batch(
            self.store, [{'prompt': 'a'}], demo=False, hook_runner_factory=broken))
        self.assertEqual(self.built, [])  # the item must not run without the runner
        self.assertEqual(self.answers(events), [])
        done = next(e['event'] for e in events if e['type'] == 'task_event' and e['event']['type'] == 'task_done')
        self.assertFalse(done['ok'])
        self.assertFalse(next(e for e in events if e['type'] == 'batch_done')['ok'])


class ServerEntryPoints(unittest.IsolatedAsyncioTestCase):
    """The server binds the checked runner to the account that owns the loop or sent the request."""

    def setUp(self):
        from server import main
        self.main = main

    def assert_checked_runner_of(self, runner, owner_id):
        from server.hook_runner import ScopedCheckedHookRunner
        self.assertIsInstance(runner, ScopedCheckedHookRunner)
        self.assertEqual(runner._owner_id, owner_id)

    async def loop_turn(self, credentials):
        main = self.main
        built = []

        async def turn(*args, **kwargs):
            yield {'type': 'assistant_message', 'content': 'answer'}

        def build(db, sid, **kwargs):
            built.append(kwargs)
            return SimpleNamespace(send=turn)

        scoped = SimpleNamespace(get=lambda sid: SimpleNamespace(model='test'))
        with patch.object(main, 'ScopedSessionStore', return_value=scoped), \
                patch.object(main, 'Agent', side_effect=build), \
                patch.object(main, 'send_demo', turn), \
                patch.object(main.config, 'has_provider_credentials', return_value=credentials):
            answer = await main._loop_runner('owner-1', False)('s', 'work', 'agent')
        self.assertEqual(answer, 'answer')
        self.assertEqual(len(built), 1)
        return built[0]

    async def test_non_demo_loop_tick_gets_a_runner_bound_to_the_loop_owner(self):
        kwargs = await self.loop_turn(credentials=True)
        self.assert_checked_runner_of(kwargs.get('hook_runner'), 'owner-1')

    async def test_demo_loop_tick_gets_no_runner(self):
        kwargs = await self.loop_turn(credentials=False)
        self.assertIsNone(kwargs.get('hook_runner'))

    async def test_batch_and_battle_pass_a_factory_bound_to_the_signed_in_user(self):
        main = self.main
        request = SimpleNamespace(is_disconnected=AsyncMock(return_value=False))
        user = SimpleNamespace(id='user-7', role='member')
        for endpoint in ('batch', 'battle'):
            with self.subTest(endpoint=endpoint):
                seen = []

                async def source(*args, **kwargs):
                    seen.append(kwargs)
                    yield {'type': 'batch_done', 'ok': True, 'tasks': []}

                with patch.object(main, '_db', return_value=SimpleNamespace()), \
                        patch.object(main.model_registry, 'route', return_value=SimpleNamespace(id='test')), \
                        patch.object(main.model_router, 'default_model', return_value='test'), \
                        patch.object(main.config, 'has_provider_credentials', return_value=True), \
                        patch.object(main, 'run_batch' if endpoint == 'batch' else 'run_battle', side_effect=source):
                    response = (await main.batch(main.BatchBody(prompts=['x']), request, user=user)
                                if endpoint == 'batch' else
                                await main.battle(main.BattleBody(prompt='x', models=['m']), request, user=user))
                    body = ''.join([chunk async for chunk in response.body_iterator])
                self.assertIn('batch_done', body)
                self.assertEqual(len(seen), 1)
                factory = seen[0].get('hook_runner_factory')
                self.assertTrue(callable(factory))
                first, second = factory(), factory()
                self.assert_checked_runner_of(first, 'user-7')
                self.assert_checked_runner_of(second, 'user-7')
                self.assertIsNot(first, second)  # one runner, with its own start-hook cache, per item


if __name__ == '__main__':
    unittest.main()
