# syntax=docker/dockerfile:1
# 国内构建可：
#   docker pull docker.m.daocloud.io/library/python:3.11-slim-bookworm
#   docker tag  docker.m.daocloud.io/library/python:3.11-slim-bookworm python:3.11-slim-bookworm
# 或：docker build --build-arg BASE_IMAGE=docker.m.daocloud.io/library/python:3.11-slim-bookworm -t file-transfer-system:latest .
ARG BASE_IMAGE=python:3.11-slim-bookworm
FROM ${BASE_IMAGE}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    FTS_HOST=0.0.0.0 \
    FTS_PORT=8790 \
    TZ=Asia/Shanghai

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
# 国内网络可加 -i https://pypi.tuna.tsinghua.edu.cn/simple
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY main.py .
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh \
    && mkdir -p /app/data /app/storage /app/logs

EXPOSE 8790

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8790/health', timeout=3)" || exit 1

ENTRYPOINT ["/entrypoint.sh"]
