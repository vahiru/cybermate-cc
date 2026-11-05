"""只听 127.0.0.1 的小 HTTP 接口。MCP server 把工具调用转发到这里，由主进程真正调 Telegram。

每次 run 注册一个一次性 token，绑定到那次 run 的群和目录；run 结束 token 作废。
"""

import asyncio
import base64
import logging
import os
import re
import secrets
import time
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path

import aiohttp
from aiohttp import web

from . import dongpo, prompt, tgfmt
from .config import _emoji
from .store import Store
from .transport import SendError, Transport

log = logging.getLogger("cgm.tools")

PHOTO_EXT = {".jpg", ".jpeg", ".png", ".webp"}
IMAGE_MIME = {".jpg": "image/jpeg", ".jpeg": "image/jpeg",
              ".png": "image/png", ".webp": "image/webp"}
PHOTO_MAX = 10 * 1024 * 1024
FILE_MAX = 50 * 1024 * 1024
IMAGES_PER_RUN = 2
REACTIONS_PER_RUN = 1
PROFILES_PER_RUN = 3
HISTORY_PAGES_PER_RUN = 3
HISTORY_PAGE = 100


class ToolError(Exception):
    pass


class DongpoReviewRequired(ToolError):
    code = "dongpo_review_required"


@dataclass
class RunCtx:
    run_id: str
    chat_id: int
    run_dir: Path
    max_sends: int
    token: str = field(default_factory=lambda: secrets.token_urlsafe(24))
    sends: int = 0
    images: int = 0
    reacts: int = 0
    profiles: int = 0
    history_pages: int = 0
    # 本轮负责的新消息，仅记录归属，不限制工具的 reply_to 或编辑对象。
    message_ids: tuple[int, ...] = ()
    dongpo_flagged: bool = False
    review_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    @property
    def out(self) -> Path:
        return self.run_dir / "out"


