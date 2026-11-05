FROM node:22-bookworm-slim

# 升级 Codex 时手动改这里
ARG CODEX_VERSION=0.164.0
ARG INSTALL_ANTIGRAVITY=false

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-venv ca-certificates sudo procps tzdata sqlite3 \
        git curl jq ffmpeg imagemagick fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g @openai/codex@${CODEX_VERSION} && npm cache clean --force

# 按需安装官方 CLI；安装脚本会校验发布包的 SHA512。
RUN if [ "$INSTALL_ANTIGRAVITY" = "true" ]; then \
        curl -fsSL https://antigravity.google/cli/install.sh -o /tmp/install-agy.sh \
        && bash /tmp/install-agy.sh --dir /usr/local/bin \
        && rm /tmp/install-agy.sh; \
    fi

COPY requirements.txt /tmp/requirements.txt
RUN python3 -m venv /opt/venv && /opt/venv/bin/pip install --no-cache-dir --timeout 120 --retries 5 -r /tmp/requirements.txt

# bot 跑主进程，持有 bot token；agent 跑 codex，读不到 bot 的环境变量。两者同属 cgm 组，共用 run 目录。
RUN groupadd cgm \
    && useradd -m -s /bin/bash -G cgm bot \
    && useradd -m -s /bin/bash -G cgm agent \
    && mkdir -p /work/groups /data \
    && chown bot:cgm /work/groups && chmod 2775 /work/groups \
    && chown bot:bot /data && chmod 700 /data

COPY docker/sudoers /etc/sudoers.d/cgm
COPY docker/agent-run /usr/local/bin/agent-run
COPY docker/matplotlibrc /home/agent/.config/matplotlib/matplotlibrc
# agent 的 CODEX_HOME 可写（codex 往里写日志和状态库），配置和人设软链接到只读挂载的 /work
RUN chmod 440 /etc/sudoers.d/cgm && chmod 755 /usr/local/bin/agent-run \
    && mkdir -p /home/agent/.codex /home/agent/.gemini \
    && chown agent:agent /home/agent/.gemini && chmod 700 /home/agent/.gemini \
    && ln -s /work/codex/config.toml /home/agent/.codex/config.toml \
    && ln -s /work/AGENTS.md /home/agent/.codex/AGENTS.md \
    && chown -R agent:agent /home/agent/.config /home/agent/.codex \
    && su agent -s /bin/sh -c "/opt/venv/bin/python -c 'import matplotlib.pyplot'"

COPY work/AGENTS.md /work/AGENTS.md
COPY work/codex /work/codex
COPY cgm /app/cgm
# 固定版本和 Git blob 校验；权重随镜像部署，运行时不访问 GitHub。
RUN cd /app && /opt/venv/bin/python -m cgm.dongpo --download

ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    LANG=C.UTF-8 \
    TZ=Asia/Shanghai \
    CGM_WORK=/work \
    CGM_DB=/data/cgm.db \
    CGM_AGENT_USER=agent \
    CGM_MCP_PYTHON=/opt/venv/bin/python

USER bot
WORKDIR /app
ENTRYPOINT ["python", "-m", "cgm"]
