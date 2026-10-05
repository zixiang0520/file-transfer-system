# syntax=docker/dockerfile:1
# 默认基础镜像 python:3.12-alpine（ZX-YSK 等无法访问 Docker Hub 的机器可本地构建，
# 依赖走 PyPI musllinux wheel，无需编译）。国内也可用：
#   --build-arg BASE_IMAGE=docker.m.daocloud.io/library/python:3.11-slim-bookworm
ARG BASE_IMAGE=python:3.12-alpine
FROM ${BASE_IMAGE}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    FTS_HOST=0.0.0.0 \
    FTS_PORT=8790 \
    TZ=Asia/Shanghai

WORKDIR /app

# alpine/slim 双兼容：alpine 用 apk，debian 系用 apt-get
RUN if command -v apk >/dev/null 2>&1; then \
        apk add --no-cache ca-certificates tzdata; \
    else \
        apt-get update && apt-get install -y --no-install-recommends ca-certificates tzdata \
        && rm -rf /var/lib/apt/lists/*; \
    fi

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
