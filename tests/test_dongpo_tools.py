import asyncio
import importlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from aiohttp import web
from aiohttp.test_utils import TestServer

from cgm import dongpo
from cgm.store import Store
from cgm.toolapi import DongpoReviewRequired, RunCtx, ToolAPI, ToolError


class DongpoToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "out").mkdir()
        (self.root / "out/file.txt").write_text("file")
        self.store = Store(self.root / "test.db")
        self.addCleanup(self.store.db.close)
        self.store.touch_chat(1, "test")
        self.tp = SimpleNamespace(me_id=99, send_text=AsyncMock(return_value=10),
                                  edit_text=AsyncMock(), send_file=AsyncMock(return_value=11),
                                  react=AsyncMock())
        self.api = ToolAPI(self.tp, self.store, 0)
        self.ctx = self.api.register(RunCtx("test", 1, self.root, 5))
        patcher = patch.object(dongpo, "detect", return_value=True)
        self.detect = patcher.start()
        self.addCleanup(patcher.stop)

    async def test_rejection_has_no_side_effects_and_retry_preserves_reply(self):
        with self.assertRaisesRegex(DongpoReviewRequired, "保留有效信息"):
            await self.api.t_send_message(self.ctx, "但这不代表……", reply_to=2)
        self.assertEqual(self.ctx.sends, 0)
        self.assertIsNone(self.store.get_message(1, 10))
        self.tp.send_text.assert_not_awaited()
        result = await self.api.t_send_message(self.ctx, "修好了", reply_to=2)
        self.assertEqual(result, {"message_id": 10, "parts": 1})
        self.assertEqual(self.ctx.sends, 1)
        self.tp.send_text.assert_awaited_once_with(1, "修好了", 2)
        self.assertEqual(self.store.get_message(1, 10).text, "修好了")
        self.detect.assert_called_once()

    async def test_successful_draft_does_not_use_up_later_review(self):
        self.detect.side_effect = [False, True]
        await self.api.t_send_message(self.ctx, "服务恢复了")
        with self.assertRaises(DongpoReviewRequired):
            await self.api.t_send_message(self.ctx, "但这不代表……")
        self.assertEqual(self.ctx.sends, 1)
        self.assertEqual(self.tp.send_text.await_count, 1)

    async def test_each_run_and_chat_has_its_own_flag(self):
        contexts = [self.ctx, RunCtx("next", 1, self.root, 5), RunCtx("other", 2, self.root, 5)]
        for ctx in contexts:
            with self.assertRaises(DongpoReviewRequired):
                await self.api.t_send_message(ctx, "但这不代表……")
            self.assertEqual(ctx.sends, 0)
        self.assertEqual(self.detect.call_count, 3)

    async def test_concurrent_calls_reject_at_most_once(self):
        results = await asyncio.gather(
            *(self.api.t_send_message(self.ctx, "但这不代表……") for _ in range(2)),
            return_exceptions=True)
        self.assertEqual(sum(isinstance(result, DongpoReviewRequired) for result in results), 1)
        self.assertEqual(sum(isinstance(result, dict) for result in results), 1)
        self.assertEqual(self.ctx.sends, 1)
        self.detect.assert_called_once()

    async def test_disabled_never_loads_or_scores(self):
        self.api.not_dongpo = False
        with patch.object(dongpo, "load_judge") as load:
            await self.api.t_send_message(self.ctx, "但这不代表……")
        load.assert_not_called()
        self.detect.assert_not_called()
        self.assertFalse(self.ctx.dongpo_flagged)

    async def test_edit_is_rejected_before_telegram_or_database_change(self):
        self.store.add_message(1, 8, "你", "等一下", sender_id=99, is_self=True)
        with self.assertRaises(DongpoReviewRequired):
            await self.api.t_edit_message(self.ctx, 8, "但这不代表……")
        self.assertEqual(self.store.get_message(1, 8).text, "等一下")
        self.tp.edit_text.assert_not_awaited()
        await self.api.t_edit_message(self.ctx, 8, "修好了")
        self.assertEqual(self.store.get_message(1, 8).text, "修好了")
        self.tp.edit_text.assert_awaited_once_with(1, 8, "修好了")
        self.assertEqual(self.ctx.sends, 0)

    async def test_file_caption_review_and_retry(self):
        with self.assertRaises(DongpoReviewRequired):
            await self.api.t_send_file(self.ctx, "out/file.txt", "但这不代表……", reply_to=2)
        self.assertEqual(self.ctx.sends, 0)
        self.tp.send_file.assert_not_awaited()
        await self.api.t_send_file(self.ctx, "out/file.txt", "结果在这里", reply_to=2)
        self.tp.send_file.assert_awaited_once_with(
            1, (self.root / "out/file.txt").resolve(), "结果在这里", 2, False)
        self.assertEqual(self.ctx.sends, 1)

    async def test_file_without_caption_does_not_score(self):
        await self.api.t_send_file(self.ctx, "out/file.txt")
        self.detect.assert_not_called()

    async def test_invalid_requests_do_not_consume_review(self):
        for call in (self.api.t_send_message(self.ctx, " "),
                     self.api.t_edit_message(self.ctx, 404, "但这不代表……"),
                     self.api.t_send_file(self.ctx, "missing", "但这不代表……")):
            with self.assertRaises(ToolError):
                await call
        self.detect.assert_not_called()
        self.assertFalse(self.ctx.dongpo_flagged)

    async def test_review_does_not_disable_send_limit(self):
        self.ctx.max_sends = 0
        with self.assertRaises(DongpoReviewRequired):
            await self.api.t_send_message(self.ctx, "但这不代表……")
        with self.assertRaisesRegex(ToolError, "不能再发"):
            await self.api.t_send_message(self.ctx, "修好了")
        self.tp.send_text.assert_not_awaited()

    async def test_http_error_and_mcp_error_preserve_review_code_and_guidance(self):
        req = SimpleNamespace(headers={"X-Run-Token": self.ctx.token},
                              match_info={"name": "send_message"},
                              json=AsyncMock(return_value={"text": "但这不代表……"}))
        with self.assertLogs("cgm.tools", level="WARNING"):
            response = await self.api._handle(req)
        body = json.loads(response.text)
        self.assertFalse(body["ok"])
        self.assertEqual(body["code"], "dongpo_review_required")
        self.assertIn("重试原工具", body["error"])
        self.assertNotIn("code", json.loads((await self.api._handle(req)).text))
        with patch.dict("os.environ", {"CGM_API": "http://example.invalid", "CGM_RUN_TOKEN": "test"}):
            mcp = importlib.import_module("cgm.mcp_telegram")
        fake_response = MagicMock()
        fake_response.__enter__.return_value = io.BytesIO(json.dumps(body).encode())
        with patch.object(mcp.urllib.request, "urlopen", return_value=fake_response):
            with self.assertRaises(RuntimeError) as error:
                mcp._call("send_message", text="但这不代表……")
        self.assertEqual(json.loads(str(error.exception)), body)

    async def test_model_failure_is_actionable_and_does_not_consume_review(self):
        self.detect.side_effect = RuntimeError("缺少东坡模型")
        with self.assertLogs("cgm.tools", level="ERROR"):
            with self.assertRaisesRegex(ToolError, "草稿尚未发送.*缺少东坡模型"):
                await self.api.t_send_message(self.ctx, "但这不代表……")
        self.assertFalse(self.ctx.dongpo_flagged)
        self.assertEqual(self.ctx.sends, 0)
        self.tp.send_text.assert_not_awaited()

    async def test_missing_weights_stop_startup_without_loading_model(self):
        with patch.object(dongpo, "MODEL_PATH", self.root / "missing.json"):
            with patch.object(dongpo, "load_judge") as load:
                with self.assertRaisesRegex(RuntimeError, "python -m cgm.dongpo --download"):
                    await self.api.start()
                load.assert_not_called()


