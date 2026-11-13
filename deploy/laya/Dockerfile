# =============================================================================
# Laya / System One sidecar 镜像（Day 10 · A2-b，dev-A）
# 形态（总览 §8 Q7 裁决，2026-11-13）：**挂载宿主已下载权重卷**——权重 615MB
# 宿主已有（本地 .env 实测 SYSTEM_ONE_MODEL_PATH=C:/models/laya/multilingual），
# 容器只装 CPU torch + transformers，不 COPY 权重进镜像层。
#
# 依赖口径：sidecar 与主 .venv 隔离（AGENTS §1 / scripts/sidecar_requirements.txt
# 「torch 不进主环境」同源）；server.py 只做 GET /health + POST /v1/decide，
# 不连业务库、不写 decision_logs（app/system_one/server.py 模块 docstring 口径）。
# =============================================================================

FROM python:3.12-slim

# torch 安装策略（S4 实测三连坑留痕：容器直连下 190MB 会 SSL 超时；经
# host.docker.internal 走本机代理被拒——代理只监听 127.0.0.1）：
# **宿主机预下载 Linux cp312 wheel 到 deploy/laya/wheels/（宿主代理稳定），
# 镜像内 --no-index 离线安装**。wheels/ 不进 git（.gitignore 登记），
# 重建命令（宿主）：
#   python -m pip download torch --index-url https://download.pytorch.org/whl/cpu \
#     --platform manylinux_2_28_x86_64 --python-version 312 --only-binary=:all: -d deploy/laya/wheels
COPY deploy/laya/wheels /tmp/wheels
RUN pip install --no-cache-dir --no-index --find-links=/tmp/wheels torch \
 && rm -rf /tmp/wheels

# 其余依赖走 PyPI（小包直连稳定）；httpx：import 链经 app/system_one/client.py 需要
RUN pip install --no-cache-dir \
      "transformers>=4.44" \
      "fastapi>=0.115" \
      "uvicorn[standard]>=0.32" \
      "pydantic>=2.9" \
      "pydantic-settings>=2.6" \
      "httpx>=0.27" \
      numpy

WORKDIR /app

# 只需要 app/ 的 system_one 与 core 配置链；alembic/业务表/库驱动一律不进本镜像
COPY app/ ./app/

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app

# 权重路径由 compose 注入 SYSTEM_ONE_MODEL_PATH=/models/laya/multilingual（宿主卷只读挂载）
EXPOSE 7001

CMD ["uvicorn", "app.system_one.server:app", "--host", "0.0.0.0", "--port", "7001"]
