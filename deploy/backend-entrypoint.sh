#!/bin/sh
# ============================================================================
# backend 容器入口（Day 10 · A1b，dev-A）
# 裁决 Q-A1：启动前自动执行 alembic upgrade head（D3「干净主机三步起全栈」
# 判据依赖库有表）。迁移连接串经 alembic/env.py 走 get_settings().database_url，
# 与应用同源；容器内由 compose 以 DATABASE_URL 环境变量注入（指向服务名）。
# 跳过迁移：SKIP_MIGRATIONS=1（冒烟/一次性容器用）。
# ============================================================================
set -e

cd /app

if [ "${SKIP_MIGRATIONS:-0}" != "1" ]; then
    echo "[entrypoint] alembic upgrade head ..."
    alembic upgrade head
else
    echo "[entrypoint] SKIP_MIGRATIONS=1，跳过迁移"
fi

exec uvicorn app.main:app --host 0.0.0.0 --port "${API_PORT:-8000}"
