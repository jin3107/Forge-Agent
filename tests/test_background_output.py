"""Regression tests for incremental background output and buffer wraparound."""
import asyncio
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from engine import background


def task():
    return {"id": "bg-test", "cmd": "test", "buf": bytearray(), "off": 0,
            "base_offset": 0, "status": "running", "code": None,
            "started": time.time(), "killed": False}


async def feed(t, *chunks):
    proc = SimpleNamespace(stdout=SimpleNamespace(read=AsyncMock(side_effect=[*chunks, b""])),
                           wait=AsyncMock(return_value=0))
    t["proc"] = proc
    await background._pump(t)


class BackgroundOutput(unittest.IsolatedAsyncioTestCase):
    async def test_new_output_after_full_buffer_was_consumed(self):
        t = task()
        await feed(t, b"a" * background.BUF_CAP)
        # Drain all retained data, then append bytes while the buffer is full.
        for _ in range((background.BUF_CAP // background.VIEW_CAP) + 1):
            background.render(t, incremental=True)
        await feed(t, b"NEW_OUTPUT_MARKER")
        result = background.render(t, incremental=True)
        self.assertIn("NEW_OUTPUT_MARKER", result)
        self.assertNotIn("output lost", result.lower())
        self.assertNotIn("NEW_OUTPUT_MARKER", background.render(t, incremental=True))

    async def test_buffer_overflow_reports_unread_loss(self):
        t = task()
        await feed(t, b"a" * (background.BUF_CAP + 100))
        result = background.render(t, incremental=True)
        self.assertIn("100", result)
        self.assertIn("output lost", result.lower())
        self.assertNotIn("output lost", background.render(t, incremental=True).lower())

    async def test_view_limit_keeps_unread_output_for_next_call(self):
        t = task()
        await feed(t, b"a" * background.VIEW_CAP + b"TAIL_MARKER")
        first = background.render(t, incremental=True)
        self.assertNotIn("TAIL_MARKER", first)
        self.assertIn("remaining", first.lower())
        second = background.render(t, incremental=True)
        self.assertIn("TAIL_MARKER", second)
        self.assertNotIn("TAIL_MARKER", background.render(t, incremental=True))

    async def test_utf8_split_across_view_boundary(self):
        t = task()
        text = "a" * (background.VIEW_CAP - 1) + "ệ" + "END"
        await feed(t, text.encode("utf-8"))
        first = background.render(t, incremental=True)
        second = background.render(t, incremental=True)
        self.assertIn("ệEND", second)
        self.assertNotIn("�", first + second)

    async def test_real_background_process_reports_overflow_and_drains_tail(self):
        with tempfile.TemporaryDirectory(prefix="forge-bg-output-") as td:
            code = "import sys; sys.stdout.write('a'*70000 + 'TAIL_MARKER'); sys.stdout.flush()"
            tid, _ = await background.start("output fixture", Path(td),
                                            exec_args=[sys.executable, "-c", code])
            t = background.get(tid)
            try:
                await asyncio.wait_for(t["proc"].wait(), timeout=10)
                # Wait for the existing pump to publish its final status.
                async def wait_for_pump():
                    while t["status"] == "running":
                        await asyncio.sleep(0.01)
                await asyncio.wait_for(wait_for_pump(), timeout=5)
                pages = [background.render(t, incremental=True)]
                while t["off"] < t["base_offset"] + len(t["buf"]):
                    pages.append(background.render(t, incremental=True))
                self.assertIn("output lost", pages[0].lower())
                self.assertIn("TAIL_MARKER", "\n".join(pages))
                self.assertEqual(len(t["buf"]), background.BUF_CAP)
            finally:
                background._TASKS.pop(tid, None)

    async def test_incomplete_utf8_waits_for_next_chunk_while_running(self):
        t = task()
        ready, release = asyncio.Event(), asyncio.Event()
        reads = 0
        async def read(_):
            nonlocal reads
            reads += 1
            if reads == 1:
                return b"\xe1\xbb"
            if reads == 2:
                ready.set()
                await release.wait()
                return b"\x87END"
            return b""
        t["proc"] = SimpleNamespace(stdout=SimpleNamespace(read=read), wait=AsyncMock(return_value=0))
        pump = asyncio.create_task(background._pump(t))
        try:
            await asyncio.wait_for(ready.wait(), 1)
            first = background.render(t, incremental=True)
            self.assertNotIn("�", first)
            release.set()
            await asyncio.wait_for(pump, 1)
            result = background.render(t, incremental=True)
            self.assertIn("ệEND", result)
            self.assertNotIn("�", result)
        finally:
            release.set()
            await asyncio.gather(pump, return_exceptions=True)


if __name__ == "__main__":
    unittest.main()
