"""每批新消息运行一次 agent CLI。同群串行，消息绑定到负责它的 run task。"""

import asyncio
import json
import logging
import os
import secrets
import shutil
import signal
import subprocess
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from . import prompt
from .config import Config
from .store import Msg, Store
from .toolapi import RunCtx, ToolAPI
from .transport import Transport

log = logging.getLogger("cgm.agent")

DOWNLOAD_MAX = 20 * 1024 * 1024   # Bot API getFile 的上限，UserBot 也沿用
# 传给 codex 的环境变量白名单。TELEGRAM_BOT_TOKEN、CGM_PASSPHRASE 不在里面。
PASS_ENV = ("LLM_API_KEY", "TZ", "LANG", "LC_ALL")


@dataclass
class Running:
    run_id: str
    proc: asyncio.subprocess.Process | None
    started: float


class Runner:
    def __init__(self, cfg: Config, store: Store, tp: Transport | None, api: ToolAPI | None,
                 notify: Callable[[str], Awaitable[None]]):
        self.cfg, self.store, self.tp, self.api, self.notify = cfg, store, tp, api, notify
        self.loops: dict[int, asyncio.Task] = {}
        self.message_loops: dict[tuple[int, int], asyncio.Task] = {}
        self.urgent: dict[int, asyncio.Event] = {}
        self.running: dict[int, Running] = {}
        self._last_error_alert = 0.0

    # ---- 暂停：全局（owner 私聊 /pause）和单群（群里任何人 /pause） ----

    @property
    def paused(self) -> bool:
        return self.store.get("paused") == "1"

    def is_paused(self, chat_id: int) -> bool:
        return self.paused or self.store.get(f"paused:{chat_id}") == "1"

    def pause(self, chat_id: int | None = None) -> None:
        self.store.put(f"paused:{chat_id}" if chat_id else "paused", "1")

    def resume(self, chat_id: int | None = None) -> None:
        """不带群 id 就全部恢复（全局和每个群）。暂停期间的消息只当历史看，不补跑。"""
        chats = [chat_id] if chat_id else self.store.chat_ids()
        if not chat_id:
            self.store.put("paused", "0")
        for cid in chats:
            self.store.put(f"paused:{cid}", "0")
            self.store.set_last_seen(cid, self.store.max_seq(cid))

    # ---- 排队 ----

    def trigger(self, chat_id: int, urgent: bool = False) -> None:
        """有新消息。没在跑就起一个循环；在跑的话，循环结束前会自己再看一眼。
        urgent（叫到它了）的消息立刻处理，其余攒 batch_seconds 秒再一起看。"""
        if self.is_paused(chat_id):
            return
        ev = self.urgent.setdefault(chat_id, asyncio.Event())
        if urgent:
            ev.set()
        t = self.loops.get(chat_id)
        if t is None or t.done():
            self.loops[chat_id] = asyncio.create_task(self._loop(chat_id))

    async def _loop(self, chat_id: int) -> None:
        ev = self.urgent.setdefault(chat_id, asyncio.Event())
        while not self.is_paused(chat_id):
            new = self.store.unseen(chat_id)
            if not new:
                return
            if not any(m.directed for m in new) and self.cfg.batch_seconds:
                # 没人叫它：从最早那条新消息起攒一会儿，热闹的时候一次看一批；中途被叫到就提前醒
                wait = new[0].ts + self.cfg.batch_seconds - time.time()
                if wait > 0:
                    try:
                        await asyncio.wait_for(ev.wait(), wait)
                    except asyncio.TimeoutError:
                        pass
                    continue
            ev.clear()
            # 先标记已读：跑的这段时间里进来的消息 seq 更大，下一圈正好接上
            self.store.set_last_seen(chat_id, new[-1].seq)
            try:
                await self.run_chat(chat_id, new)
            except Exception:
                log.exception("chat=%s run 出错", chat_id)
                await asyncio.sleep(5)

    async def stop(self, chat_id: int | None = None) -> int:
        n = 0
        for cid, r in list(self.running.items()):
            if chat_id is None or cid == chat_id:
                if r.proc:
                    await self._kill(r.proc)
                    n += 1
        return n

    # ---- 一次 run ----

    def _new_run_dir(self, chat_id: int) -> tuple[str, Path]:
        run_id = time.strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(2)
        run_dir = self.cfg.groups / str(chat_id) / "runs" / run_id
        (run_dir / "in").mkdir(parents=True)
        (run_dir / "out").mkdir()
        return run_id, run_dir

    async def _download(self, msgs: list[Msg], in_dir: Path) -> dict[int, str]:
        files: dict[int, str] = {}
        if not self.tp:
            return files
        for m in msgs:
            if not m.file_id or m.msg_id in files:
                continue
            name = f"{m.msg_id}_{_safe(m.file_name or 'file')}"
            try:
                if await self.tp.download(m.chat_id, m.msg_id, m.file_id, in_dir / name,
                                          DOWNLOAD_MAX):
                    files[m.msg_id] = f"in/{name}"
            except Exception as e:
                log.warning("下载 #%s 的附件失败: %s", m.msg_id, e)
        return files

    async def run_chat(self, chat_id: int, new: list[Msg]) -> None:
        # 一批消息共用一个 handle；先登记再 await，重复调度不会启动第二个 run。
        new = [m for m in new if (chat_id, m.msg_id) not in self.message_loops]
        if not new:
            return
        keys = {(chat_id, m.msg_id) for m in new}
        task = asyncio.create_task(self._run_chat(chat_id, new))
        for key in keys:
            self.message_loops[key] = task
        try:
            await task
        finally:
            for key in keys:
                if self.message_loops.get(key) is task:
                    del self.message_loops[key]

    async def _run_chat(self, chat_id: int, new: list[Msg]) -> None:
        cfg = self.cfg
        run_id, run_dir = self._new_run_dir(chat_id)
        group_dir = run_dir.parent.parent
        notes_path = group_dir / "notes.md"
        if not notes_path.exists():
            notes_path.touch()

        history = self.store.history(chat_id, new[0].seq, cfg.history)
        if self.tp:
            try:
                await self.tp.mark_read(chat_id, new[-1].msg_id)
            except Exception as e:
                log.debug("标已读失败: %s", e)
        # 新消息的附件，加上新消息回复的那条的附件（「这张图什么意思」常常是回复旧图）
        targets = list(new)
        for m in new:
            if m.reply_to and (r := self.store.get_message(chat_id, m.reply_to)) and r.file_id:
                targets.append(r)
        files = await self._download(targets, run_dir / "in")

        reactions = {}
        if self.tp:
            try:
                ids = [m.msg_id for m in history + new]
                reactions = await asyncio.wait_for(self.tp.reactions(chat_id, ids), 10)
            except Exception as e:
                log.debug("拉表情失败: %s", e)
        text = prompt.build(title=self.store.chat_title(chat_id), notes=notes_path.read_text(),
                            notes_path=notes_path, history=history, new=new, files=files,
                            reactions=reactions, names=self.store.names(chat_id),
                            me=self.tp.me_id if self.tp else 0)
        directed = any(m.directed for m in new)
        await self.execute(chat_id, run_id, run_dir, text, n_new=len(new), typing=directed,
                           message_ids=tuple(m.msg_id for m in new))

    async def execute(self, chat_id: int | None, run_id: str, run_dir: Path, text: str, *,
                      n_new: int = 0, typing: bool = False,
                      message_ids: tuple[int, ...] = ()) -> dict:
        cfg = self.cfg
        (run_dir / "prompt.md").write_text(text)

        ctx = None
        if chat_id is not None and self.api:
            ctx = self.api.register(RunCtx(run_id, chat_id, run_dir, cfg.max_sends,
                                           message_ids=message_ids))

        started = time.time()
        typer = asyncio.create_task(self._typing(chat_id)) if typing and chat_id is not None else None
        timed_out, out, err, proc = False, b"", b"", None
        try:
            cmd = self._command(run_dir, ctx)
            proc = await self._spawn(cmd, run_dir)
            if chat_id is not None:
                self.running[chat_id] = Running(run_id, proc, started)
            try:
                data = b"" if self.backend == "antigravity" else text.encode()
                out, err = await asyncio.wait_for(proc.communicate(data), cfg.run_timeout)
            except asyncio.TimeoutError:
                timed_out = True
                await self._kill(proc)
                out, err = await proc.communicate()
        except asyncio.CancelledError:
            if proc:
                await self._kill(proc)
                await proc.communicate()
            raise
        finally:
            if typer:
                typer.cancel()
            if ctx:
                self.api.unregister(ctx)
            if chat_id is not None:
                self.running.pop(chat_id, None)

        (run_dir / "events.jsonl").write_bytes(out)
        if err:
            (run_dir / "stderr.txt").write_bytes(err)
        res = self._parse(out)
        if not res["error"] and proc.returncode not in (0, None):
            res["error"] = (err.decode(errors="replace").strip()[-500:] or f"exit {proc.returncode}")
        if not res["error"] and not res["completed"]:
            res["error"] = "agent 未返回完成事件"

        u = res["usage"]
        row = {
            "run_id": run_id, "chat_id": chat_id or 0, "started": started, "n_new": n_new,
            "turns": res["steps"], "duration": round(time.time() - started, 1),
            "cost": self._cost(u), "input": u["input"], "cache_read": u["cache_read"],
            "cache_write": u["cache_write"], "output": u["output"],
            "sends": ctx.sends if ctx else 0, "timed_out": int(timed_out),
            "error": "timeout" if timed_out else res["error"],
        }
        self.store.record_run(row)
        log.info("run %s", json.dumps(row, ensure_ascii=False))
        await self._after_run(row)
        return res

    @property
    def backend(self) -> str:
        return getattr(self.cfg, "backend", "codex")

    def _parse(self, out: bytes) -> dict:
        return parse_antigravity_events(out) if self.backend == "antigravity" else parse_events(out)

    def _command(self, run_dir: Path, ctx: RunCtx | None = None) -> list[str]:
        cfg = self.cfg
        mcp = Path(__file__).with_name("mcp_telegram.py").resolve()
        if self.backend == "antigravity":
            agents = run_dir / ".agents"
            agents.mkdir(exist_ok=True)
            # 每个 run 自己的配置，避免并发群互相覆盖 MCP token 或会话。
            rules = (cfg.work / "AGENTS.md").read_text()
            rules = rules.replace("view_image", "view_file")
            rules += ("\n\nTelegram 操作只使用 telegram MCP server 的工具（通过 call_mcp_tool 调用）。"
                      "内置 send_message 是 agent 间通信，不能用来回复 Telegram。\n")
            (agents / "AGENTS.md").write_text(rules)
            servers = {}
            if ctx:
                servers["telegram"] = {
                    "command": cfg.mcp_python, "args": ["-I", str(mcp)],
                    "env": {"CGM_API": f"http://127.0.0.1:{cfg.api_port}",
                            "CGM_RUN_TOKEN": ctx.token},
                    "timeoutSeconds": 300,
                }
            (agents / "mcp_config.json").write_text(json.dumps({"mcpServers": servers}))
            # agy 不接受文本 stdin。读 prompt.md 避免长聊天超过系统 argv 长度限制。
            cmd = [cfg.antigravity_bin, "--output-format", "stream-json",
                   "--dangerously-skip-permissions", "--disable-slash-commands",
                   "--print=请先读取当前目录的 prompt.md，然后完成其中的本轮任务。"]
            if cfg.model:
                cmd += ["--model", cfg.model]
            return cmd
        cmd = self._codex_cmd()
        if ctx:
            cmd += ["-c", f"mcp_servers.telegram.command={_toml(cfg.mcp_python)}",
                    "-c", f"mcp_servers.telegram.args=[\"-I\", {_toml(str(mcp))}]",
                    "-c", "mcp_servers.telegram.env={CGM_API=%s, CGM_RUN_TOKEN=%s}" % (
                        _toml(f"http://127.0.0.1:{cfg.api_port}"), _toml(ctx.token)),
                    "-c", "mcp_servers.telegram.tool_timeout_sec=300"]
        return cmd + ["-"]

    def _codex_cmd(self) -> list[str]:
        """权限全放开：隔离靠容器和单独的 agent 用户，不靠 codex 自己的沙箱。
        codex 的沙箱在无特权容器里起不来，workspace-write 还默认断网，查资料就废了。"""
        cfg = self.cfg
        return [cfg.codex_bin, "exec", "--json", "--ephemeral", "--skip-git-repo-check",
                "--dangerously-bypass-approvals-and-sandbox", "-m", cfg.model,
                "-c", f"model_providers.llm.base_url={_toml(cfg.llm_base_url)}"]

    def _cost(self, u: dict) -> float:
        """按 CGM_PRICE 估算（美元/百万 token：输入、输出、缓存读、缓存写）。codex 不报花费。"""
        p_in, p_out, p_cr, p_cw = self.cfg.price
        return round((u["input"] * p_in + u["output"] * p_out + u["cache_read"] * p_cr
                      + u["cache_write"] * p_cw) / 1e6, 6)

    async def _spawn(self, cmd: list[str], cwd: Path) -> asyncio.subprocess.Process:
        keys = ("TZ", "LANG", "LC_ALL", "DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR") \
            if self.backend == "antigravity" else PASS_ENV
        env = {k: os.environ[k] for k in keys if k in os.environ}
        if self.backend == "codex":
            env["CODEX_HOME"] = str(self.cfg.codex_home)
        if self.cfg.agent_user:
            # 换成 agent 用户跑：它读不到主进程的 /proc/<pid>/environ，拿不到 bot token 或 session 口令
            keep = ",".join(sorted(env))
            cmd = ["sudo", "-n", "-H", "-u", self.cfg.agent_user, f"--preserve-env={keep}",
                   "/usr/local/bin/agent-run", *cmd]
            env["PATH"] = os.environ.get("PATH", "/usr/bin:/bin")
        else:
            env.update({k: os.environ[k] for k in ("PATH", "HOME") if k in os.environ})
        return await asyncio.create_subprocess_exec(
            *cmd, cwd=cwd, env=env, start_new_session=True,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE)

    async def _kill(self, proc: asyncio.subprocess.Process) -> None:
        """杀掉整个会话：codex 本身、它起的 shell、python、MCP server。"""
        if proc.returncode is not None:
            return
        if self.cfg.agent_user:
            # sudo 是 root 的，杀不动；以 agent 身份按会话号杀掉它底下所有进程，sudo 随之退出
            k = await asyncio.create_subprocess_exec(
                "sudo", "-n", "-u", self.cfg.agent_user, "pkill", "-KILL", "-s", str(proc.pid),
                stderr=asyncio.subprocess.DEVNULL)
            await k.wait()
        else:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    async def _typing(self, chat_id: int) -> None:
        while self.tp:
            try:
                await self.tp.typing(chat_id)
            except Exception:
                pass
            await asyncio.sleep(4.5)

    async def _after_run(self, row: dict) -> None:
        err = row["error"]
        if err and err != "timeout" and time.time() - self._last_error_alert > 600:
            self._last_error_alert = time.time()
            await self.notify(f"run {row['run_id']} 出错：\n{err[:300]}")
        day = time.strftime("%Y-%m-%d")
        if self.store.get("cost_alert_day") != day:
            spent = sum(r["cost"] or 0 for r in self.store.runs_since(day_start()))
            if spent > self.cfg.cost_alert_usd:
                self.store.put("cost_alert_day", day)
                await self.notify(f"今天已经花了 ${spent:.2f}，超过提醒线 ${self.cfg.cost_alert_usd:.2f}。"
                                  "不会自动停，需要的话 /pause。")

    # ---- 启动自检、清理 ----

    async def selfcheck(self) -> str | None:
        """跑一次最小的 agent CLI。返回 None 表示正常，否则是失败原因。"""
        tmp = self.cfg.groups / "_selfcheck"
        shutil.rmtree(tmp, ignore_errors=True)
        (tmp / "out").mkdir(parents=True)
        text = "这是启动自检，不要调用任何其他工具，只回复两个字母：ok"
        (tmp / "prompt.md").write_text(text)
        proc = None
        try:
            proc = await self._spawn(self._command(tmp), tmp)
            out, err = await asyncio.wait_for(
                proc.communicate(b"" if self.backend == "antigravity" else text.encode()), 120)
        except asyncio.TimeoutError:
            if proc:
                await self._kill(proc)
                await proc.communicate()
            return "自检超时（120 秒）"
        except OSError as e:
            return f"起不来 {self.backend}：{e}"
        except asyncio.CancelledError:
            if proc:
                await self._kill(proc)
                await proc.communicate()
            raise
        res = self._parse(out)
        if res["error"]:
            return f"{self.backend} 报错：{res['error']}"
        if proc.returncode or not res["completed"]:
            return f"{self.backend} 异常退出（{proc.returncode}）：{err.decode(errors='replace')[-300:]}"
        return None

    def cleanup(self) -> int:
        cutoff = time.time() - self.cfg.keep_runs_hours * 3600
        n = 0
        for d in self.cfg.groups.glob("*/runs/*"):
            if d.is_dir() and d.stat().st_mtime < cutoff:
                shutil.rmtree(d, ignore_errors=True)
                if d.exists() and self.cfg.agent_user:
                    # agent 建的子目录万一不可写，以 agent 身份再删一遍
                    subprocess.run(["sudo", "-n", "-u", self.cfg.agent_user, "rm", "-rf", "--", str(d)])
                    shutil.rmtree(d, ignore_errors=True)
                n += not d.exists()
        return n


