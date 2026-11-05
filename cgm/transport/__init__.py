"""Telegram 这一层的统一接口。Bot API（aiogram）和 UserBot（Telethon）各实现一份。

主循环、工具接口、run 都只认这里的 InMessage 和 Transport，不碰具体库。
发出去的文本一律是 Telegram HTML（由 tgfmt 生成）。
"""

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass
class InMessage:
    chat_id: int
    chat_title: str | None
    msg_id: int
    sender_id: int | None
    sender: str
    ts: int
    text: str
    reply_to: int | None = None
    file_id: str | None = None      # 非空 = 有可下载的图片/文件
    file_name: str | None = None
    directed: bool = False          # @它、回复它、叫它名字
    private: bool = False
    out: bool = False               # 这个账号自己发的（UserBot 在别的设备上手动发的也算）
    from_bot: bool = False          # 别的 bot 发的：入库但不触发 run，免得两个 bot 对刷


class SendError(Exception):
    """发送类操作失败。消息会原样返回给模型。"""


OnMessage = Callable[[InMessage], Awaitable[None]]


class Transport(Protocol):
    me_id: int
    me_name: str
    username: str                   # 不带 @，没有就是空串

    async def connect(self) -> None: ...
    async def listen(self, on_message: OnMessage, on_edit: OnMessage) -> None:
        """注册回调并一直跑下去，断线不可恢复时返回。"""
    async def close(self) -> None: ...

    async def send_text(self, chat_id: int, html: str, reply_to: int | None = None) -> int: ...
    async def edit_text(self, chat_id: int, msg_id: int, html: str) -> None: ...
    async def send_file(self, chat_id: int, path: Path, caption: str | None,
                        reply_to: int | None, photo: bool) -> int: ...
    async def react(self, chat_id: int, msg_id: int, emoji: str) -> None: ...
    async def typing(self, chat_id: int) -> None: ...
    async def mark_read(self, chat_id: int, msg_id: int) -> None: ...
    async def download(self, chat_id: int, msg_id: int, file_id: str, dest: Path,
                       max_size: int) -> bool: ...
    async def history(self, chat_id: int, limit: int) -> list[InMessage]: ...
    async def reactions(self, chat_id: int, msg_ids: list[int]) -> dict[int, "Reactions"]:
        """这些消息上大家点的表情。拿不到的 transport 返回 {}。"""
    async def profile(self, chat_id: int, *, msg_id: int | None, user_id: int | None,
                      username: str | None, avatar: Path) -> dict:
        """一个人的公开资料：名字、用户名、简介等。头像存到 avatar，存成功才在结果里带 avatar 字段。"""


@dataclass
class Reactions:
    counts: list[tuple[str, int]]            # (表情, 人数)
    by: list[tuple[int, str]]                # (谁, 表情)，Telegram 只给小群和最近几个


_NOISE = re.compile(r"https?://\S+|[@/]\w+")


def name_called(text: str, names: tuple[str, ...]) -> bool:
    """消息里有没有叫它的名字。英文名要整词出现（Chiyuki 不算 yuki），链接、@用户名、/命令 里的不算。"""
    text = _NOISE.sub(" ", text or "")
    for n in names:
        if n.isascii():
            if re.search(rf"(?<![A-Za-z0-9_]){re.escape(n)}(?![A-Za-z0-9_])", text, re.I):
                return True
        elif n in text:
            return True
    return False


REACTION_HINT = "这个 emoji 不能当表情回应。能用的比如 🎉 🔥 🤯 😢 🤗 ❤ 🙏"


def make(cfg) -> Transport:
    if cfg.transport == "user":
        from .userbot import UserBot
        return UserBot(cfg)
    from .botapi import BotAPI
    return BotAPI(cfg)
