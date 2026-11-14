"""AuthStore SQLite connections must close after transactions and setup errors."""
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
from engine.auth import AuthStore

class TrackedConnection(sqlite3.Connection):
    instances = []
    fail_pragma = None
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.closed = False
        self.instances.append(self)
    def execute(self, sql, *args, **kwargs):
        if self.fail_pragma and sql.startswith(self.fail_pragma):
            raise sqlite3.OperationalError('injected setup failure')
        return super().execute(sql, *args, **kwargs)
    def close(self):
        self.closed = True
        return super().close()

class AuthConnectionLifecycle(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='forge-auth-lifecycle-')
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'auth.db'
        TrackedConnection.instances = []
        TrackedConnection.fail_pragma = None
        original = sqlite3.connect
        def connect(*args, **kwargs):
            kwargs['factory'] = TrackedConnection
            return original(*args, **kwargs)
        self.patcher = patch('engine.auth.sqlite3.connect', side_effect=connect)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def assert_all_closed(self):
        self.assertTrue(TrackedConnection.instances)
        self.assertTrue(all(c.closed for c in TrackedConnection.instances), 'every opened connection must be explicitly closed')
        for c in TrackedConnection.instances:
            with self.assertRaises(sqlite3.ProgrammingError): c.execute('SELECT 1')

    def test_normal_reads_writes_early_return_and_errors_close(self):
        store = AuthStore(self.path)
        self.assertFalse(store.has_users())
        user = store.create_user('alice@example.test', name='Alice')
        self.assertEqual(store.get(user.id).id, user.id)
        self.assertIsNone(store.get('missing'))
        self.assertEqual(len(store.list_users()), 1)
        store.set_registration_enabled(True)
        self.assertTrue(store.registration_enabled())
        flow = store.create_oauth_flow()
        self.assertEqual(store.consume_oauth_flow(flow['state'])['nonce'], flow['nonce'])
        self.assertIsNone(store.consume_oauth_flow(flow['state']))
        token = store.create_session(user.id)
        self.assertEqual(store.user_for_session(token).id, user.id)
        store.set_enabled(user.id, False)
        self.assertIsNone(store.user_for_session(token))
        with self.assertRaises(ValueError): store.create_user('alice@example.test')
        self.assert_all_closed()

    def test_transaction_commits_and_exception_rolls_back_and_closes(self):
        store = AuthStore(self.path)
        with store._connect() as db:
            db.execute("INSERT INTO auth_settings(key,value) VALUES('commit-test','yes')")
        with self.assertRaisesRegex(RuntimeError, 'rollback'):
            with store._connect() as db:
                db.execute("INSERT INTO auth_settings(key,value) VALUES('rollback-test','no')")
                raise RuntimeError('rollback')
        with store._connect() as db:
            self.assertEqual(db.execute("SELECT value FROM auth_settings WHERE key='commit-test'").fetchone()['value'], 'yes')
            self.assertIsNone(db.execute("SELECT value FROM auth_settings WHERE key='rollback-test'").fetchone())
        self.assert_all_closed()

    def test_commit_failure_rolls_back_and_closes(self):
        store = AuthStore(self.path)
        with store._connect() as db:
            db.execute('CREATE TABLE parent (id INTEGER PRIMARY KEY)')
            db.execute('CREATE TABLE child (pid INTEGER REFERENCES parent(id) DEFERRABLE INITIALLY DEFERRED)')
        with self.assertRaises(sqlite3.IntegrityError):
            with store._connect() as db:
                db.execute('INSERT INTO child(pid) VALUES (999)')
        with store._connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) AS n FROM child').fetchone()['n'], 0)
        self.assert_all_closed()

    def test_threaded_sessions_and_oauth_flows_close_connections(self):
        store = AuthStore(self.path)
        user = store.create_user('threaded@example.test')
        def task(_):
            token = store.create_session(user.id)
            flow = store.create_oauth_flow()
            self.assertEqual(store.user_for_session(token).id, user.id)
            self.assertEqual(store.consume_oauth_flow(flow['state'])['nonce'], flow['nonce'])
            self.assertIsNone(store.consume_oauth_flow(flow['state']))
            store.revoke_session(token)
            self.assertIsNone(store.user_for_session(token))
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(task, range(16)))
        self.assert_all_closed()

    def test_connection_setup_failure_closes_open_connection(self):
        for pragma in ('PRAGMA foreign_keys', 'PRAGMA busy_timeout'):
            with self.subTest(pragma=pragma):
                TrackedConnection.fail_pragma = pragma
                with self.assertRaisesRegex(sqlite3.OperationalError, 'injected setup failure'):
                    AuthStore(self.path)
                self.assert_all_closed()
