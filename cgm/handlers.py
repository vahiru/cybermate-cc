"""收到消息之后：群消息入库并触发 run；管理命令（owner 私聊或群里，群友只能 /pause）。和 transport 无关。"""

import html
import logging
import time

from .agent import Runner, day_start
from .config import Config
from .store import Store
from .transport import InMessage, Transport

log = logging.getLogger("cgm.handlers")


class Handlers:
    def __init__(self, cfg: Config, store: Store, runner: Runner, tp: Transport):
        self.cfg, self.store, self.runner, self.tp = cfg, store, runner, tp
        self.unlisted: set[int] = set()

    def ingest(self, m: InMessage) -> None:
        self.store.add_message(
            m.chat_id, m.msg_id, "你" if m.out else m.sender, m.text, sender_id=m.sender_id,
            is_self=m.out, ts=m.ts, reply_to=m.reply_to, file_id=m.file_id,
            file_name=m.file_name, directed=m.directed)

    def dm_allowed(self, user_id: int | None) -> bool:
        if self.cfg.dm == "off" or user_id is None:
            return False
        if self.cfg.dm == "all" or user_id == self.cfg.owner_id:
            return True
        return self.store.spoke_in(user_id, self.cfg.chats)

    async def on_message(self, m: InMessage) -> None:
        if m.private:
            await self.on_private(m)
            return
        if m.chat_id not in self.cfg.chats:
            if m.chat_id not in self.unlisted:
                self.unlisted.add(m.chat_id)
                log.warning("群 %s（%s）不在 CGM_CHATS 里，忽略", m.chat_id, m.chat_title)
            return
        self.store.touch_chat(m.chat_id, m.chat_title)
        # 工具发出去的消息已经入过库，这里 INSERT OR IGNORE；在别的设备上手动发的也记成「你」
        self.ingest(m)
        if m.out or m.from_bot or self.other_bot_command(m):
            return
        if await self.group_command(m):
            # 命令不交给模型：标成已读，免得下次 run 把它当成聊天
            self.store.set_last_seen(m.chat_id, self.store.max_seq(m.chat_id))
            return
        self.runner.trigger(m.chat_id, urgent=m.directed)

    async def on_private(self, m: InMessage) -> None:
        """私聊当成两个人的群：有自己的聊天记录和笔记，每条都算叫到它。"""
        if m.out:
            # UserBot 在别的设备上手动回的私信：已经在聊的对话里记成「你」
            if self.store.max_seq(m.chat_id):
                self.ingest(m)
            return
        if m.sender_id == self.cfg.owner_id and await self.command(m):
            return
        if m.from_bot or not self.dm_allowed(m.sender_id):
            if m.chat_id not in self.unlisted:
                self.unlisted.add(m.chat_id)
                log.info("私聊 %s（%s）不在允许范围（CGM_DM=%s），忽略", m.chat_id, m.sender, self.cfg.dm)
            return
        m.directed = True
        self.store.touch_chat(m.chat_id, f"私聊 · {m.sender}")
        self.ingest(m)
        self.runner.trigger(m.chat_id, urgent=True)

    async def on_edit(self, m: InMessage) -> None:
        if m.chat_id in self.cfg.chats or (m.private and self.store.max_seq(m.chat_id)):
            self.store.edit_message(m.chat_id, m.msg_id, m.text)

    async def backfill(self) -> None:
        """库里还没有这个群的消息时，拉一段历史当上下文（只有 UserBot 拉得到）。"""
        for chat_id in self.cfg.chats:
            if self.store.max_seq(chat_id):
                continue
            try:
                msgs = await self.tp.history(chat_id, self.cfg.history)
            except Exception as e:
                log.warning("拉 %s 的历史失败：%s", chat_id, e)
                continue
            for m in msgs:
                self.store.touch_chat(m.chat_id, m.chat_title)
                self.ingest(m)
            self.store.set_last_seen(chat_id, self.store.max_seq(chat_id))
            if msgs:
                log.info("群 %s 拉了 %d 条历史", chat_id, len(msgs))

    # ---- 群里的命令 ----

    def other_bot_command(self, m: InMessage) -> bool:
        """/xxx@别的bot：留在库里当上下文，但不是叫它，不触发 run。"""
        if not m.text.startswith("/"):
            return False
        target = m.text.split()[0].partition("@")[2].lower()
        return bool(target) and target != self.tp.username.lower()

    async def group_command(self, m: InMessage) -> bool:
        """/pause 群里谁都能发，只停这个群；其余命令只认 owner，结果私聊回给 owner。
        不是这些命令（比如别的 bot 的 /xxx）就返回 False，当普通消息。"""
        if not m.text.startswith("/"):
            return False
        cmd = m.text.split()[0].split("@")[0].lower()
        owner = m.sender_id == self.cfg.owner_id
        if cmd in ("/pause", "/stop") and not owner:
            cmd = "/pause"  # 群友说 stop 也当暂停：停下正在跑的，并且不再触发
        if cmd == "/pause":
            self.runner.pause(m.chat_id)
            await self.runner.stop(m.chat_id)
            log.info("群 %s 被 %s 暂停", m.chat_id, m.sender)
            await self._say(m, "已暂停，不会再说话或点表情。管理员发 /resume 恢复")
            if not owner:
                await self.notify_owner(f"{self.store.chat_title(m.chat_id)} 里 {m.sender} 把我暂停了")
            return True
        if not owner:
            return cmd in ("/resume", "/status", "/last", "/notes")  # 吞掉，不给模型看
        if cmd == "/resume":
            self.runner.resume(m.chat_id)
            await self._say(m, "回来了")
            return True
        fn = getattr(self, f"c_{cmd[1:]}", None)
        if not fn:
            return False
        await self.notify_owner(await fn(str(m.chat_id)))
        return True

    async def _say(self, m: InMessage, text: str) -> None:
        """不经过模型、直接以账号身份回一句（命令的确认）。"""
        mid = await self.tp.send_text(m.chat_id, html.escape(text, quote=False), m.msg_id)
        self.store.add_message(m.chat_id, mid, "你", text, sender_id=self.tp.me_id, is_self=True,
                               reply_to=m.msg_id)

    async def notify_owner(self, text: str) -> None:
        if self.cfg.owner_id:
            try:
                await self.tp.send_text(self.cfg.owner_id, html.escape(text, quote=False))
            except Exception as e:
                log.warning("私聊 owner 失败：%s", e)

    # ---- owner 私聊 ----

    async def reply(self, m: InMessage, text: str) -> None:
        await self.tp.send_text(m.chat_id, html.escape(text, quote=False))

    def _chat_arg(self, arg: str) -> int | None:
        if arg.lstrip("-").isdigit():
            return int(arg)
        return next(iter(self.cfg.chats)) if len(self.cfg.chats) == 1 else None

    async def command(self, m: InMessage) -> bool:
        """owner 的管理命令。不是已知命令就返回 False，当普通私信处理。"""
        if not m.text.startswith("/"):
            return False
        cmd, _, arg = m.text.partition(" ")
        cmd, arg = cmd.split("@")[0].lower(), arg.strip()
        fn = getattr(self, f"c_{cmd[1:]}", None)
        if not fn:
            return False
        await self.reply(m, await fn(arg))
        return True

    async def c_status(self, arg: str) -> str:
        runner, store = self.runner, self.store
        lines = [f"{self.cfg.transport} · " + ("全部暂停（/resume 恢复）" if runner.paused else "在线")]
        for cid in sorted(self.cfg.chats | runner.running.keys()):
            r = runner.running.get(cid)
            state = f"正在跑 {r.run_id}（{time.time() - r.started:.0f}s）" if r else "空闲"
            if runner.is_paused(cid) and not runner.paused:
                state = "已暂停"
            lines.append(f"{store.chat_title(cid)}（{cid}）：{state}")
        runs = store.runs_since(day_start())
        cost = sum(r["cost"] or 0 for r in runs)
        spoke = sum(1 for r in runs if r["sends"])
        read = sum(r["cache_read"] or 0 for r in runs)
        total = read + sum((r["cache_write"] or 0) + (r["input"] or 0) for r in runs)
        hit = f"{read / total:.0%}" if total else "-"
        lines.append(f"今天 {len(runs)} 次 run，开口 {spoke} 次，花费 ${cost:.3f}，缓存命中 {hit}")
        if fw := getattr(self.tp, "flood_waits", 0):
            lines.append(f"FloodWait 累计 {fw} 次")
        return "\n".join(lines)

    async def c_stop(self, arg: str) -> str:
        chat = int(arg) if arg.lstrip("-").isdigit() else None
        n = await self.runner.stop(chat)
        return f"杀掉了 {n} 个 run" if n else "没有在跑的 run"

    async def c_pause(self, arg: str) -> str:
        """/pause 全部暂停；/pause <群id> 只停一个群。都会顺手杀掉正在跑的。"""
        chat = int(arg) if arg.lstrip("-").isdigit() else None
        self.runner.pause(chat)
        n = await self.runner.stop(chat)
        what = self.store.chat_title(chat) if chat else "全部"
        return f"{what}暂停了，杀掉了 {n} 个在跑的 run。消息照常入库，/resume 恢复"

    async def c_resume(self, arg: str) -> str:
        """/resume 全部恢复（包括群友在群里暂停的）；/resume <群id> 只恢复一个群。"""
        chat = int(arg) if arg.lstrip("-").isdigit() else None
        self.runner.resume(chat)
        what = self.store.chat_title(chat) if chat else "全部"
        return f"{what}恢复了。暂停期间的消息只当历史看，不补跑"

    async def c_last(self, arg: str) -> str:
        out = []
        for r in self.store.last_runs(10):
            t = time.strftime("%m-%d %H:%M", time.localtime(r["started"]))
            s = (f"{t} {self.store.chat_title(r['chat_id'])} 新{r['n_new']} 轮{r['turns']} "
                 f"{r['duration']}s ${r['cost'] or 0:.4f} 发{r['sends']}")
            if r["error"]:
                s += f" ✗ {r['error'][:40]}"
            out.append(s)
        return "\n".join(out) or "还没有 run"

    async def c_notes(self, arg: str) -> str:
        chat = self._chat_arg(arg)
        if chat is None:
            return "有多个群，用 /notes <chat_id>"
        p = self.cfg.groups / str(chat) / "notes.md"
        body = p.read_text().strip() if p.exists() else ""
        return body[:4000] or "（空）"
