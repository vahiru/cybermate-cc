"""UserBot（Telethon / MTProto）。能拉历史，能看到别的 bot，能标已读。

几条从旧版带过来的经验：
- api_id 必须自己去 my.telegram.org 申请。官方客户端的公开 id（2040、6 等）会被拿官方客户端的
  行为特征比对，实测两分钟就被踢下线
- 设备指纹别留 Telethon 默认值，和登录时用的保持一致
- 发消息前按字数「打一会儿字」。挡不住 spam 判定（那得靠养号），但至少不主动招惹
"""

import asyncio
import logging
import random
from pathlib import Path

from telethon import TelegramClient, events, utils
from telethon.extensions import html as tl_html
from telethon.errors import (FloodWaitError, MessageNotModifiedError, ReactionInvalidError,
                             RPCError)
from telethon.sessions import StringSession
from telethon.tl.functions.channels import GetFullChannelRequest
from telethon.tl.functions.messages import SendReactionRequest, SetTypingRequest
from telethon.tl.functions.users import GetFullUserRequest
from telethon.tl.types import Channel, ReactionEmoji, SendMessageTypingAction, User

from .. import creds
from . import REACTION_HINT, InMessage, OnMessage, Reactions, SendError, name_called

log = logging.getLogger("cgm.userbot")

MS_PER_CHAR = (60, 100)     # 打字速度，按可见字数算
MAX_TYPING = 6.0            # 最多假装打几秒

# 顺序要紧：贴纸、动图、语音这些在 Telethon 里也都是 document
_MEDIA = [("sticker", "贴纸"), ("gif", "GIF"), ("voice", "语音"), ("video_note", "视频消息"),
          ("video", "视频"), ("audio", "音频"), ("poll", "投票"), ("geo", "位置"),
          ("contact", "名片")]


def client_from(cfg, session: str = "", api_id: int = 0, api_hash: str = "") -> TelegramClient:
    return TelegramClient(
        StringSession(session), api_id or cfg.tg_api_id, api_hash or cfg.tg_api_hash,
        device_model=cfg.device_model, system_version=cfg.system_version,
        app_version=cfg.app_version, flood_sleep_threshold=60)


