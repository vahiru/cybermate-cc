"""Bot API（aiogram 长轮询）。拉不到历史，看不到别的 bot 的消息。"""

from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import FSInputFile, Message, ReactionTypeEmoji, ReplyParameters

from . import REACTION_HINT, InMessage, OnMessage, SendError, name_called


def _reply(msg_id: int | None) -> ReplyParameters | None:
    return ReplyParameters(message_id=msg_id, allow_sending_without_reply=True) if msg_id else None


class BotAPI:
    def __init__(self, cfg):
        self.cfg = cfg
        self.bot = Bot(cfg.bot_token)
        self.me_id = self.bot.id
        self.me_name = ""
        self.username = ""

    async def connect(self) -> None:
        me = await self.bot.get_me()
        self.me_id, self.me_name, self.username = me.id, me.full_name, me.username or ""

    async def close(self) -> None:
        await self.bot.session.close()

    async def listen(self, on_message: OnMessage, on_edit: OnMessage) -> None:
        router = Router()

        @router.message(F.chat.type.in_({"group", "supergroup", "private"}))
        async def _msg(m: Message) -> None:
            await on_message(self._convert(m))

        @router.edited_message(F.chat.type.in_({"group", "supergroup"}))
        async def _edit(m: Message) -> None:
            await on_edit(self._convert(m))

        dp = Dispatcher()
        dp.include_router(router)
        await dp.start_polling(self.bot, allowed_updates=["message", "edited_message"],
                               handle_signals=True)

    # ---- 收 ----

    def _convert(self, m: Message) -> InMessage:
        text, fid, fname = _describe(m)
        u = m.from_user
        return InMessage(
            chat_id=m.chat.id, chat_title=m.chat.title, msg_id=m.message_id,
            sender_id=u.id if u else None,
            sender=(m.sender_chat.title if m.sender_chat else u.full_name if u else "?") or "?",
            ts=int(m.date.timestamp()), text=text,
            reply_to=m.reply_to_message.message_id if m.reply_to_message else None,
            file_id=fid, file_name=fname, directed=self._directed(m),
            private=m.chat.type == "private", from_bot=bool(u and u.is_bot))

    def _directed(self, m: Message) -> bool:
        r = m.reply_to_message
        if r and r.from_user and r.from_user.id == self.me_id:
            return True
        text = m.text or m.caption or ""
        at = f"@{self.username}".lower()
        for e in m.entities or m.caption_entities or []:
            if e.type == "mention" and e.extract_from(text).lower() == at:
                return True
            if e.type == "text_mention" and e.user and e.user.id == self.me_id:
                return True
            if e.type == "bot_command" and e.extract_from(text).lower().endswith(at):
                return True
        return name_called(text, self.cfg.names)

    # ---- 发 ----

    async def send_text(self, chat_id: int, html: str, reply_to: int | None = None) -> int:
        try:
            m = await self.bot.send_message(chat_id, html, parse_mode="HTML",
                                            reply_parameters=_reply(reply_to))
        except TelegramAPIError as e:
            raise SendError(str(e)) from e
        return m.message_id

    async def edit_text(self, chat_id: int, msg_id: int, html: str) -> None:
        try:
            await self.bot.edit_message_text(html, chat_id=chat_id, message_id=msg_id,
                                             parse_mode="HTML")
        except TelegramBadRequest as e:
            if "not modified" not in str(e):
                raise SendError(str(e)) from e
        except TelegramAPIError as e:
            raise SendError(str(e)) from e

    async def send_file(self, chat_id: int, path: Path, caption: str | None,
                        reply_to: int | None, photo: bool) -> int:
        kw = dict(caption=caption, parse_mode="HTML", reply_parameters=_reply(reply_to))
        try:
            if photo:
                m = await self.bot.send_photo(chat_id, FSInputFile(path), **kw)
            else:
                m = await self.bot.send_document(chat_id, FSInputFile(path), **kw)
        except TelegramAPIError as e:
            raise SendError(str(e)) from e
        return m.message_id

    async def react(self, chat_id: int, msg_id: int, emoji: str) -> None:
        try:
            await self.bot.set_message_reaction(chat_id, msg_id, [ReactionTypeEmoji(emoji=emoji)])
        except TelegramAPIError as e:
            raise SendError(REACTION_HINT if "REACTION_INVALID" in str(e) else str(e)) from e

    async def typing(self, chat_id: int) -> None:
        await self.bot.send_chat_action(chat_id, "typing")

    async def mark_read(self, chat_id: int, msg_id: int) -> None:
        pass  # bot 没有已读

    async def download(self, chat_id: int, msg_id: int, file_id: str, dest: Path,
                       max_size: int) -> bool:
        f = await self.bot.get_file(file_id)
        if (f.file_size or 0) > max_size:
            return False
        await self.bot.download_file(f.file_path, dest)
        return True

    async def history(self, chat_id: int, limit: int) -> list[InMessage]:
        return []  # Bot API 拉不到历史，只能从被拉进群那一刻开始攒

    async def reactions(self, chat_id: int, msg_ids: list[int]) -> dict:
        return {}  # Bot API 查不了消息上的表情

    async def profile(self, chat_id: int, *, msg_id: int | None, user_id: int | None,
                      username: str | None, avatar: Path) -> dict:
        """Bot API 只能查 bot 见过的人；按消息 id 查要靠库里记下的 user_id。"""
        target = user_id or (f"@{username.lstrip('@')}" if username else None)
        if target is None:
            raise SendError("找不到这个人")
        try:
            c = await self.bot.get_chat(target)
        except TelegramAPIError as e:
            raise SendError(f"查不到：{e}") from e
        info = {"name": c.full_name or c.title, "username": c.username, "bio": c.bio or c.description or ""}
        if c.photo:
            f = await self.bot.get_file(c.photo.big_file_id)
            await self.bot.download_file(f.file_path, avatar)
            info["avatar"] = True
        return info


def _describe(m: Message) -> tuple[str, str | None, str | None]:
    """(文本, file_id, 文件名)。只有图片和文件会下载，其余用一个方括号标签代替。"""
    text = m.text or m.caption or ""
    fid = fname = None
    if m.photo:
        fid, fname = m.photo[-1].file_id, "photo.jpg"
    elif m.document:
        fid, fname = m.document.file_id, m.document.file_name or "file"
    elif m.sticker:
        text = f"[贴纸 {m.sticker.emoji or ''}]".replace(" ]", "]")
    elif m.animation:
        text = f"[GIF] {text}".strip()
    elif m.voice:
        text = "[语音]"
    elif m.video_note:
        text = "[视频消息]"
    elif m.video:
        text = f"[视频] {text}".strip()
    elif m.audio:
        text = f"[音频 {m.audio.title or m.audio.file_name or ''}] {text}".strip()
    elif m.poll:
        text = f"[投票] {m.poll.question}：" + " / ".join(o.text for o in m.poll.options)
    elif m.location:
        text = "[位置]"
    if m.forward_origin:
        o = m.forward_origin
        src = getattr(o, "sender_user", None) or getattr(o, "chat", None)
        name = getattr(src, "full_name", None) or getattr(src, "title", None) \
            or getattr(o, "sender_user_name", None) or "?"
        text = f"[转发自 {name}] {text}"
    return text, fid, fname

