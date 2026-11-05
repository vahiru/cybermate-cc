# 赛博群友（Codex / Antigravity）

群里每来一条消息，就跑一次 `codex exec`，模型是经 Floway（OpenAI Responses 兼容）转发的 Claude Haiku。由它自己决定沉默、闲聊还是干活，通过一组 Telegram 工具（MCP）直接发消息。主进程只做收发和排队。

默认后端是 Codex，也支持 `CGM_BACKEND=antigravity`，直接运行官方 `agy` CLI，使用该系统用户已登录的 Antigravity 账号额度。

为什么是 Codex 不是 Claude Code：Floway 拒绝 Claude Code 自带的 `claude-code-20250219` beta 头，而 Codex 走 Responses 接口没有这个问题；Codex 是单个二进制，关掉用不上的功能后提示前缀约 8k token（Claude Code 是 2–4 万）。

可以用 bot 跑（Bot API），也可以用真人账号跑（UserBot / MTProto），由 `CGM_TRANSPORT` 切换，其余一切相同。

旧的 DeepSeek 版在 `../poorbot`，两者不共享代码。

## 使用 Antigravity 账号额度

通过官方 CLI 的 [headless 模式](https://antigravity.google/docs/cli/headless/) 运行，不需要推理 API key。
先按[官方说明](https://antigravity.google/docs/getting-started/?tab=cli)安装 `agy`，在运行 agent 的**同一系统用户**下交互启动一次 `agy` 完成登录。
认证由 CLI 管理，主进程不读取或转存 OAuth token；未登录、额度不足等错误会走现有自检和报错提醒。

配置 `.env`（本地调试时需自行把环境变量导入 shell；程序不自动加载 `.env`）：

```dotenv
CGM_BACKEND=antigravity
CGM_ANTIGRAVITY_BIN=agy
# 删除原有 CGM_MODEL 可沿用 CLI 默认模型；也可用 agy models 查看后明确指定
# CGM_MODEL=gemini-3.8-flash-low
```

Telegram 的 token/session、群列表和 owner 等配置沿用前面的启动步骤。`LLM_API_KEY` 可以留空；
此时现有 MCP `generate_image` 默认关闭。需要继续通过 Floway 生图时，保留 `LLM_API_KEY` 和
`CGM_LLM_BASE_URL`，也可设 `CGM_IMAGE_MODEL=` 明确关闭生图 API。Antigravity 推理不会收到这个 key。
默认 `CGM_PRICE=0,0,0,0`，统计 token 和运行时间，不把账号额度折算成美元；原有 `CGM_PRICE` 若已设置会继续生效。

本地验证（以下命令不连接 Telegram；`CGM_WORK` 中应有项目的 `AGENTS.md`）：

```bash
CGM_BACKEND=antigravity CGM_AGENT_USER= CGM_WORK="$PWD/work" \
    CGM_DB=/tmp/cgm-antigravity-check.db .venv/bin/python -m cgm selfcheck
```

每次 run 在自己的 `.agents/` 中放置人设副本和 Telegram MCP 配置，令牌仅对本轮有效；
`agy` 先读取当前目录的 `prompt.md`，再处理消息。不同群使用独立的新会话，保留原有群笔记、
消息队列、发言次数上限、`/stop`、超时和统计。完整事件流仍存为 `events.jsonl`。

Docker 可用附加配置安装官方 CLI，并持久化 agent 的 `.gemini` 目录：

```bash
docker compose -f compose.yaml -f compose.antigravity.yaml build
docker volume create cybermate-cc_antigravity
docker run --rm -it --user agent -e SSH_CONNECTION -e SSH_TTY \
    -v cybermate-cc_antigravity:/home/agent/.gemini \
    --entrypoint /usr/local/bin/agy cybermate-cc-cgm:latest
docker compose -f compose.yaml -f compose.antigravity.yaml run --rm cgm selfcheck
docker compose -f compose.yaml -f compose.antigravity.yaml up -d
```

登录容器只传 SSH 环境，不加载服务的 `.env`。完成官方认证后退出 CLI。
以上卷名对应默认项目名 `cybermate-cc`；若指定了其他 Compose 项目名，须同步调整卷名前缀。
后续 Compose 命令也应带这两个 `-f`。
官方认证可能使用系统钥匙串，`.gemini` 卷只持久化文件；Linux 容器还需满足官方 CLI 的
认证存储条件，不能直接沿用 macOS 钥匙串登录。本机已验证账号推理和 Telegram MCP；
Linux amd64 容器已验证官方 OAuth 登录、跨容器复用登录、自检与 Telegram 连接。
安装脚本使用构建时的官方版本，Codex 默认构建不安装它。

## 跑起来（bot）

1. 找 @BotFather 建 bot，**`/setprivacy` 设为 Disable**（否则 bot 只能看到 @它 的消息），然后把它拉进群
2. `cp .env.example .env`，填 `TELEGRAM_BOT_TOKEN`、`LLM_API_KEY`、`CGM_OWNER_ID`
3. `mkdir -p sessions && docker compose up -d --build`
4. 在群里随便发一句，`docker compose logs` 里会看到 `群 -100xxx（群名）不在 CGM_CHATS 里`，把这个 id 填进 `CGM_CHATS`，`docker compose up -d`
5. 私聊 bot 发一次 `/start`，之后用量提醒和报错才能私聊给你

## 跑起来（UserBot）

用一个专门的小号，别用自己的主号：这个账号发的每条消息都会被当成「你」写进聊天记录。

1. session：可以复用兼容的旧版 cybermate session，将其拷到 `sessions/live.enc`，口令不变。
   要新登录的话：
   ```bash
   docker compose run --rm -it -v "$PWD/sessions:/sessions" \
       -e TG_API_ID=... -e TG_API_HASH=... cgm login
   ```
   容器里的 `bot` 用户要对宿主机的 `sessions/` 有写权限；嫌麻烦就在本机 venv 里登：`CGM_SESSION=sessions/live.enc TG_API_ID=... TG_API_HASH=... python -m cgm login`。
   api_id 必须是自己在 my.telegram.org 申请的，**别用官方客户端的公开 id**（2040、6 等），实测两分钟就被踢下线
2. `.env` 里设 `CGM_TRANSPORT=user`、`CGM_PASSPHRASE`、`LLM_API_KEY`、`CGM_OWNER_ID`
3. 容器里的 `bot` 用户要能读这个文件：`chmod 644 sessions/live.enc`。它是密文，agent 读得到文件但读不到口令
4. `docker compose run --rm cgm chats` 列出账号所在的群和 id，填进 `CGM_CHATS`
5. `docker compose up -d --build`；用 owner 账号私聊这个小号一次，管理命令和提醒就走这个私聊

和 bot 模式的区别：

| | bot | UserBot |
|---|---|---|
| 首次启动的上下文 | 从零开始攒 | 每个群先拉最近 50 条历史 |
| 别的 bot 的消息 | 看不到 | 入库当上下文，但不触发 run（防两个 bot 对刷） |
| 已读 | 没有 | 每次 run 开始把新消息标为已读 |
| 发消息 | 立即 | 先「正在输入」，按每字 60–100ms 停顿（最多 6 秒）再发 |
| 被叫到 | @、回复、`CGM_NAMES` | 同左（用 Telegram 的 mentioned 标记） |
| 在手机上用这个号手动发的话 | — | 记成「你」，下次 run 能看到 |
| 限流 | — | 60 秒以内的 FloodWait 自动等；更长的返回给模型，`/status` 里显示累计次数 |

启动时会先用所选后端跑一次最小自检；不通过就不接消息，每 5 分钟重试，原因写在日志里并私聊 owner。

改人设或规则：编辑 `work/AGENTS.md` 或 `work/codex/config.toml`，然后 `docker compose restart`，不用重建镜像。升级 Codex：改 `Dockerfile` 里的 `CODEX_VERSION` 再重建。

`generate_image` 支持文字生图和参考图改图，例如 `generate_image(prompt="改成水彩风格", images=["in/photo.jpg"], filename="watercolor.png")`。`images` 可传本次 run 的 `in/`、`out/` 图片路径（PNG/JPEG/WebP，最多 16 张，每张小于 50MB），结果仍写到 `out/`，用 `send_file` 发出。传图时走兼容的 `/images/edits` multipart 接口，需要 Floway 上游支持；不传图时走 `/images/generations`。

## 发言草稿检查（东坡语法）

默认开启本地小模型检查，参考 [Edelweiss 的这个提交](https://github.com/chiyuki0325/Edelweiss/commit/1b5fafab4b2f4ac0fbc7a02529f5039c6551ef06)，
检测凭空预设反驳、追加不必要免责声明的防御性结尾。
在 `send_message` 正文、`edit_message` 新正文和 `send_file` 的说明文字发送前统一检查，Bot 和 UserBot 都适用。

先匹配 `[不但仅凭只尚却]|无法|更准确`，再对末尾 120 个 Unicode 字符做字符 TF-IDF / 逻辑回归推理，
分数严格超过模型自带阈值才退回。只用 Python 标准库，不额外请求 LLM；权重在第一次命中关键词时加载，整个进程共用一份。
每个 run 最多退回一次，返回 `dongpo_review_required` 和改写建议，让 agent 保留有效信息、合理限定和有依据的纠正后重试。
退回不消耗发言额度、不修改 Telegram 消息、不写入聊天记录；工具日志和本次 run 的事件流仍保留这次调用。
它是一次表达复核，改写后的重试允许通过，存在误判和漏判。

Docker 构建时自动下载约 1.4 MB 的固定版本权重，并校验大小及 Git blob SHA-1；权重随镜像部署，运行时不访问 GitHub。
本地调试先运行：

```bash
.venv/bin/python -m cgm.dongpo --download
```

也可从[固定版本模型地址](https://raw.githubusercontent.com/chiyuki0325/Edelweiss/1b5fafab4b2f4ac0fbc7a02529f5039c6551ef06/src/driver/dongpo/model.json)
下载原始文件到 `cgm/dongpo-model.json`，保留原始字节；下载命令会校验已有文件，Docker 构建也会复用它。
不要重新格式化 JSON，否则完整性校验会失败。来源及校验值见 [cgm/dongpo-model.md](cgm/dongpo-model.md)。
启用检查却缺少权重时，工具服务启动会明确报错；运行中校验或推理失败会返回工具错误。

关闭检查：在 `.env` 设置 `CGM_NOT_DONGPO=0`，然后 `docker compose up -d` 重新创建容器。
接入代码和模型下载流程首次上线需要重建镜像，按下方部署流程执行。

测试：`.venv/bin/python -m unittest discover -s tests -v`。没有本地权重时会跳过真实模型冒烟测试，其余测试不需要网络或 Telegram。

## 部署到远端服务器

例如在本机交叉构建 amd64 镜像并通过 SSH 传到服务器：

```bash
docker build --platform linux/amd64 -t cybermate-cc-cgm:latest .
docker save cybermate-cc-cgm:latest | gzip | ssh USER@SERVER 'gunzip | docker load'
# 同步 work/、compose.yaml 和填写好的 .env 到远端项目目录后：
ssh USER@SERVER 'cd ~/cybermate-cc && docker compose up -d --no-build'
```

按服务器资源调整 compose 中的 `mem_limit`。Codex 后端需在 `.env` 中设置 `CGM_LLM_BASE_URL` 为实际的 Responses 兼容 API 地址。

## Codex 的权限

`codex exec --dangerously-bypass-approvals-and-sandbox`，配置里也是 `approval_policy = "never"`、`sandbox_mode = "danger-full-access"`。隔离靠容器和单独的 agent 用户，不靠 Codex 自己的沙箱：它在无特权容器里起不来，`workspace-write` 还默认断网，查资料就废了。

`work/codex/config.toml` 里关掉了 apps、browser_use、computer_use、image_generation、multi_agent、plugins 等群聊用不上的功能，每一个都会往系统提示里塞工具定义。网页搜索 `web_search = "live"` 走 Floway 配的 Tavily。

## 结构

```
Telegram ─→ 主进程（bot 用户）──写入──→ SQLite
              │  每群一个循环：有新消息就拼 prompt.md，起一次 codex exec
              │
              ├─ sudo -u agent ─→ codex exec（cwd = run 目录，CODEX_HOME 里的 AGENTS.md → /work/AGENTS.md）
              │                     └─ MCP server（stdio）─HTTP+run token─┐
              └─ 127.0.0.1:8765 ←───────────────────────────────────────┘
                   真正调用 Telegram：send_message / edit_message / send_file / react
```

| 文件 | 干什么 |
|---|---|
| `cgm/__main__.py` | 入口：常驻、自检、手动 run |
| `cgm/handlers.py` | 群消息入库并触发；owner 私聊命令；UserBot 冷启动拉历史 |
| `cgm/transport/` | Telegram 这一层：`botapi.py`（aiogram）、`userbot.py`（Telethon），接口在 `__init__.py` |
| `cgm/creds.py` | UserBot session 加解密（scrypt + AES-GCM，和旧版同格式） |
| `cgm/agent.py` | 每群排队、建 run 目录、下载附件、起 `codex exec`、解析事件流、超时、统计、提醒、清理 |
| `cgm/prompt.py` | 拼 `prompt.md`：群笔记 → 最近 50 条 → 新消息 → 固定指令 |
| `cgm/toolapi.py` | 本地 HTTP 接口：校验 run token、每次 run 最多 5 条、`out/` 路径检查 |
| `cgm/dongpo.py` | 东坡语法检测：本地字符 TF-IDF / 逻辑回归、权重懒加载、固定版本下载及校验 |
| `cgm/mcp_telegram.py` | stdio MCP server，只转发，不持有 bot token |
| `cgm/tgfmt.py` | Markdown 代码块等转 Telegram HTML，超 4096 字按段切 |
| `cgm/store.py` | SQLite：消息、每群读到哪了、run 统计、开关 |
| `work/AGENTS.md` | 人设和行为规则（作为 Codex 的全局 AGENTS.md，处在提示词前部吃缓存） |
| `work/codex/config.toml` | 模型端点、权限、关掉的功能、网页搜索 |

容器里的目录：

```
/work/AGENTS.md                      只读挂载
/work/codex/config.toml              只读挂载
/home/agent/.codex/                  agent 的 CODEX_HOME，可写；config.toml 和 AGENTS.md 是指向 /work 的软链接
/work/groups/<chat_id>/notes.md      群笔记，agent 自己读写
/work/groups/<chat_id>/runs/<run_id>/{in,out,prompt.md,events.jsonl}   保留 24 小时
/data/cgm.db                         只有 bot 用户能读
/sessions/live.enc                   UserBot 的加密 session，只读挂载
```

## 私聊

私聊当成一个两个人的群：有自己的聊天记录和群笔记（`/work/groups/<user_id>/`），每条私信都算叫到它，触发一次 run。
谁能私聊由 `CGM_DM` 决定：`members`（默认，owner 加上在受管群里说过话的人）、`all`、`off`。
不在范围内的私信直接忽略，不花钱；UserBot 模式下这条尤其要紧，真人账号会收到 spam。

owner 发的 `/status` 等已知命令走管理命令，其余私信和别人一样正常聊。

## 两个用户

主进程以 `bot` 用户运行，持有 bot token（或 UserBot 的 session 口令）；`codex exec` 通过 sudo 以 `agent` 用户运行，环境变量里只有 `LLM_API_KEY` 和 `CODEX_HOME`。如果两者是同一用户，agent 起的任何 shell 都能 `cat /proc/<主进程>/environ` 拿到 token 或口令，所以必须分开。UserBot 模式下这一点更要紧：泄露的是一个真人账号。两者同属 `cgm` 组共用 run 目录；数据库目录 `0700`，agent 读不到。

超时或 `/stop` 时，主进程以 agent 身份 `pkill -s` 杀掉整个会话（codex、它起的 shell/python、MCP server）。Codex 没有轮数上限，失控靠 `CGM_RUN_TIMEOUT`（默认 5 分钟）兜底。

本地调试不设 `CGM_AGENT_USER` 时不切用户；`CGM_CODEX_HOME` 指向一个放了 `config.toml`、`AGENTS.md` 软链接的目录。

## 里程碑验收怎么跑

```bash
# 1. 容器与 codex exec：out/ 里应出现中文标题正常的图
echo '画一张 y=sin(x) 的图，标题用中文，存到 out/' > /tmp/p.md
docker compose cp /tmp/p.md cgm:/tmp/p.md
docker compose exec cgm python -m cgm run --prompt /tmp/p.md

# 2. Telegram 工具：不接群，直接让它往测试群发
docker compose exec cgm python -m cgm run --chat -100xxx --prompt /tmp/p2.md
```

第 2 步的 prompt 写成「用 send_message 发一句话，再用 edit_message 改掉，然后把一张图用 send_file 发出来」。连发 6 条那条验收，模型会按 AGENTS.md 的「不刷屏」拒绝，上限本身在 `toolapi.py` 里硬拦，第 6 次调用返回错误。

## owner 私聊命令

私聊里只认 `CGM_OWNER_ID`，别人发这些命令会被当成普通私信。UserBot 模式下就是私聊那个小号。

| 命令 | |
|---|---|
| `/status` | 全局和各群是否暂停、是否在跑、今日 run 数、开口次数、花费、缓存命中比例 |
| `/stop [chat_id]` | 杀掉在跑的 run |
| `/pause [chat_id]` | 不带参数全部暂停，带群 id 只停一个群；都会顺手杀掉在跑的。消息照常入库 |
| `/resume [chat_id]` | 不带参数全部恢复（包括群友在群里暂停的），带群 id 只恢复一个群。不补跑暂停期间的消息 |

**在群里**：任何人发 `/pause`（或 `/stop`）都能暂停这个群，正在跑的也一起停，账号回一句「已暂停」并私聊通知 owner。只有 owner 能恢复（群里 `/resume` 或私聊）。owner 在群里发 `/status` 等其余命令，结果私聊回给 owner。这些命令都不交给模型。别的 bot 的 `/xxx` 照常当聊天。

**合并**：没叫它的消息攒 `CGM_BATCH_SECONDS`（默认 45）秒再一起跑一次，叫到它的立刻跑。热闹的群里能把 run 数降到几分之一。

**消息归属**：用 `(chat_id, msg_id) → run task` 记录正在处理的消息，一批新消息共用一个 task，同一条消息不会同时分配给两个 run。结束、异常、超时或取消后清理映射；已处理的消息由每群的已读游标排除，不会重新作为新消息分配。prompt 明确列出本轮负责的消息 ID，历史和翻页结果只作上下文，避免下一轮再次回应旧请求。工具层不检查回复归属，因此模型仍可能违反提示而重复回复。

| `/last` | 最近 10 次 run 一行摘要 |
| `/notes [chat_id]` | 看群笔记 |

当日花费超过 `CGM_COST_ALERT_USD` 私聊提醒一次，不自动停。Codex 不报花费，`cost` 是按 `CGM_PRICE` 单价和 token 数估算的，Floway 的实际计费以它为准。run 出错（额度、限流等）私聊提醒，10 分钟内最多一次。

## 日志

每次 run 一行 `run {...}`：run id、群、新消息数、轮数、耗时、`cost`、`input`/`cache_read`/`cache_write`/`output` token、发了几条、是否超时、错误。同样的数据在 `runs` 表里。完整的 codex 事件流在 run 目录的 `events.jsonl`。「轮数」是这次 run 里工具调用和消息的步数。

```bash
docker compose exec cgm sqlite3 /data/cgm.db \
  "select date(started,'unixepoch','localtime') d, count(*), sum(sends>0), round(sum(cost),3),
          round(1.0*sum(cache_read)/sum(cache_read+cache_write+input),2) from runs group by d"
```

## 本地实测（Codex + Floway Haiku，假 Telegram，花费为估算）

| 场景 | 结果 | 耗时 | 花费 |
|---|---|---|---|
| 群友闲聊，没叫它（缓存热） | 沉默 | 3.6–4.2s | $0.004 |
| @它画 sin 图，中文标题 | 占位「等下」→ 发图 → 占位改成「好了喵 图在上面」 | 20s | $0.028 |
| 「以后叫我老明」 | 回一句，群笔记里记下称呼 | 13s | $0.025 |

缓存热的时候，约 14k 输入 token 里 11k 命中缓存。

## 待定

- bot 的名字和性格：`work/AGENTS.md` 先沿用旧版的 yuki_mochi 猫娘，定稿后改
- 历史窗口 50 条：试用一周后按花费和效果调
- 是否加 Sonnet subagent 处理重活