class UserBot:
    def __init__(self, cfg):
        self.cfg = cfg
        secret = creds.load(cfg.session_file)
        self.client = client_from(cfg, secret["session"], secret.get("api_id", 0),
                                  secret.get("api_hash", ""))
        self.me_id = 0
        self.me_name = ""
        self.username = ""
        self.flood_waits = 0

    async def connect(self) -> None:
        await self.client.connect()
        if not await self.client.is_user_authorized():
            raise SystemExit("UserBot session 已失效，重新跑 python -m cgm login")
        me = await self.client.get_me()
        self.me_id, self.me_name, self.username = me.id, utils.get_display_name(me), me.username or ""
        # StringSession 不存实体缓存，不拉一遍对话列表的话按 id 发消息会找不到群
        await self.client.get_dialogs()
        log.info("UserBot 就绪：%s @%s", me.id, me.username)

    async def close(self) -> None:
        await self.client.disconnect()

    async def listen(self, on_message: OnMessage, on_edit: OnMessage) -> None:
        async def _new(event) -> None:
            if m := await self._convert(event.message):
                await on_message(m)

        async def _edit(event) -> None:
            if m := await self._convert(event.message):
                await on_edit(m)

        self.client.add_event_handler(_new, events.NewMessage())
        self.client.add_event_handler(_edit, events.MessageEdited())
        await self.client.run_until_disconnected()

    # ---- 收 ----

    async def _convert(self, m) -> InMessage | None:
        if not (m.is_group or m.is_private):
            return None
        sender = m.sender or await m.get_sender()
        text, fid, fname = await _describe(m)
        if not text and not fid:
            return None
        chat = m.chat or await m.get_chat()
        directed = bool(m.mentioned) or name_called(m.message, self.cfg.names)
        return InMessage(
            chat_id=utils.get_peer_id(m.peer_id),
            chat_title=getattr(chat, "title", None), msg_id=m.id,
            sender_id=m.sender_id, sender=utils.get_display_name(sender) or "?" if sender else "?",
            ts=int(m.date.timestamp()), text=text, reply_to=m.reply_to_msg_id,
            file_id=fid, file_name=fname, directed=directed and not m.out,
            private=m.is_private, out=bool(m.out),
            from_bot=bool(getattr(sender, "bot", False)))

    async def history(self, chat_id: int, limit: int) -> list[InMessage]:
        """冷启动时拉一段历史，prompt 里一开始就有上下文。"""
        out = []
        async for m in self.client.iter_messages(chat_id, limit=limit):
            if msg := await self._convert(m):
                out.append(msg)
        return list(reversed(out))

    async def reactions(self, chat_id: int, msg_ids: list[int]) -> dict[int, Reactions]:
        """一次请求拉回这些消息的表情：各表情人数，加上 Telegram 愿意给的「谁点了什么」。"""
        out = {}
        for m in await self.client.get_messages(chat_id, ids=msg_ids):
            r = getattr(m, "reactions", None)
            if not r or not r.results:
                continue
            counts = [(getattr(x.reaction, "emoticon", None) or "自定义表情", x.count)
                      for x in r.results]
            by = [(utils.get_peer_id(x.peer_id), getattr(x.reaction, "emoticon", None) or "自定义表情")
                  for x in (r.recent_reactions or [])]
            out[m.id] = Reactions(counts, by)
        return out

    async def profile(self, chat_id: int, *, msg_id: int | None, user_id: int | None,
                      username: str | None, avatar: Path) -> dict:
        ent = None
        if msg_id:
            # 从消息拿发送者最可靠：带着 access_hash，不依赖实体缓存
            m = await self.client.get_messages(chat_id, ids=msg_id)
            ent = m and (m.sender or await m.get_sender())
        if ent is None and (user_id or username):
            ent = await self._call(self.client.get_entity(user_id or username))
        if ent is None:
            raise SendError("找不到这个人")
        info = {"name": utils.get_display_name(ent), "username": getattr(ent, "username", None)}
        if isinstance(ent, User):
            full = (await self._call(self.client(GetFullUserRequest(ent)))).full_user
            info |= {"bio": full.about or "", "bot": bool(ent.bot), "premium": bool(ent.premium)}
        elif isinstance(ent, Channel):
            full = (await self._call(self.client(GetFullChannelRequest(ent)))).full_chat
            info |= {"bio": full.about or "", "kind": "频道或群"}
        if await self.client.download_profile_photo(ent, file=str(avatar)):
            info["avatar"] = True
        return info

    # ---- 发 ----

    async def _pace(self, chat_id: int, html: str) -> None:
        visible = len(tl_html.parse(html)[0]) if html else 0
        await self.typing(chat_id)
        await asyncio.sleep(min(visible * random.randint(*MS_PER_CHAR) / 1000, MAX_TYPING))

    async def _call(self, coro):
        try:
            return await coro
        except FloodWaitError as e:
            # 60 秒以内的 Telethon 自己睡过去了，能到这里的都是长的
            self.flood_waits += 1
            log.warning("FloodWait %ss（累计 %d 次）", e.seconds, self.flood_waits)
            raise SendError(f"被 Telegram 限流，要等 {e.seconds} 秒") from e
        except RPCError as e:
            raise SendError(str(e)) from e

    async def send_text(self, chat_id: int, html: str, reply_to: int | None = None) -> int:
        await self._pace(chat_id, html)
        m = await self._call(self.client.send_message(chat_id, html, parse_mode="html",
                                                      reply_to=reply_to))
        return m.id

    async def edit_text(self, chat_id: int, msg_id: int, html: str) -> None:
        try:
            await self._call(self.client.edit_message(chat_id, msg_id, html, parse_mode="html"))
        except SendError as e:
            if not isinstance(e.__cause__, MessageNotModifiedError):
                raise

    async def send_file(self, chat_id: int, path: Path, caption: str | None,
                        reply_to: int | None, photo: bool) -> int:
        m = await self._call(self.client.send_file(
            chat_id, str(path), caption=caption, parse_mode="html", reply_to=reply_to,
            force_document=not photo))
        return m.id

    async def react(self, chat_id: int, msg_id: int, emoji: str) -> None:
        try:
            await self._call(self.client(SendReactionRequest(
                peer=chat_id, msg_id=msg_id, reaction=[ReactionEmoji(emoticon=emoji)])))
        except SendError as e:
            if isinstance(e.__cause__, ReactionInvalidError):
                raise SendError(REACTION_HINT) from e
            raise

    async def typing(self, chat_id: int) -> None:
        await self.client(SetTypingRequest(chat_id, SendMessageTypingAction()))

    async def mark_read(self, chat_id: int, msg_id: int) -> None:
        await self.client.send_read_acknowledge(chat_id, max_id=msg_id)

    async def download(self, chat_id: int, msg_id: int, file_id: str, dest: Path,
                       max_size: int) -> bool:
        m = await self.client.get_messages(chat_id, ids=msg_id)
        if not m or not m.file or (m.file.size or 0) > max_size:
            return False
        await m.download_media(file=str(dest))
        return True


async def _describe(m) -> tuple[str, str | None, str | None]:
    text = m.message or ""
    fid = fname = None
    for attr, tag in _MEDIA:
        if getattr(m, attr, None):
            if attr == "sticker":
                text = f"[贴纸 {m.file.emoji or ''}]".replace(" ]", "]")
            elif attr == "poll":
                p = m.poll.poll
                q = getattr(p.question, "text", p.question)
                text = f"[投票] {q}：" + " / ".join(getattr(a.text, "text", a.text)
                                                   for a in p.answers)
            else:
                text = f"[{tag}] {text}".strip()
            break
    else:
        if m.photo:
            fid, fname = "mtproto", "photo.jpg"
        elif m.document:
            fid, fname = "mtproto", m.file.name or f"file{m.file.ext or ''}"
    if m.fwd_from:
        name = m.fwd_from.from_name
        if not name and m.forward:
            src = m.forward.sender or m.forward.chat
            name = utils.get_display_name(src) if src else None
        text = f"[转发自 {name or '?'}] {text}"
    return text, fid, fname
