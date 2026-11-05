import asyncio
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock

from cgm.agent import Runner
from cgm.store import Store
from cgm.toolapi import ToolAPI


class RunnerFixture:
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = Store(Path(tmp.name) / "messages.db")
        self.addCleanup(self.store.db.close)
        cfg = SimpleNamespace(groups=Path(tmp.name) / "groups", history=30,
                              batch_seconds=0, max_sends=5, api_port=0,
                              mcp_python=sys.executable, run_timeout=10,
                              price=(1, 1, 1, 1))
        self.runner = Runner(cfg, self.store, None, None, AsyncMock())

    def message(self, msg_id, chat_id=1, *, directed=False, reply_to=None):
        self.store.touch_chat(chat_id, f"chat {chat_id}")
        self.store.add_message(chat_id, msg_id, "user", f"request {msg_id}",
                               directed=directed, reply_to=reply_to)
        return self.store.get_message(chat_id, msg_id)


class MessageLoopTests(RunnerFixture, unittest.IsolatedAsyncioTestCase):
    async def test_duplicate_batch_shares_one_handle(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def execute(*args, **kwargs):
            entered.set()
            await release.wait()

        self.runner.execute = AsyncMock(side_effect=execute)
        messages = [self.message(1), self.message(2)]
        task = asyncio.create_task(self.runner.run_chat(1, messages))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            handles = self.runner.message_loops
            self.assertIs(handles[(1, 1)], handles[(1, 2)])
            await self.runner.run_chat(1, messages)
            self.runner.execute.assert_awaited_once()
        finally:
            release.set()
            await task
        self.assertEqual(self.runner.message_loops, {})

    async def test_new_arrival_waits_for_next_run_and_history_is_context(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def execute(*args, **kwargs):
            if kwargs["message_ids"] == (1,):
                entered.set()
                await release.wait()

        self.runner.execute = AsyncMock(side_effect=execute)
        self.message(1, directed=True)
        self.runner.trigger(1, urgent=True)
        loop = self.runner.loops[1]
        try:
            await asyncio.wait_for(entered.wait(), 1)
            self.message(2, reply_to=1)
            self.runner.trigger(1)
            self.assertIs(self.runner.loops[1], loop)
            self.runner.execute.assert_awaited_once()
        finally:
            release.set()
            await loop

        first, second = self.runner.execute.await_args_list
        self.assertEqual(first.kwargs["message_ids"], (1,))
        self.assertEqual(second.kwargs["message_ids"], (2,))
        text = second.args[3]
        self.assertIn("本轮只处理这些新消息：#2。", text)
        history, new = text.split("## 新消息")
        self.assertIn("#1 ", history)
        self.assertIn("#2 ", new)
        self.assertIn("不要重新回应其中的旧请求", history)
        self.assertIn("如果新消息追问旧话题或要求重做", history)
        self.assertEqual(self.runner.message_loops, {})
        self.assertEqual(self.store.unseen(1), [])
        # 同一个 Telegram 事件再次触发时，已读游标阻止它重新进入新消息。
        self.runner.trigger(1)
        await self.runner.loops[1]
        self.assertEqual(self.runner.execute.await_count, 2)

    async def test_same_message_id_in_different_chats_is_independent(self):
        entered, release = asyncio.Event(), asyncio.Event()
        count = 0

        async def execute(*args, **kwargs):
            nonlocal count
            count += 1
            if count == 2:
                entered.set()
            await release.wait()

        self.runner.execute = AsyncMock(side_effect=execute)
        tasks = [asyncio.create_task(self.runner.run_chat(cid, [self.message(1, cid)]))
                 for cid in (1, 2)]
        try:
            await asyncio.wait_for(entered.wait(), 1)
            handles = self.runner.message_loops
            self.assertIsNot(handles[(1, 1)], handles[(2, 1)])
        finally:
            release.set()
            await asyncio.gather(*tasks)
        self.assertEqual(self.runner.message_loops, {})

    async def test_preparation_error_releases_ownership(self):
        self.runner._download = AsyncMock(side_effect=RuntimeError("download failed"))
        with self.assertRaisesRegex(RuntimeError, "download failed"):
            await self.runner.run_chat(1, [self.message(1)])
        self.assertEqual(self.runner.message_loops, {})

    async def test_cancellation_during_preparation_releases_ownership(self):
        entered = asyncio.Event()

        async def download(*args):
            entered.set()
            await asyncio.Event().wait()

        self.runner._download = AsyncMock(side_effect=download)
        task = asyncio.create_task(self.runner.run_chat(1, [self.message(1)]))
        try:
            await asyncio.wait_for(entered.wait(), 1)
        finally:
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(self.runner.message_loops, {})

    async def test_private_chat_prompt_limits_scope_without_requiring_reply_to(self):
        self.store.touch_chat(1, "私聊 · user")
        self.runner.execute = AsyncMock()
        message = self.message(1, directed=True)
        self.store.touch_chat(1, "私聊 · user")
        await self.runner.run_chat(1, [message])
        text = self.runner.execute.await_args.args[3]
        self.assertTrue(text.startswith("# 群：私聊 · user"))
        self.assertIn("私聊也只回应本轮新消息", text)
        self.assertEqual(self.runner.execute.await_args.kwargs["message_ids"], (1,))


class ExecutionOwnershipTests(RunnerFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.runner.api = ToolAPI(None, self.store, 0)
        self.runner._codex_cmd = MagicMock(return_value=["codex"])
        self.runner._after_run = AsyncMock()
        self.entered, self.exited = asyncio.Event(), asyncio.Event()
        self.proc = SimpleNamespace(returncode=None)

        async def communicate(*args):
            self.entered.set()
            await self.exited.wait()
            return b'{"type":"turn.completed"}\n', b""

        async def kill(proc):
            proc.returncode = -9
            self.exited.set()

        self.proc.communicate = AsyncMock(side_effect=communicate)
        self.runner._spawn = AsyncMock(return_value=self.proc)
        self.runner._kill = AsyncMock(side_effect=kill)

    async def test_timeout_releases_message_mapping_and_run_token(self):
        self.runner.cfg.run_timeout = 0.01
        await self.runner.run_chat(1, [self.message(1)])
        self.runner._kill.assert_awaited_once_with(self.proc)
        self.assertEqual(self.runner.message_loops, {})
        self.assertEqual(self.runner.api.runs, {})
        self.assertEqual(self.runner.running, {})
        self.assertEqual(self.store.last_runs(1)[0]["timed_out"], 1)

    async def test_stop_keeps_scope_in_context_and_cleans_up(self):
        task = asyncio.create_task(self.runner.run_chat(1, [self.message(1)]))
        try:
            await asyncio.wait_for(self.entered.wait(), 1)
            ctx, = self.runner.api.runs.values()
            self.assertEqual(ctx.message_ids, (1,))
            self.assertIn((1, 1), self.runner.message_loops)
        finally:
            stopped = await self.runner.stop(1)
            await task
        self.assertEqual(stopped, 1)
        self.assertEqual(self.runner.message_loops, {})
        self.assertEqual(self.runner.api.runs, {})
        self.assertEqual(self.runner.running, {})

    async def test_cancelling_run_kills_process_and_cleans_up(self):
        task = asyncio.create_task(self.runner.run_chat(1, [self.message(1)]))
        try:
            await asyncio.wait_for(self.entered.wait(), 1)
        finally:
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.runner._kill.assert_awaited_once_with(self.proc)
        self.assertEqual(self.runner.message_loops, {})
        self.assertEqual(self.runner.api.runs, {})
        self.assertEqual(self.runner.running, {})


if __name__ == "__main__":
    unittest.main()