@unittest.skipUnless(dongpo.MODEL_PATH.is_file(), "尚未下载真实东坡模型")
class DongpoRealHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_model_through_mcp_and_http_reviews_then_retries(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = Store(root / "test.db")
            self.addCleanup(store.db.close)
            store.touch_chat(1, "test")
            tp = SimpleNamespace(me_id=99, send_text=AsyncMock(side_effect=[10, 11]))
            api = ToolAPI(tp, store, 0)
            ctx = api.register(RunCtx("real-model", 1, root, 5))
            app = web.Application()
            app.router.add_post("/tool/{name}", api._handle)
            with patch.dict("os.environ", {"CGM_API": "http://unused.invalid", "CGM_RUN_TOKEN": ctx.token}):
                mcp = await asyncio.to_thread(importlib.import_module, "cgm.mcp_telegram")
            async with TestServer(app) as server:
                with (patch.object(mcp, "API", str(server.make_url("")).rstrip("/")),
                      patch.object(mcp, "TOKEN", ctx.token)):
                    normal = "不需要重启，修改配置后会自动生效。"
                    result = json.loads(await asyncio.to_thread(mcp._call, "send_message", text=normal))
                    self.assertEqual(result["message_id"], 10)
                    self.assertFalse(ctx.dongpo_flagged)
                    with self.assertLogs("cgm.tools", level="WARNING"):
                        with self.assertRaises(RuntimeError) as error:
                            await asyncio.to_thread(mcp._call, "send_message",
                                                    text="已经修好了，但这不代表以后不会出错。")
                    self.assertEqual(json.loads(str(error.exception))["code"], "dongpo_review_required")
                    self.assertEqual(ctx.sends, 1)
                    self.assertIsNone(store.get_message(1, 11))
                    corrected = "已经修好了，修改配置后会自动生效。"
                    result = json.loads(await asyncio.to_thread(mcp._call, "send_message", text=corrected))
                    self.assertEqual(result["message_id"], 11)
                    self.assertEqual(ctx.sends, 2)
                    self.assertEqual(store.get_message(1, 11).text, corrected)
                    self.assertEqual(tp.send_text.await_count, 2)


if __name__ == "__main__":
    unittest.main()
