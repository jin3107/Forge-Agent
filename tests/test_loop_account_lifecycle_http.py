"""Loops, accounts and administrators over real HTTP (loopback fixture server, fake provider).

Demo mode is used on purpose: a demo tick is the kind that the hook approval check never covered,
so only the loop code's own account handling can stop it.
"""
import time

from test_hook_gate_loop_batch_http import LOOP_INTERVAL, FixtureServerCase  # noqa: E402


class LoopAccountLifecycleHTTP(FixtureServerCase):
    @classmethod
    def server_env(cls):
        return {'FORGE_TEST_DEMO': '1'}

    def admin_loops(self):
        answer = self.admin.get('/api/admin/loops')
        self.assertEqual(answer.status_code, 200, answer.text)
        return answer.json()['loops']

    def test_disabling_the_owner_ends_a_demo_loop_at_once_and_it_stays_ended(self):
        owner = self.login('member2@example.test')
        account = '/api/auth/users/%s' % owner.get('/api/auth/me').json()['user']['id']
        sid = self.new_session(owner)
        self.addCleanup(lambda: self.admin.patch(account, json={'enabled': True}))
        self.start_loop(sid, 0, 'hello', client=owner)  # unlimited, demo
        state = self.wait_loop(sid, self.first_tick_over, owner)
        self.assert_tick_ran(state)
        runs = state['runs']

        self.assertEqual(self.admin.patch(account, json={'enabled': False}).status_code, 200)
        self.assertEqual(owner.get('/api/sessions/%s/loop' % sid).status_code, 401)
        self.assertNotIn(sid, [row['session'] for row in self.admin_loops()])
        time.sleep(LOOP_INTERVAL + 3)  # a tick would be due within the interval

        self.assertEqual(self.admin.patch(account, json={'enabled': True}).status_code, 200)
        state = self.loop_state(sid, self.login('member2@example.test'))
        self.assertIs(state.get('finished'), True, state)
        self.assertEqual(state.get('last_error_code'), 'owner_disabled', state)
        self.assertEqual(state.get('runs'), runs, state)  # no tick ran while disabled or after

    def test_administrator_can_list_and_stop_another_accounts_loop(self):
        sid = self.new_session()
        self.start_loop(sid, 0, 'private task text')
        self.wait_loop(sid, self.first_tick_over)
        me = self.member.get('/api/auth/me').json()['user']['id']

        self.assertEqual(self.member.get('/api/admin/loops').status_code, 403)  # not for members
        self.assertEqual(self.member.delete('/api/admin/loops/%s' % sid).status_code, 403)
        rows = [row for row in self.admin_loops() if row['session'] == sid]
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual(rows[0]['owner'], me)
        self.assertNotIn('private task text', str(rows))  # the task text stays with its owner

        answer = self.admin.delete('/api/admin/loops/%s' % sid)
        self.assertEqual((answer.status_code, answer.json()), (200, {'ok': True}))
        self.assertNotIn(sid, [row['session'] for row in self.admin_loops()])
        self.assertFalse(self.loop_state(sid).get('running'))

    def test_deleting_the_session_stops_its_loop(self):
        sid = self.new_session()
        self.start_loop(sid, 0, 'hello')
        self.wait_loop(sid, self.first_tick_over)
        self.assertIn(sid, [row['session'] for row in self.admin_loops()])

        self.assertEqual(self.member.delete('/api/sessions/%s' % sid).json(), {'ok': True})
        self.assertNotIn(sid, [row['session'] for row in self.admin_loops()])
