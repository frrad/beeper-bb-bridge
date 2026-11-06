import asyncio
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
import bridge


def message(**overrides):
    return {"id": "msg-1", "chatID": "!chat:beeper.local", "timestamp": "2026-11-05T20:00:00Z",
            "isSender": True, "text": "@bb investigate this", "linkedMessageID": "older-message", **overrides}


def args(*extra):
    return bridge.parser().parse_args(["run", "--project", "proj_test", *extra])


class TriggerTests(unittest.TestCase):
    def test_own_prefix_only(self):
        for changes in ({"isSender": False}, {"isSender": None}, {"isDeleted": True}, {"isHidden": True},
                        {"text": "someone said @bb investigate"}, {"text": "@bbq"}, {"id": ""},
                        {"text": "@BB investigate"}, {"text": " @bb investigate"},
                        {"text": "@bb\ninvestigate"}, {"text": "@bb"}, {"text": "@bb "},
                        {"timestamp": "bad"}, {"timestamp": "2026-11-05T20:00:00"}):
            with self.subTest(changes=changes):
                self.assertIsNone(bridge.trigger(message(**changes), "server", [], 0))
        job = bridge.trigger(message(text="@bb investigate\nthis"), "server", [], 0)
        self.assertEqual(job["instruction"], "investigate\nthis")

    def test_age_chat_and_dedup_identity(self):
        self.assertIsNone(bridge.trigger(message(), "server", ["another-chat"], 0))
        self.assertIsNone(bridge.trigger(message(), "server", [], 2_000_000_000))
        first = bridge.trigger(message(), "server", [], 0)
        edited = bridge.trigger(message(text="@bb different instruction"), "server", [], 0)
        self.assertEqual(first["key"], edited["key"])
        self.assertNotEqual(first["key"], bridge.trigger(message(chatID="other"), "server", [], 0)["key"])

    def test_reference_prompt_preserves_instruction(self):
        job = bridge.trigger(message(text="@bb inspect `literal` $(literal)\nand explain"), "server", [], 0)
        text = bridge.prompt(job)
        self.assertIn("inspect `literal` $(literal)\nand explain", text)
        self.assertIn('"repliedToMessageID": "older-message"', text)
        self.assertIn('"timestamp": "2026-11-05T20:00:00Z"', text)
        self.assertIn("Beeper MCP", text)
        self.assertIn("Read the source conversation for context using Beeper MCP.\nStart", text)
        self.assertNotIn("local Beeper CLI", text)

    def test_suffix_preserved_verbatim(self):
        suffix = " investigate this\nincluding details  "
        job = bridge.trigger(message(text="@bb " + suffix), "server", [], 0)
        self.assertEqual(job["instruction"], suffix)
        self.assertIn(suffix, bridge.prompt(job))


class BridgeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "state.sqlite3"
        self.state = bridge.State(self.path)
        self.runner = bridge.Bridge(args(), self.state)
        self.runner.since = 0

    def tearDown(self):
        self.state.close()
        self.temp.cleanup()

    async def test_duplicate_edits_and_restarts_spawn_once(self):
        event = {"type": "message.upserted", "chatID": message()["chatID"], "entries": [message()]}
        with patch("bridge.command", new=AsyncMock(return_value={"id": "thr_test"})) as command:
            await self.runner.event(event)
            event["entries"][0]["text"] = "@bb edited"
            await self.runner.event(event)
            self.state.close()
            self.state = bridge.State(self.path)
            runner = bridge.Bridge(args(), self.state)
            runner.since = 0
            await runner.event(event)
        command.assert_awaited_once()
        argv, text = command.await_args.args
        self.assertEqual(argv[:3], ["bb", "thread", "spawn"])
        self.assertIn("--prompt-file", argv)
        self.assertIn("investigate this", text)
        self.assertEqual(self.state.jobs()[0]["threadID"], "thr_test")

    async def test_partial_event_hydrates_and_captures_reply(self):
        event = {"type": "message.upserted", "chatID": message()["chatID"], "ids": ["msg-1"]}
        with patch.object(self.runner, "beeper", new=AsyncMock(return_value=message())) as beeper:
            with patch("bridge.command", new=AsyncMock(return_value={"id": "thr_test"})) as command:
                await self.runner.event(event)
        beeper.assert_awaited_once_with("messages", "show", "--chat", message()["chatID"], "--id", "msg-1")
        self.assertIn("older-message", command.await_args.args[1])

    async def test_inbound_and_noncommands_do_not_hydrate(self):
        event = {"type": "message.upserted", "chatID": message()["chatID"], "entries": [
            {"id": "one", "isSender": False}, {"id": "two", "text": "hello"}]}
        with patch.object(self.runner, "beeper", new=AsyncMock()) as beeper:
            await self.runner.event(event)
        beeper.assert_not_awaited()
        self.assertEqual(self.state.jobs(), [])

    async def test_ambiguous_spawn_never_retries_automatically(self):
        job = bridge.trigger(message(), "server", [], 0)
        self.state.enqueue(job, self.runner.spawn_argv())
        with patch("bridge.command", new=AsyncMock(side_effect=RuntimeError("transport lost"))) as command:
            await self.runner.drain()
            await self.runner.drain()
        command.assert_awaited_once()
        self.assertEqual(self.state.jobs()[0]["state"], "uncertain")

    async def test_killed_spawn_becomes_uncertain_on_restart(self):
        job = bridge.trigger(message(), "server", [], 0)
        self.state.enqueue(job, self.runner.spawn_argv())
        self.state.mark(job["key"], "spawning")
        self.state.close()
        self.state = bridge.State(self.path)
        self.assertEqual(self.state.jobs()[0]["state"], "uncertain")

    async def test_dry_run_does_not_spawn(self):
        self.runner.args.dry_run = True
        self.runner.accept(message())
        with patch("bridge.command", new=AsyncMock()) as command:
            await self.runner.drain()
        command.assert_not_awaited()
        self.assertEqual(self.state.jobs()[0]["state"], "dry-run")

    async def test_catchup_overflow_keeps_checkpoint(self):
        checkpoint = self.state.checkpoint(self.runner.scope)
        own = message(timestamp=bridge.iso(datetime.now(timezone.utc).timestamp()))
        with patch.object(self.runner, "beeper", new=AsyncMock(return_value=[own] * 1000)):
            with self.assertRaisesRegex(RuntimeError, "1000"):
                await self.runner.sweep()
        self.assertEqual(self.state.checkpoint(self.runner.scope), checkpoint)
        self.assertEqual(len(self.state.jobs()), 1)

    async def test_periodic_catchup_recovers_missing_event(self):
        own = message(timestamp=bridge.iso(datetime.now(timezone.utc).timestamp()))
        with patch.object(self.runner, "beeper", new=AsyncMock(return_value=[own])):
            with patch("bridge.command", new=AsyncMock(return_value={"id": "thr_catchup"})) as command:
                await self.runner.sweep()
                await self.runner.sweep()
        command.assert_awaited_once()

    async def test_actual_watcher_subprocess_and_stdin_spawn(self):
        fixture = Path(self.temp.name) / "fake_cli.py"
        fixture.write_text('''#!/usr/bin/env python3
import json, sys, time
from datetime import datetime, timezone
if sys.argv[1] == 'watch':
    print(json.dumps({'event': 'watch.open'}), file=sys.stderr, flush=True)
    print(json.dumps({'type': 'message.upserted', 'chatID': '!chat:beeper.local', 'entries': [{
        'id': 'integration', 'isSender': True, 'text': '@bb inspect `literal` $(literal)',
        'timestamp': datetime.now(timezone.utc).isoformat(), 'linkedMessageID': 'old'}]}), flush=True)
    time.sleep(30)
elif sys.argv[1:3] == ['messages', 'search']:
    print(json.dumps({'success': True, 'data': []}))
elif sys.argv[1:3] == ['thread', 'spawn']:
    text = sys.stdin.read()
    assert '$(literal)' in text and 'old' in text and '--prompt-file' in sys.argv
    print(json.dumps({'id': 'thr_integration'}))
else:
    sys.exit(3)
''')
        fixture.chmod(0o700)
        self.runner.args.beeper = str(fixture)
        self.runner.args.bb = str(fixture)
        task = asyncio.create_task(self.runner.watch_once())
        try:
            async with asyncio.timeout(5):
                while not self.state.jobs() or self.state.jobs()[0]["state"] != "spawned":
                    if task.done():
                        task.result()
                    await asyncio.sleep(0.02)
            self.assertEqual(self.state.jobs()[0]["threadID"], "thr_integration")
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_timeout_terminates_child(self):
        import sys
        with self.assertRaises(asyncio.TimeoutError):
            await bridge.command([sys.executable, "-c", "import time; time.sleep(30)"], timeout=0.05)

    async def test_reconnects_after_watcher_disconnect(self):
        with patch.object(self.runner, "watch_once", new=AsyncMock(side_effect=[RuntimeError("disconnected"), asyncio.CancelledError()])) as watch:
            with patch("bridge.asyncio.sleep", new=AsyncMock()) as sleep:
                with self.assertRaises(asyncio.CancelledError):
                    await self.runner.run()
        self.assertEqual(watch.await_count, 2)
        sleep.assert_awaited_once_with(1)

    async def test_single_instance_lock(self):
        with self.assertRaisesRegex(RuntimeError, "Another bridge"):
            bridge.State(self.path)


if __name__ == "__main__":
    unittest.main()
