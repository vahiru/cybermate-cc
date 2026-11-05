"""所有配置都从环境变量读。密钥：TELEGRAM_BOT_TOKEN（或 UserBot 的 CGM_PASSPHRASE）和 LLM_API_KEY。"""

import os
from dataclasses import dataclass
from pathlib import Path


# 不许它点的表情。中文网络语境下带嘲讽、阴阳、敷衍、看戏意味的，加上本来就冒犯的
REACTION_BLACKLIST = (
    # 嘲讽、阴阳怪气（😁 黄豆龇牙、🤣 笑别人、🕊 鸽了、🤔 在这里读作质疑）
    "😁 🤣 🤡 🌚 🤨 😐 🥴 🤓 💅 🗿 😈 🤪 😎 🆒 🕊 🤔 "
    # 敷衍、看戏（👌 行吧、👏 反讽式鼓掌、👀 吃瓜）
    "🥱 🤷 🤷‍♂️ 🤷‍♀️ 🙈 🙉 🙊 👌 👏 🫡 👀 "
    # 冒犯、带颜色
    "🖕 💩 🤮 🤬 😡 👎 💋 🌭 🍌 💊"
)


def _emoji(s: str) -> str:
    """去掉变体选择符，让 ❤ 和 ❤️ 算同一个。"""
    return s.replace("\ufe0f", "").strip()


def _ints(raw: str) -> frozenset[int]:
    return frozenset(int(x) for x in raw.replace(",", " ").split() if x)


@dataclass(frozen=True)
class Config:
    bot_token: str
    owner_id: int
    chats: frozenset[int]        # 只在这些群里干活；空 = 哪个群都不理
    names: tuple[str, ...]       # 消息里出现这些词也算叫到它
    work: Path                   # /work：AGENTS.md、codex/config.toml、groups/
    db: Path
    model: str                   # antigravity 可留空，沿用 CLI 的默认模型
    llm_base_url: str            # OpenAI Responses 兼容的端点（Floway）
    image_model: str             # generate_image 用的模型；空 = 不提供生图
    reaction_blacklist: frozenset[str]  # 不许它点的表情
    price: tuple[float, float, float, float]  # 估算花费用：输入、输出、缓存读、缓存写，美元/百万 token
    run_timeout: int             # 秒
    history: int                 # prompt 里带多少条旧消息
    batch_seconds: int           # 没叫它的消息攒多久再跑；0 = 每条都立刻跑
    max_sends: int               # 每次 run 最多发几条
    cost_alert_usd: float        # 当日花费超过它就私聊 owner 一次
    api_port: int
    agent_user: str              # 用哪个系统用户跑 codex；空 = 和主进程同一用户（本地调试）
    codex_bin: str
    codex_home: Path             # agent 的 CODEX_HOME，里面的 config.toml 和 AGENTS.md 指向 /work
    mcp_python: str
    keep_runs_hours: int
    transport: str               # bot | user
    dm: str                      # 谁能私聊它：off | members（owner + 受管群里说过话的人）| all
    # 以下只有 transport = user 时用到
    session_file: Path           # 加密的 StringSession，口令在 CGM_PASSPHRASE
    tg_api_id: int               # 登录时用；已有 .enc 里存了就以 .enc 为准
    tg_api_hash: str
    device_model: str
    system_version: str
    app_version: str
    not_dongpo: bool = True       # 本地小模型检查发言草稿，每轮最多要求改写一次
    backend: str = "codex"        # codex | antigravity
    antigravity_bin: str = "agy"

    @property
    def groups(self) -> Path:
        return self.work / "groups"


def load(require_token: bool = True, require_llm: bool = True) -> Config:
    e = os.environ.get
    backend = e("CGM_BACKEND", "codex")
    if backend not in ("codex", "antigravity"):
        raise SystemExit("CGM_BACKEND 只能是 codex 或 antigravity")
    transport = e("CGM_TRANSPORT", "bot")
    if transport not in ("bot", "user"):
        raise SystemExit("CGM_TRANSPORT 只能是 bot 或 user")
    token = e("TELEGRAM_BOT_TOKEN", "")
    if require_token and transport == "bot" and not token:
        raise SystemExit("缺少 TELEGRAM_BOT_TOKEN")
    dm = e("CGM_DM", "members")
    if dm not in ("off", "members", "all"):
        raise SystemExit("CGM_DM 只能是 off、members 或 all")
    if require_token and transport == "user" and not e("CGM_PASSPHRASE"):
        raise SystemExit("缺少 CGM_PASSPHRASE（解密 UserBot session 用）")
    if require_llm and backend == "codex" and not e("LLM_API_KEY"):
        raise SystemExit("缺少 LLM_API_KEY")
    agent_user = e("CGM_AGENT_USER", "")
    price = tuple(float(x) for x in e("CGM_PRICE", "0,0,0,0" if backend == "antigravity"
                                    else "1,5,0.1,1.25").split(","))
    if len(price) != 4:
        raise SystemExit("CGM_PRICE 要四个数：输入,输出,缓存读,缓存写")
    return Config(
        bot_token=token,
        owner_id=int(e("CGM_OWNER_ID", "0")),
        chats=_ints(e("CGM_CHATS", "")),
        names=tuple(n for n in e("CGM_NAMES", "").replace(",", " ").split() if n),
        work=Path(e("CGM_WORK", "/work")),
        db=Path(e("CGM_DB", "/data/cgm.db")),
        model=e("CGM_MODEL", "" if backend == "antigravity" else "claude-haiku-5-5"),
        llm_base_url=e("CGM_LLM_BASE_URL", "https://api.example.com/v1"),
        image_model=e("CGM_IMAGE_MODEL", "" if backend == "antigravity" and not e("LLM_API_KEY")
                      else "gpt-image-2"),
        reaction_blacklist=frozenset(
            _emoji(x) for x in e("CGM_REACTION_BLACKLIST", REACTION_BLACKLIST).replace(",", " ").split()),
        price=price,
        run_timeout=int(e("CGM_RUN_TIMEOUT", "300")),
        history=int(e("CGM_HISTORY", "50")),
        batch_seconds=int(e("CGM_BATCH_SECONDS", "45")),
        max_sends=int(e("CGM_MAX_SENDS", "5")),
        cost_alert_usd=float(e("CGM_COST_ALERT_USD", "2")),
        api_port=int(e("CGM_API_PORT", "8765")),
        agent_user=agent_user,
        codex_bin=e("CGM_CODEX_BIN", "codex"),
        codex_home=Path(e("CGM_CODEX_HOME", "/home/agent/.codex")),
        mcp_python=e("CGM_MCP_PYTHON", "python3"),
        keep_runs_hours=int(e("CGM_KEEP_RUNS_HOURS", "24")),
        transport=transport,
        dm=dm,
        session_file=Path(e("CGM_SESSION", "/sessions/live.enc")),
        tg_api_id=int(e("TG_API_ID", "0")),
        tg_api_hash=e("TG_API_HASH", ""),
        # 和旧版 cybermate 登录时用的一致；换了会像换了台设备
        device_model=e("CGM_DEVICE_MODEL", "Desktop"),
        system_version=e("CGM_SYSTEM_VERSION", "Windows 10"),
        app_version=e("CGM_APP_VERSION", "5.5.1"),
        not_dongpo=e("CGM_NOT_DONGPO", "1").strip().lower() not in ("0", "false", "off", "no"),
        backend=backend,
        antigravity_bin=e("CGM_ANTIGRAVITY_BIN", "agy"),
    )
