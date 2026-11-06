# Day 4 声纹推理容器 — 只做 /extract 端点
# 不装 torch（重依赖隔离），用 sherpa-onnx 加载 CAM++ .onnx
FROM python:3.12-slim

WORKDIR /app

# 安装系统依赖（numpy 等需要）
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Python 依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 应用代码
COPY app.py .

# 模型挂载点（运行时从 compose 挂载进来，.gitignore 不入库）
RUN mkdir -p /app/models

EXPOSE 7003

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:7003/health').read()" || exit 1

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "7003"]
