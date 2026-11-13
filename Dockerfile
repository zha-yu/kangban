# syntax=docker/dockerfile:1
# ============================================================================
# backend 镜像（Day 10 · 节点 A1，dev-A）
# 口径：TD §8.3 Day 10「多阶段、非 root」；骨架对齐 deploy/weather/Dockerfile 既有
#       三个镜像的统一口径（python:3.12-slim + uv --frozen + 密钥不进镜像层）。
# 高风险文件（AGENTS §5）：方案与命令级自检见
#       docs/plans/day10/dev-a-容器化与部署链.md §4 S1（确认后落码）。
# 裁决：Q-A1 entrypoint 自动 alembic upgrade head；Q-A2 static/ COPY 进镜像
#       （实测 155MB，/static 挂载依赖 cwd）。
# 注意：容器内为 Linux 默认事件循环——Windows 方案 A（SelectorEventLoopPolicy）
#       仅本机联调适用，本文件不引入 policy 代码（任务书 §1.3）。
# ============================================================================

# ---------- builder：按 uv.lock 安装依赖（依赖清单先行，命中层缓存） ----------
FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# 全部依赖均为预编译 wheel（webrtcvad-wheels / av / psycopg[binary]），无需 gcc
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

# ---------- runtime：仅 .venv + 应用源码 + 迁移物 + 静态资产 ----------
FROM python:3.12-slim AS runtime

# 非 root 用户（对齐 deploy/weather/Dockerfile）
RUN useradd --create-home --uid 1000 silvermate

WORKDIR /app

COPY --from=builder --chown=silvermate:silvermate /app/.venv ./.venv
COPY --chown=silvermate:silvermate app/ ./app/
# 迁移物（Q-A1）——.dockerignore 已放行 alembic/ 与 alembic.ini
COPY --chown=silvermate:silvermate alembic/ ./alembic/
COPY --chown=silvermate:silvermate alembic.ini ./
# 静态资产（Q-A2）：main.py 的 /static 挂载取 os.getcwd()/static，TTS 落盘同处
COPY --chown=silvermate:silvermate static/ ./static/

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# 注意：属主一律在 COPY 时以 --chown 设置，禁止 RUN chown -R（会把整个 /app
# 复制进新层，A1 首版实测多占约 580MB、chown 耗时 34.8s）。
COPY --chown=silvermate:silvermate deploy/backend-entrypoint.sh /usr/local/bin/backend-entrypoint
RUN chmod +x /usr/local/bin/backend-entrypoint

USER silvermate

EXPOSE 8000

ENTRYPOINT ["backend-entrypoint"]
