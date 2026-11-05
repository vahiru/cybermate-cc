import base64
import tempfile
from email.parser import BytesParser
from email.policy import default
from types import SimpleNamespace
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from cgm.toolapi import RunCtx, ToolAPI, ToolError


class ImageInputTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.run = self.root / "run"
        for folder in ("in", "out"):
            (self.run / folder).mkdir(parents=True)
        self.ctx = RunCtx("test", 1, self.run, 5)
        self.requests = []
        self.api = ToolAPI(None, None, 0, llm_base_url="https://image.invalid",
                           llm_key="test-key", image_model="test-model")
        response = MagicMock(status=200)
        response.json = AsyncMock(return_value={"data": [{
            "b64_json": base64.b64encode(b"result").decode()}]})
        self.response = response
        session = MagicMock()
        session.post.side_effect = lambda url, **kwargs: self.capture(url, kwargs)
        session_context = MagicMock()
        session_context.__aenter__ = AsyncMock(return_value=session)
        patcher = patch("cgm.toolapi.aiohttp.ClientSession", return_value=session_context)
        patcher.start()
        self.addCleanup(patcher.stop)

    def capture(self, url, kwargs):
        async def enter():
            self.assertEqual(kwargs["headers"]["Authorization"], "Bearer test-key")
            if "data" in kwargs:
                payload = kwargs["data"]()
                chunks = []
                await payload.write(SimpleNamespace(write=AsyncMock(side_effect=chunks.append)))
                message = BytesParser(policy=default).parsebytes(
                    ("Content-Type: " + payload.content_type + "\r\n\r\n").encode()
                    + b"".join(chunks))
                kwargs["fields"] = [
                    (part.get_param("name", header="content-disposition"),
                     part.get_filename(), part.get_content_type(), part.get_payload(decode=True))
                    for part in message.iter_parts()]
            self.requests.append((url.rsplit("/", 1)[-1], kwargs))
            return self.response
        request = MagicMock()
        request.__aenter__ = AsyncMock(side_effect=enter)
        return request

    async def test_text_generation_and_empty_images(self):
        for images in (None, []):
            result = await self.api.t_generate_image(self.ctx, "draw", images=images)
            self.assertEqual(result, {"path": "out/image.png"})
            self.assertEqual((self.run / result["path"]).read_bytes(), b"result")
            self.assertEqual((self.requests[-1][0], self.requests[-1][1]["json"]), ("generations", {
                "model": "test-model", "prompt": "draw", "n": 1}))

    async def test_multiple_images_uploaded_in_order(self):
        (self.run / "in/a.png").write_bytes(b"first-image")
        second = self.run / "out/b.JPG"
        second.write_bytes(b"second-image")
        result = await self.api.t_generate_image(self.ctx, "combine", "combined.png",
                                                ["in/a.png", str(second)])
        endpoint, kwargs = self.requests[0]
        fields = kwargs["fields"]
        self.assertEqual(endpoint, "edits")
        self.assertEqual([(f[0], f[3]) for f in fields[:3]], [
            ("model", b"test-model"), ("prompt", b"combine"), ("n", b"1")])
        self.assertEqual(fields[3:], [
            ("image[]", "a.png", "image/png", b"first-image"),
            ("image[]", "b.JPG", "image/jpeg", b"second-image")])
        self.assertEqual((self.run / result["path"]).read_bytes(), b"result")

    async def test_invalid_inputs_do_not_call_upstream_or_use_quota(self):
        outside = self.root / "secret.png"
        outside.write_bytes(b"secret")
        (self.run / "in/link.png").symlink_to(outside)
        (self.run / "in/file.txt").write_text("text")
        (self.run / "in/empty.png").touch()
        for images in ("in/a.png", [1], ["in/a.png"] * 17, [str(outside)],
                       ["../secret.png"], ["in/link.png"], ["in/missing.png"],
                       ["in/file.txt"], ["in/empty.png"]):
            with self.subTest(images=images), self.assertRaises(ToolError):
                await self.api.t_generate_image(self.ctx, "draw", images=images)
        self.assertEqual(self.requests, [])
        self.assertEqual(self.ctx.images, 0)

    async def test_large_input_rejected(self):
        (self.run / "in/a.png").write_bytes(b"1234")
        with patch("cgm.toolapi.FILE_MAX", 4), self.assertRaises(ToolError):
            await self.api.t_generate_image(self.ctx, "draw", images=["in/a.png"])
        self.assertEqual(self.ctx.images, 0)

    async def test_edits_share_generation_quota(self):
        self.ctx.images = 2
        with self.assertRaises(ToolError):
            await self.api.t_generate_image(self.ctx, "draw", images=["in/a.png"])
        self.assertEqual(self.requests, [])


if __name__ == "__main__":
    unittest.main()
