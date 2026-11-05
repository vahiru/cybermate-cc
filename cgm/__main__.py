"""入口。

    python -m cgm                                常驻：收消息、排队、跑 codex
    python -m cgm selfcheck                      只做一次启动自检
    python -m cgm run --prompt FILE [--chat ID]  手动跑一次 run（里程碑 1、2 的验收）
    python -m cgm login                          UserBot 登录：手机号 → 验证码 → 2FA，存成加密 session
    python -m cgm chats                          列出当前账号所在的群和 id（填 CGM_CHATS 用）
"""

import argparse
import asyncio
import html
import logging
import os
from pathlib import Path

from . import config, transport
from .agent import Runner
from .handlers import Handlers
from .store import Store
from .toolapi import ToolAPI

log = logging.getLogger("cgm")


def _notifier(tp: transport.Transport | None, owner_id: int):
    async def notify(text: str) -> None:
        log.warning("通知 owner：%s", text)
        if tp and owner_id:
            try:
                await tp.send_text(owner_id, html.escape(text, quote=False))
            except Exception as e:
                log.error("私聊 owner 失败：%s", e)
    return notify


async def janitor(runner: Runner) -> None:
    while True:
        try:
            if n := runner.cleanup():
                log.info("清理了 %d 个过期 run 目录", n)
        except Exception:
            log.exception("清理失败")
        await asyncio.sleep(3600)


async def serve(cfg: config.Config) -> None:
    store = Store(cfg.db)
    tp = transport.make(cfg)
    await tp.connect()
    log.info("%s %s（%s），群 %s", cfg.transport, tp.me_name, tp.me_id, sorted(cfg.chats) or "未配置")
    api = ToolAPI(tp, store, cfg.api_port, llm_base_url=cfg.llm_base_url,
                  llm_key=os.environ.get("LLM_API_KEY", ""), image_model=cfg.image_model,
                  reaction_blacklist=cfg.reaction_blacklist, not_dongpo=cfg.not_dongpo)
    await api.start()
    notify = _notifier(tp, cfg.owner_id)
    runner = Runner(cfg, store, tp, api, notify)

    while why := await runner.selfcheck():
        log.error("自检失败，暂不接消息：%s", why)
        await notify(f"自检失败，暂不接消息，5 分钟后重试：\n{why[:300]}")
        await asyncio.sleep(300)
    log.info("自检通过")

    h = Handlers(cfg, store, runner, tp)
    await h.backfill()
    cleaner = asyncio.create_task(janitor(runner))  # noqa: F841  留住引用，免得被回收
    await tp.listen(h.on_message, h.on_edit)
    log.error("和 Telegram 的连接断了，退出等重启")
    raise SystemExit(1)


async def manual(cfg: config.Config, prompt_file: Path, chat: int | None) -> None:
    store = Store(cfg.db)
    tp = api = None
    if chat is not None:
        tp = transport.make(cfg)
        await tp.connect()
        api = ToolAPI(tp, store, cfg.api_port, llm_base_url=cfg.llm_base_url,
                  llm_key=os.environ.get("LLM_API_KEY", ""), image_model=cfg.image_model,
                  reaction_blacklist=cfg.reaction_blacklist, not_dongpo=cfg.not_dongpo)
        await api.start()
    runner = Runner(cfg, store, tp, api, _notifier(None, 0))
    run_id, run_dir = runner._new_run_dir(chat or 0)
    res = await runner.execute(chat, run_id, run_dir, prompt_file.read_text())
    print(f"run 目录：{run_dir}")
    print(f"out/：{sorted(p.name for p in (run_dir / 'out').iterdir())}")
    print(f"最后一句：{res['last']!r}  错误：{res['error']}")
    if tp:
        await tp.close()


async def selfcheck(cfg: config.Config) -> None:
    why = await Runner(cfg, Store(cfg.db), None, None, _notifier(None, 0)).selfcheck()
    print(why or "ok")
    raise SystemExit(1 if why else 0)


async def login(cfg: config.Config) -> None:
    from . import creds
    from .transport.userbot import client_from
    if not cfg.tg_api_id or not cfg.tg_api_hash:
        raise SystemExit("先设 TG_API_ID / TG_API_HASH（my.telegram.org 自己申请的，别用官方客户端的）")
    client = client_from(cfg)
    await client.start()
    me = await client.get_me()
    session = client.session.save()
    await client.disconnect()
    creds.save(cfg.session_file, {"session": session, "api_id": cfg.tg_api_id,
                                  "api_hash": cfg.tg_api_hash})
    print(f"已存 {cfg.session_file}（{me.id} @{me.username}）")


async def chats(cfg: config.Config) -> None:
    from .transport.userbot import UserBot
    ub = UserBot(cfg)
    await ub.connect()
    async for d in ub.client.iter_dialogs():
        if d.is_group:
            print(f"{d.id:>16}  {d.name}")
    await ub.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)
    # run 目录和群笔记要让 agent 用户也能写（同属 cgm 组）
    os.umask(0o002)

    p = argparse.ArgumentParser(prog="cgm")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("selfcheck")
    sub.add_parser("login")
    sub.add_parser("chats")
    r = sub.add_parser("run")
    r.add_argument("--prompt", type=Path, required=True)
    r.add_argument("--chat", type=int)
    a = p.parse_args()

    if a.cmd == "run":
        cfg = config.load(require_token=a.chat is not None)
        asyncio.run(manual(cfg, a.prompt, a.chat))
    elif a.cmd == "selfcheck":
        asyncio.run(selfcheck(config.load(require_token=False)))
    elif a.cmd == "login":
        asyncio.run(login(config.load(require_token=False, require_llm=False)))
    elif a.cmd == "chats":
        asyncio.run(chats(config.load(require_llm=False)))
    else:
        asyncio.run(serve(config.load()))


if __name__ == "__main__":
    main()
