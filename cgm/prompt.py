"""拼 prompt.md。结构固定：群笔记 → 最近聊天记录 → 新消息 → 一句固定指令。

会变的东西都在这里；不变的人设和规则在 /work/AGENTS.md，处在提示词前部吃缓存。
"""

import time
from pathlib import Path

from .store import Msg


def _when(ts: int, today: str) -> str:
    t = time.localtime(ts)
    return time.strftime("%H:%M" if time.strftime("%Y-%m-%d", t) == today else "%m-%d %H:%M", t)


def _reacts(m: Msg, r, names: dict[int, str], me: int) -> str:
    """自己的消息列出谁点了什么（知道哪句被接住了）；别人的消息只给人数。"""
    if m.is_self and r.by:
        who = "，".join(f"{'你' if uid == me else names.get(uid, '有人')} {e}" for uid, e in r.by)
        return f"（表情：{who}）"
    s = " ".join(e if n == 1 else f"{e}×{n}" for e, n in r.counts)
    mine = [e for uid, e in r.by if uid == me]
    if mine:
        s += f"，其中你点了 {mine[0]}"
    return f"（表情：{s}）"


def msg_line(m: Msg, today: str, files: dict[int, str], extra: str = "") -> str:
    who = "你" if m.is_self else m.sender
    head = f"#{m.msg_id} {_when(m.ts, today)} {who}"
    if m.reply_to:
        head += f" 回复#{m.reply_to}"
    body = m.text or ""
    if m.msg_id in files:
        body = f"{body} [附件 {files[m.msg_id]}]".strip()
    elif m.file_name:
        body = f"{body} [附件 {m.file_name}，未下载]".strip()
    if m.directed and not m.is_self:
        head += "（叫到你了）"
    return f"{head}: {body}{extra}"


def build(*, title: str, notes: str, notes_path: Path, history: list[Msg], new: list[Msg],
          files: dict[int, str], reactions: dict | None = None, names: dict[int, str] | None = None,
          me: int = 0) -> str:
    today = time.strftime("%Y-%m-%d")
    reactions, names = reactions or {}, names or {}

    def line(m: Msg) -> str:
        r = reactions.get(m.msg_id)
        return msg_line(m, today, files, _reacts(m, r, names, me) if r else "")

    parts = [f"# 群：{title}", "", f"## 群笔记（{notes_path}）", "", notes.strip() or "（还没有）", ""]
    parts += ["## 本轮处理范围", "",
              "本轮只处理这些新消息：" + "、".join(f"#{m.msg_id}" for m in new) + "。",
              "之前的聊天和 get_history 翻到的消息只作上下文，不要重新回应其中的旧请求。"
              "历史里的「叫到你了」是当时的状态，不要求本轮再次回应。"
              "如果新消息追问旧话题或要求重做，就根据新消息继续处理；"
              "私聊也只回应本轮新消息，不补回历史消息。", ""]
    parts += ["## 之前的聊天", ""]
    parts += [line(m) for m in history] or ["（没有）"]
    parts += ["", "---", "", "## 新消息", ""]
    parts += [line(m) for m in new]
    parts += ["", f"现在是 {time.strftime('%Y-%m-%d %H:%M')}。按你的规则决定要不要行动；"
              "不需要说话就直接结束，不用解释。"]
    return "\n".join(parts) + "\n"