def parse_antigravity_events(out: bytes) -> dict:
    """agy --output-format stream-json；最终 result 的用量已是累计值。"""
    res = {"usage": {"input": 0, "cache_read": 0, "cache_write": 0, "output": 0},
           "steps": 0, "error": None, "completed": False, "last": ""}
    steps = set()
    for line in out.decode(errors="replace").splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(ev, dict):
            continue
        if ev.get("event") == "step_update":
            step = ev.get("step_update") or {}
            if step.get("state") == "DONE" and step.get("step_type") != "user_input":
                steps.add(step.get("step_index"))
        elif ev.get("event") == "result":
            result = ev.get("result") or {}
            res["completed"] = result.get("status") == "SUCCESS"
            res["last"] = result.get("response") or ""
            if not res["completed"]:
                res["error"] = str(result.get("error") or result.get("status")
                                   or "Antigravity 未返回状态")[:500]
            u = result.get("usage") or {}
            cached = u.get("cache_read_tokens", 0)
            res["usage"] = {"input": max(u.get("input_tokens", 0) - cached, 0),
                            "cache_read": cached, "cache_write": 0,
                            # thinking_tokens 已包含在 output_tokens 里，不重复累加。
                            "output": u.get("output_tokens", 0)}
    res["steps"] = len(steps)
    return res


