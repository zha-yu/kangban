# mock-operations MCP 独立镜像（Day 10 七服务版提前件，端口 7102）。
#
# 为什么不用 backend 镜像：backend 服务本身尚无 Dockerfile（完整版留 Day 10），
# 本镜像按需最小化——只装运行时依赖（--no-dev）+ 源码（app/ 与 mock-operations），
# 不带 .env / 测试 / 前端 / 媒体文件（.dockerignore 收口，AGENTS §5：密钥不进镜像层）。
# weather-mcp 上容器时照此文件复制改路径即可。
#
# 构建（compose 内完成）：docker compose build mock-operations-mcp
# 运行：容器内 CMD 自带 --http；绑定地址由 compose 设 MOCK_OPERATIONS_MCP_HOST=0.0.0.0，
#       DATABASE_URL 指向 compose 服务名 postgres；不做宿主机端口映射。

FROM python:3.12-slim

# uv 按 uv.lock --frozen 精确安装，容器与本地 .venv 依赖一致
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

# 非 root 运行（AGENTS §5 部署红线）
RUN useradd --create-home --shell /usr/sbin/nologin silvermate

WORKDIR /srv

# 先只拷依赖清单：uv.lock 不变时源码改动不会打穿依赖层缓存
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev --no-install-project

# 源码：mock-operations 依赖 app/**（app.db 会话工厂、core 配置、services 写库层）
COPY app ./app
COPY mcp-servers/mock-operations ./mcp-servers/mock-operations

USER silvermate

ENV PYTHONPATH=/srv
EXPOSE 7102

# uv run --no-sync：运行时不再碰网络/锁文件；容器固定 streamable-http 模式
CMD ["uv", "run", "--no-sync", "python", "mcp-servers/mock-operations/server.py", "--http"]
