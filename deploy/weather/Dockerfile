# weather-mcp 独立镜像（Day 10 七服务版提前件，端口 7101）。
#
# 与 deploy/mock-operations/Dockerfile 同款骨架（唯一真实外部接口 MCP，AGENTS §1）：
# uv --frozen --no-dev 精确装依赖、非 root 运行、.env 不进镜像层（AGENTS §5）。
# weather_core 依赖 app.core.*（配置/日志/trace context），故同样 COPY app/；
# 但 weather 不连数据库，容器无需 DATABASE_URL。
# WEATHER_API_KEY 等密钥由 compose 从 .env 注入环境变量，不写入任何镜像层。
#
# 构建：docker compose build weather-mcp
# 运行：容器内 CMD 自带 --http；绑定地址由 compose 设 WEATHER_MCP_HOST=0.0.0.0。

FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

RUN useradd --create-home --shell /usr/sbin/nologin silvermate

WORKDIR /srv

COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev --no-install-project

COPY app ./app
COPY mcp-servers/weather ./mcp-servers/weather

USER silvermate

ENV PYTHONPATH=/srv
EXPOSE 7101

CMD ["uv", "run", "--no-sync", "python", "mcp-servers/weather/server.py", "--http"]