def parse_events(out: bytes) -> dict:
    """codex exec --json 的事件流 → 用量、步数、错误、最后一句话。

    item 类型的 error（比如「Model metadata not found」）只是警告，不算失败；
    失败看 turn.failed 和顶层 error 事件。
    """
    usage = {"input": 0, "cache_read": 0, "cache_write": 0, "output": 0}
    res = {"usage": usage, "steps": 0, "error": None, "completed": False, "last": ""}
    for line in out.decode(errors="replace").splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        t = ev.get("type")
        if t == "turn.completed":
            res["completed"] = True
            u = ev.get("usage") or {}
            cached, written = u.get("cached_input_tokens", 0), u.get("cache_write_input_tokens", 0)
            # input_tokens 里包含了缓存读和缓存写，拆出来单算
            usage["input"] += max(u.get("input_tokens", 0) - cached - written, 0)
            usage["cache_read"] += cached
            usage["cache_write"] += written
            usage["output"] += u.get("output_tokens", 0)
        elif t == "turn.failed":
            res["error"] = str((ev.get("error") or {}).get("message") or ev)[:500]
        elif t == "error":
            res["error"] = str(ev.get("message") or ev)[:500]
        elif t == "item.completed":
            item = ev.get("item") or {}
            if item.get("type") == "agent_message":
                res["last"] = item.get("text", "")
            elif item.get("type") != "error":
                res["steps"] += 1
    return res


def _toml(s: str) -> str:
    return json.dumps(s)  # JSON 字符串字面量对这些值也是合法的 TOML 字符串


def day_start() -> float:
    t = time.localtime()
    return time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1))


def _safe(name: str) -> str:
    keep = "".join(c if c.isalnum() or c in "._-" else "_" for c in name)
    return keep[-80:] or "file"
