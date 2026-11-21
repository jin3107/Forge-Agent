"""Package-version parsing closes its file on every normal/error path."""
import io
import importlib.util
import unittest
from unittest.mock import patch
from server import main

@unittest.skipUnless(importlib.util.find_spec('tomllib'), 'tomllib is unavailable on this Python')
class PackageVersionFile(unittest.TestCase):
    def test_success_closes_source(self):
        stream = io.BytesIO(b'[project]\nversion = "9.8.7"\n')
        with patch('builtins.open', return_value=stream) as opened:
            self.assertEqual(main._pkg_version(), '9.8.7')
        opened.assert_called_once_with(main.ROOT / 'pyproject.toml', 'rb')
        self.assertTrue(stream.closed)

    def test_invalid_or_missing_version_closes_source_and_falls_back(self):
        for data in (b'[project\n', b'[project]\nname="test"\n'):
            with self.subTest(data=data):
                stream = io.BytesIO(data)
                with patch('builtins.open', return_value=stream):
                    self.assertEqual(main._pkg_version(), '?')
                self.assertTrue(stream.closed)

    def test_read_failure_closes_and_falls_back(self):
        class BrokenStream(io.BytesIO):
            def read(self, *args): raise OSError('read failed')
        stream = BrokenStream(b'test')
        with patch('builtins.open', return_value=stream):
            self.assertEqual(main._pkg_version(), '?')
        self.assertTrue(stream.closed)

    def test_open_failure_falls_back(self):
        with patch('builtins.open', side_effect=OSError('unavailable')):
            self.assertEqual(main._pkg_version(), '?')

class PackageHealth(unittest.IsolatedAsyncioTestCase):
    async def test_missing_tomllib_keeps_fallback(self):
        with patch.dict('sys.modules', {'tomllib': None}), patch('builtins.open') as opened:
            self.assertEqual(main._pkg_version(), '?')
            opened.assert_not_called()

    async def test_health_uses_cached_version_without_file_read(self):
        with patch.object(main, 'APP_VERSION', 'test-version'), patch.object(main, '_pkg_version', side_effect=AssertionError('must not reread')):
            response = await main.health()
        self.assertEqual(response['version'], 'test-version')
        self.assertTrue(response['ok'])
