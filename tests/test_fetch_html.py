"""HTML text extraction regressions for fetch_url (not an HTML sanitizer)."""
import asyncio
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from engine import tools

ROOT = Path(__file__).resolve().parent.parent


class HTMLExtraction(unittest.IsolatedAsyncioTestCase):
    def test_typical_html_text_entities_and_hidden_sections(self):
        html = ('<head><title>HIDDEN_HEAD</title></head><p>Xin&nbsp;chào &amp; bạn</p>'
                '<script>HIDDEN_SCRIPT</script><style>HIDDEN_STYLE</style>'
                '<noscript>HIDDEN_NOSCRIPT</noscript><svg>HIDDEN_SVG</svg>'
                '<div>Dòng<br class="x">sau &lt;code&gt;</div>')
        result = tools._strip_html(html)
        self.assertIn('Xin chào & bạn', result)
        self.assertIn('Dòng\nsau <code>', result)
        for hidden in ('HIDDEN_HEAD', 'HIDDEN_SCRIPT', 'HIDDEN_STYLE', 'HIDDEN_NOSCRIPT', 'HIDDEN_SVG'):
            self.assertNotIn(hidden, result)

    def test_quoted_angle_comment_and_unclosed_script(self):
        result = tools._strip_html('<p title="a > b">visible</p><!-- HIDDEN_COMMENT -->'
                                   '<script>HIDDEN_UNCLOSED')
        self.assertIn('visible', result)
        self.assertNotIn('b">', result)
        self.assertNotIn('HIDDEN_COMMENT', result)
        self.assertNotIn('HIDDEN_UNCLOSED', result)

    def test_unicode_offsets_raw_script_and_partial_close(self):
        html = ('İ visible<script> if (a < b) x(); </scripture>STILL_HIDDEN'
                '</ScRiPt><p>AFTER</p>')
        result = tools._strip_html(html)
        self.assertIn('İ visible', result)
        self.assertIn('AFTER', result)
        self.assertNotIn('STILL_HIDDEN', result)
        self.assertNotIn('x();', result)

    def test_character_references_are_decoded_only_once(self):
        result = tools._strip_html('<p>&#38;amp; &amp;lt; &#00000065; &#x000041;</p>')
        self.assertEqual(result, '&amp; &lt; A A')

    def test_very_long_numeric_entity_does_not_raise(self):
        result = tools._strip_html('start &#' + '9' * 10000 + '; end')
        self.assertIn('start', result)
        self.assertIn('end', result)
        self.assertIn('�', result)
        self.assertIn('A', tools._strip_html('&#' + '0' * 5000 + '65;'))

    def test_adversarial_tags_complete_in_bounded_test_process(self):
        # subprocess timeout prevents a broken implementation from freezing the test runner.
        code = ("from engine.tools import _strip_html; "
                "_strip_html('<script>'*15000); _strip_html('<'*120000); "
                "_strip_html('<script>'+'</scriptx>'*11000); "
                "_strip_html('\\n'+' '*119999); print('done')")
        completed = subprocess.run([sys.executable, '-c', code], cwd=ROOT,
                                   capture_output=True, text=True, timeout=3, check=True)
        self.assertEqual(completed.stdout.strip(), 'done')

    async def test_fetch_integration_retains_byte_cap_and_readable_text(self):
        original = httpx.AsyncClient
        content = b'<p>BEFORE</p><script>hidden</script><p>AFTER</p>' + b'<script>' * 20000
        def factory(*args, **kwargs):
            kwargs['transport'] = httpx.MockTransport(
                lambda r: httpx.Response(200, headers={'content-type': 'text/html'}, content=content))
            return original(*args, **kwargs)
        with patch('engine.tools._public_addresses', return_value=['93.184.216.34']), \
             patch('httpx.AsyncClient', side_effect=factory):
            result = await asyncio.wait_for(tools._fetch_url(None, {'url': 'https://example.com/'}), 4)
        self.assertIn('BEFORE', result)
        self.assertIn('AFTER', result)
        self.assertNotIn('hidden', result)
        self.assertIn('giới hạn tải', result)


if __name__ == '__main__':
    unittest.main()