class ToolAPI:
    def __init__(self, tp: Transport, store: Store, port: int, *, llm_base_url: str = "",
                 llm_key: str = "", image_model: str = "",
                 reaction_blacklist: frozenset[str] = frozenset(), not_dongpo: bool = True):
        self.tp, self.store, self.port = tp, store, port
        self.reaction_blacklist = reaction_blacklist
        self.not_dongpo = not_dongpo
        self.llm_base_url, self.llm_key, self.image_model = llm_base_url, llm_key, image_model
        self.runs: dict[str, RunCtx] = {}

    def register(self, ctx: RunCtx) -> RunCtx:
        self.runs[ctx.token] = ctx
        return ctx

    def unregister(self, ctx: RunCtx) -> None:
        self.runs.pop(ctx.token, None)

    async def start(self) -> None:
        # 检查文件存在即可；没有命中关键词时仍不加载权重。
        if self.not_dongpo and not dongpo.MODEL_PATH.is_file():
            raise RuntimeError(dongpo.MODEL_SETUP)
        app = web.Application()
        app.router.add_post("/tool/{name}", self._handle)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", self.port).start()

    async def _handle(self, req: web.Request) -> web.Response:
        ctx = self.runs.get(req.headers.get("X-Run-Token", ""))
        if not ctx:
            return web.json_response({"ok": False, "error": "run 已结束"}, status=403)
        name = req.match_info["name"]
        fn = getattr(self, f"t_{name}", None)
        if not fn:
            return web.json_response({"ok": False, "error": f"没有 {name} 这个工具"}, status=404)
        args = await req.json()
        try:
            result = await fn(ctx, **args)
            log.info("run=%s tool=%s args=%s -> %s", ctx.run_id, name, _brief(args), result)
            return web.json_response({"ok": True, "result": result})
        except (ToolError, SendError, TypeError) as e:
            log.warning("run=%s tool=%s args=%s failed: %s", ctx.run_id, name, _brief(args), e)
            body = {"ok": False, "error": str(e)}
            if isinstance(e, DongpoReviewRequired):
                body["code"] = e.code
            return web.json_response(body)

    # ---- 工具 ----

    async def _review_text(self, ctx: RunCtx, text: str) -> None:
        if not self.not_dongpo:
            return
        async with ctx.review_lock:
            if ctx.dongpo_flagged:
                return
            # 首次加载和推理放在线程里，避免阻塞其他群的 HTTP 工具请求。
            try:
                detected = await asyncio.to_thread(dongpo.detect, text)
            except (OSError, ValueError, RuntimeError) as e:
                log.exception("run=%s 东坡检测失败", ctx.run_id)
                raise ToolError(f"草稿尚未发送，东坡检测失败：{e}") from e
            if not detected:
                return
            ctx.dongpo_flagged = True
            raise DongpoReviewRequired(
                "草稿尚未发送，可能包含 GPT 式防御性表达：预设对方提出了不存在的反驳，"
                "并追加「仅凭这些还不能……」「但这不代表……」一类说明。"
                "请对照上下文，只删除凭空预设的辩驳和不必要的免责声明，保留有效信息、"
                "合理限定和有依据的纠正，再自然地改写并重试原工具。"
                "不要只替换关键词来规避检测，也不要删掉有信息量的整条回复。"
                "如果整句没有实质内容，可以直接结束，或用 react 点一个允许的表情。"
                "这次退回不占发言额度。")

    def _take_send(self, ctx: RunCtx) -> None:
        if ctx.sends >= ctx.max_sends:
            raise ToolError(f"这次已经发了 {ctx.sends} 条，不能再发了。把要说的合并进已发的消息（edit_message）。")
        ctx.sends += 1

    def _record_self(self, ctx: RunCtx, msg_id: int, text: str, reply_to: int | None) -> None:
        self.store.add_message(ctx.chat_id, msg_id, "你", text, sender_id=self.tp.me_id,
                               is_self=True, reply_to=reply_to)

    async def _send_chunks(self, ctx: RunCtx, chunks: list[str], text: str,
                           reply_to: int | None) -> list[int]:
        ids = []
        for i, chunk in enumerate(chunks):
            mid = await self.tp.send_text(ctx.chat_id, chunk, reply_to if i == 0 else None)
            ids.append(mid)
            # 库里只存第一段原文，后续段落存成指向它的占位，免得历史里重复一遍
            self._record_self(ctx, mid, text if i == 0 else f"（接上条，第 {i + 1} 段）",
                              reply_to if i == 0 else ids[0])
        return ids

    async def t_send_message(self, ctx: RunCtx, text: str, reply_to: int | None = None) -> dict:
        if not text.strip():
            raise ToolError("text 不能为空")
        await self._review_text(ctx, text)
        self._take_send(ctx)
        ids = await self._send_chunks(ctx, tgfmt.render(text), text, reply_to)
        return {"message_id": ids[0], "parts": len(ids)}

    async def t_edit_message(self, ctx: RunCtx, message_id: int, text: str) -> dict:
        old = self.store.get_message(ctx.chat_id, message_id)
        if not old or not old.is_self:
            raise ToolError(f"#{message_id} 不是你发的消息，只能改自己的")
        await self._review_text(ctx, text)
        chunks = tgfmt.render(text)
        await self.tp.edit_text(ctx.chat_id, message_id, chunks[0])
        self.store.edit_message(ctx.chat_id, message_id, text)
        # 改出来比 4096 长：多出来的部分接着发，不占发言额度
        extra = await self._send_chunks(ctx, chunks[1:], "（接上条）", message_id) if chunks[1:] else []
        return {"message_id": message_id, "parts": 1 + len(extra)}

    async def t_send_file(self, ctx: RunCtx, path: str, caption: str | None = None,
                          reply_to: int | None = None) -> dict:
        p = Path(path) if os.path.isabs(path) else ctx.run_dir / path
        p = p.resolve()
        if not p.is_relative_to(ctx.out.resolve()):
            raise ToolError("只能发本次 run 的 out/ 目录下的文件")
        if not p.is_file():
            raise ToolError(f"文件不存在：{p.name}")
        size = p.stat().st_size
        if size > FILE_MAX:
            raise ToolError("文件超过 50MB，发不出去")
        if caption and caption.strip():
            await self._review_text(ctx, caption)
        self._take_send(ctx)
        cap = tgfmt.render(caption, tgfmt.CAPTION_LIMIT)[0] if caption and caption.strip() else None
        photo = p.suffix.lower() in PHOTO_EXT and size <= PHOTO_MAX
        mid = await self.tp.send_file(ctx.chat_id, p, cap, reply_to, photo)
        self._record_self(ctx, mid, f"[文件 {p.name}] {caption or ''}".strip(), reply_to)
        return {"message_id": mid}

    async def t_generate_image(self, ctx: RunCtx, prompt: str, filename: str = "image.png",
                               images: list[str] | None = None) -> dict:
        """调 Floway 的生图接口，图存进 out/。key 在主进程里，agent 的 shell 碰不到。"""
        if not self.image_model:
            raise ToolError("生图没开")
        if ctx.images >= IMAGES_PER_RUN:
            raise ToolError(f"这次已经生成了 {ctx.images} 张，不能再生成了")
        if images is not None and (not isinstance(images, list)
                                   or any(not isinstance(p, str) for p in images)):
            raise ToolError("images 要填图片路径列表")
        if images and len(images) > 16:
            raise ToolError("一次最多传入 16 张参考图")
        sources = []
        roots = [(ctx.run_dir / folder).resolve() for folder in ("in", "out")]
        for source in images or []:
            p = Path(source)
            p = (p if p.is_absolute() else ctx.run_dir / p).resolve()
            if not any(p.is_relative_to(root) for root in roots):
                raise ToolError("参考图只能用本次 run 的 in/ 或 out/ 目录下的文件")
            if not p.is_file():
                raise ToolError(f"参考图不存在：{p.name}")
            if p.suffix.lower() not in IMAGE_MIME:
                raise ToolError("参考图只支持 PNG、JPEG、WebP")
            if not 0 < p.stat().st_size < FILE_MAX:
                raise ToolError("参考图不能为空，且每张须小于 50MB")
            sources.append(p)
        name = re.sub(r"[^\w.-]", "_", Path(filename).name) or "image.png"
        if not name.lower().endswith(".png"):
            name += ".png"
        ctx.images += 1
        with ExitStack() as stack:
            endpoint = "generations"
            payload = {"json": {"model": self.image_model, "prompt": prompt, "n": 1}}
            if sources:
                endpoint = "edits"
                form = aiohttp.FormData()
                for key, value in (("model", self.image_model), ("prompt", prompt), ("n", "1")):
                    form.add_field(key, value)
                for p in sources:
                    form.add_field("image[]", stack.enter_context(p.open("rb")),
                                   filename=p.name, content_type=IMAGE_MIME[p.suffix.lower()])
                payload = {"data": form}
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=240)) as s:
                async with s.post(f"{self.llm_base_url}/images/{endpoint}",
                                  headers={"Authorization": f"Bearer {self.llm_key}"},
                                  **payload) as r:
                    body = await r.json(content_type=None)
        if r.status != 200 or not body.get("data"):
            log.warning("run=%s 生图失败 %s: %s", ctx.run_id, r.status, str(body)[:300])
            raise ToolError("生图失败了，可能是内容不被允许或者服务出错")
        path = ctx.out / name
        path.write_bytes(base64.b64decode(body["data"][0]["b64_json"]))
        return {"path": f"out/{name}"}

    async def t_get_profile(self, ctx: RunCtx, message_id: int | None = None,
                            username: str | None = None) -> dict:
        """查一个人的公开资料，头像存进 in/ 供 view_image 看。"""
        if not message_id and not username:
            raise ToolError("给一个 message_id（那人发的任意一条消息）或者 username")
        if ctx.profiles >= PROFILES_PER_RUN:
            raise ToolError(f"这次已经查了 {ctx.profiles} 个人了")
        user_id = None
        if message_id:
            m = self.store.get_message(ctx.chat_id, message_id)
            if not m:
                raise ToolError(f"没有 #{message_id} 这条消息")
            if m.is_self:
                raise ToolError("这是你自己发的")
            user_id = m.sender_id
        ctx.profiles += 1
        username = username.lstrip("@") if username else None
        key = user_id or re.sub(r"\W", "_", username or "")
        avatar = ctx.run_dir / "in" / f"avatar_{key}.jpg"
        info = await self.tp.profile(ctx.chat_id, msg_id=message_id, user_id=user_id,
                                     username=username,
                                     avatar=avatar)
        if info.pop("avatar", False):
            info["avatar"] = f"in/{avatar.name}"
        return info

    async def t_get_history(self, ctx: RunCtx, before_id: int) -> dict:
        """往前翻库里更早的聊天记录，格式和 prompt 里一样。只读本地库，不碰 Telegram。"""
        if ctx.history_pages >= HISTORY_PAGES_PER_RUN:
            raise ToolError(f"这次已经翻了 {ctx.history_pages} 页了，就按看到的来")
        m = self.store.get_message(ctx.chat_id, before_id)
        if not m:
            raise ToolError(f"没有 #{before_id} 这条消息")
        ctx.history_pages += 1
        msgs = self.store.history(ctx.chat_id, m.seq, HISTORY_PAGE)
        if not msgs:
            return {"messages": "（库里没有更早的了）", "more": False}
        today = time.strftime("%Y-%m-%d")
        more = bool(self.store.history(ctx.chat_id, msgs[0].seq, 1))
        return {"messages": "\n".join(prompt.msg_line(x, today, {}) for x in msgs), "more": more}

    async def t_react(self, ctx: RunCtx, message_id: int, emoji: str) -> dict:
        if _emoji(emoji) in self.reaction_blacklist:
            raise ToolError(f"{emoji} 不许用，换一个，或者不点")
        if ctx.reacts >= REACTIONS_PER_RUN:
            raise ToolError("这次已经点过表情了，一次只点一个")
        await self.tp.react(ctx.chat_id, message_id, emoji)
        ctx.reacts += 1
        return {"message_id": message_id, "emoji": emoji}


def _brief(args: dict) -> dict:
    return {k: (v[:60] + "…" if isinstance(v, str) and len(v) > 60 else v) for k, v in args.items()}
