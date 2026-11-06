import asyncio
from pathlib import Path
import tempfile
import unittest
from typing import cast
from unittest.mock import AsyncMock, patch
from pydantic import ValidationError
import bridge
from models import Job


def message(**overrides: object) -> dict[str, object]:
    return {"id": "msg-1", "chatID": "!chat:beeper.local", "timestamp": "2026-11-05T20:00:00Z",
            "isSender": True, "text": "@bb investigate this", "linkedMessageID": "older-message", **overrides}


def spawn_response() -> dict[str, str]:
    return {"id": "thr_test"}


def args(*extra: str) -> bridge.Options:
    return bridge.parser().parse_args(["run", "--project", "proj_test", *extra], namespace=bridge.Options())


class TriggerTests(unittest.TestCase):
    def test_payload_types_are_strict(self) -> None:
        for changes in ({"isSender": "true"}, {"linkedMessageID": 7}, {"text": 123}):
            with self.subTest(changes=changes):
                self.assertIsNone(bridge.trigger(message(**changes), "server", [], 0))
        # Beeper may add fields unrelated to trigger handling.
        self.assertIsNotNone(bridge.trigger(message(newField="ignored"), "server", [], 0))

    def test_job_model_validates_serialized_payload(self) -> None:
        job = bridge.trigger(message(), "server", [], 0)
        assert job is not None
        self.assertEqual(Job.model_validate_json(job.model_dump_json()), job)
        with self.assertRaises(ValidationError):
            Job.model_validate_json('{"key": 123, "instruction": "task", "source": {}}')
        with self.assertRaises(ValidationError):
            Job.model_validate_json(job.model_dump_json()[:-1] + ', "unexpected": true}')

    def test_own_prefix_only(self) -> None:
        for changes in ({"isSender": False}, {"isSender": None}, {"isDeleted": True}, {"isHidden": True},
                        {"text": "someone said @bb investigate"}, {"text": "@bbq"}, {"id": ""},
                        {"text": "@BB investigate"}, {"text": " @bb investigate"},
                        {"text": "@bb\ninvestigate"}, {"text": "@bb"}, {"text": "@bb "},
                        {"timestamp": "bad"}, {"timestamp": "2026-11-05T20:00:00"}):
            with self.subTest(changes=changes):
                self.assertIsNone(bridge.trigger(message(**changes), "server", [], 0))
        job = bridge.trigger(message(text="@bb investigate\nthis"), "server", [], 0)
        assert job is not None
        self.assertEqual(job.instruction, "investigate\nthis")

    def test_age_chat_and_dedup_identity(self) -> None:
        self.assertIsNone(bridge.trigger(message(), "server", ["another-chat"], 0))
        self.assertIsNone(bridge.trigger(message(), "server", [], 2_000_000_000))
        first = bridge.trigger(message(), "server", [], 0)
        assert first is not None
        edited = bridge.trigger(message(text="@bb different instruction"), "server", [], 0)
        assert edited is not None
        self.assertEqual(first.key, edited.key)
        other = bridge.trigger(message(chatID="other"), "server", [], 0)
        assert other is not None
        assert other is not None
        self.assertNotEqual(first.key, other.key)

    def test_reference_prompt_preserves_instruction(self) -> None:
        job = bridge.trigger(message(text="@bb inspect `literal` $(literal)\nand explain"), "server", [], 0)
        assert job is not None
        text = bridge.prompt(job)
        self.assertIn("inspect `literal` $(literal)\nand explain", text)
        self.assertIn('"repliedToMessageID": "older-message"', text)
        self.assertIn('"timestamp": "2026-11-05T20:00:00Z"', text)
        self.assertIn("Beeper MCP", text)
        self.assertIn("Read the source conversation for context using Beeper MCP.\nStart", text)
        self.assertNotIn("local Beeper CLI", text)

    def test_suffix_preserved_verbatim(self) -> None:
        suffix = " investigate this\nincluding details  "
        job = bridge.trigger(message(text="@bb " + suffix), "server", [], 0)
        assert job is not None
        self.assertEqual(job.instruction, suffix)
        self.assertIn(suffix, bridge.prompt(job))


class BridgeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "state.sqlite3"
        self.state = bridge.State(self.path)
        self.runner = bridge.Bridge(args(), self.state)
        self.runner.since = 0

    def tearDown(self) -> None:
        self.state.close()
        self.temp.cleanup()

    async def test_stored_job_is_validated_before_spawning(self) -> None:
        job = bridge.trigger(message(), "server", [], 0)
        assert job is not None
        self.state.enqueue(job, self.runner.spawn_argv())
        pending = self.state.pending()
        self.assertEqual(pending[0][1], job)
        self.state.db.execute("UPDATE jobs SET payload='{}'")
        self.state.db.commit()
        with patch("bridge.command", new=AsyncMock()) as command:
            with self.assertRaisesRegex(RuntimeError, "Stored job"):
                await self.runner.drain()
        command.assert_not_awaited()

    async def test_malformed_spawn_response_is_uncertain(self) -> None:
        job = bridge.trigger(message(), "server", [], 0)
        assert job is not None
        self.state.enqueue(job, self.runner.spawn_argv())
        invalid: dict[str, object] = {"id": 123}
        with patch("bridge.command", new=AsyncMock(return_value=invalid)):
            await self.runner.drain()
        self.assertEqual(self.state.jobs()[0]["state"], "uncertain")

    async def test_duplicate_edits_and_restarts_spawn_once(self) -> None:
        sent = message()
        event: dict[str, object] = {"type": "message.upserted", "chatID": sent["chatID"], "entries": [sent]}
        with patch("bridge.command", new=AsyncMock(return_value=spawn_response())) as command:
            await self.runner.event(event)
            sent["text"] = "@bb edited"
            await self.runner.event(event)
            self.state.close()
            self.state = bridge.State(self.path)
            runner = bridge.Bridge(args(), self.state)
            runner.since = 0
            await runner.event(event)
        command.assert_awaited_once()
        assert command.await_args is not None
        argv, text = cast(tuple[list[str], str], command.await_args.args)
        expected: list[str] = ["bb", "thread", "spawn"]
        self.assertEqual(argv[:3], expected)
        self.assertIn("--prompt-file", argv)
        self.assertIn("investigate this", text)
        self.assertEqual(self.state.jobs()[0]["threadID"], "thr_test")

    async def test_partial_event_hydrates_and_captures_reply(self) -> None:
        event = {"type": "message.upserted", "chatID": message()["chatID"], "ids": ["msg-1"]}
        with patch.object(self.runner, "beeper", new=AsyncMock(return_value=message())) as beeper:
            with patch("bridge.command", new=AsyncMock(return_value=spawn_response())) as command:
                await self.runner.event(event)
        beeper.assert_awaited_once_with("messages", "show", "--chat", message()["chatID"], "--id", "msg-1")
        assert command.await_args is not None
        _, text = cast(tuple[list[str], str], command.await_args.args)
        self.assertIn("older-message", text)

    async def test_inbound_and_noncommands_do_not_hydrate(self) -> None:
        event = {"type": "message.upserted", "chatID": message()["chatID"], "entries": [
            {"id": "one", "isSender": False}, {"id": "two", "text": "hello"}]}
        with patch.object(self.runner, "beeper", new=AsyncMock()) as beeper:
            await self.runner.event(event)
        beeper.assert_not_awaited()
        self.assertFalse(self.state.jobs())

    async def test_ambiguous_spawn_never_retries_automatically(self) -> None:
        job = bridge.trigger(message(), "server", [], 0)
        assert job is not None
        self.state.enqueue(job, self.runner.spawn_argv())
        with patch("bridge.command", new=AsyncMock(side_effect=RuntimeError("transport lost"))) as command:
            await self.runner.drain()
            await self.runner.drain()
        command.assert_awaited_once()
        self.assertEqual(self.state.jobs()[0]["state"], "uncertain")

    async def test_killed_spawn_becomes_uncertain_on_restart(self) -> None:
        job = bridge.trigger(message(), "server", [], 0)
        assert job is not None
        self.state.enqueue(job, self.runner.spawn_argv())
        self.state.mark(job.key, "spawning")
        self.state.close()
        self.state = bridge.State(self.path)
        self.assertEqual(self.state.jobs()[0]["state"], "uncertain")

    async def test_dry_run_does_not_spawn(self) -> None:
        self.runner.args.dry_run = True
        self.runner.accept(message())
        with patch("bridge.command", new=AsyncMock()) as command:
            await self.runner.drain()
        command.assert_not_awaited()
        self.assertEqual(self.state.jobs()[0]["state"], "dry-run")

    async def test_actual_watcher_subprocess_and_stdin_spawn(self) -> None:
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
            # The fixture rejects every Beeper command except watch. Staying
            # connected while idle confirms no background search is attempted.
            await asyncio.sleep(0.05)
            self.assertFalse(task.done())
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_timeout_terminates_child(self) -> None:
        import sys
        with self.assertRaises(asyncio.TimeoutError):
            await bridge.command([sys.executable, "-c", "import time; time.sleep(30)"], timeout=0.05)

    async def test_reconnects_after_watcher_disconnect(self) -> None:
        failures: list[BaseException] = [RuntimeError("disconnected"), asyncio.CancelledError()]
        with patch.object(self.runner, "watch_once", new=AsyncMock(side_effect=failures)) as watch:
            with patch("bridge.asyncio.sleep", new=AsyncMock()) as sleep:
                with self.assertRaises(asyncio.CancelledError):
                    await self.runner.run()
        self.assertEqual(watch.await_count, 2)
        sleep.assert_awaited_once_with(1)

    async def test_single_instance_lock(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "Another bridge"):
            bridge.State(self.path)


if __name__ == "__main__":
    unittest.main()
