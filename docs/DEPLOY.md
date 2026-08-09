# Docker / 生产部署说明

## 1. 前置要求

- Docker 24+（或 Docker Engine + Compose v2 插件）
- 开放端口（默认 **8790**），或用反向代理（Nginx / Caddy）
- 磁盘：预留上传空间（本地后端时）

```bash
docker --version
docker compose version
```

### 国内拉取基础镜像较慢时

```bash
docker pull docker.m.daocloud.io/library/python:3.11-slim-bookworm
docker tag  docker.m.daocloud.io/library/python:3.11-slim-bookworm python:3.11-slim-bookworm
# 或构建时指定：
docker build --build-arg BASE_IMAGE=docker.m.daocloud.io/library/python:3.11-slim-bookworm \
  -t file-transfer-system:latest .
```

## 2. 最快上手（Compose）

```bash
git clone https://github.com/zixiang0520/file-transfer-system.git
cd file-transfer-system

# 创建持久化目录
mkdir -p data storage logs

# 构建并后台启动
docker compose up -d --build

# 看日志
docker compose logs -f --tail=100
```

访问：

| 页面 | 地址 |
|------|------|
| 前台上传 | http://服务器IP:8790/ |
| 提取 | http://服务器IP:8790/extract |
| 管理后台 | http://服务器IP:8790/admin |

默认账号：`admin` / `admin123` → **登录后立刻改密**（后台「管理员账号」）。

公网地址请在后台填写 **公网地址**（`site.public_base_url`），用于生成提取链接。

## 3. 仅构建 / 运行镜像

```bash
# 构建
docker build -t file-transfer-system:latest .

# 运行（务必挂载 data + storage）
docker run -d --name file-transfer-system \
  --restart unless-stopped \
  -p 8790:8790 \
  -e TZ=Asia/Shanghai \
  -v "$(pwd)/data:/app/data" \
  -v "$(pwd)/storage:/app/storage" \
  -v "$(pwd)/logs:/app/logs" \
  file-transfer-system:latest
```

换端口示例（宿主机 18080 → 容器 8790）：

```bash
docker run -d --name file-transfer-system \
  -p 18080:8790 \
  -v "$(pwd)/data:/app/data" \
  -v "$(pwd)/storage:/app/storage" \
  file-transfer-system:latest
```

## 4. 数据持久化（重要）

| 挂载点 | 内容 |
|--------|------|
| `/app/data` | `config.json`、SQLite `transfers.db` |
| `/app/storage` | 本地存储的上传文件 |
| `/app/logs` | 日志（预留） |

**不要**把 `data/`、`storage/` 打进镜像；备份时拷贝这两个目录即可。

升级：

```bash
git pull
docker compose build --pull
docker compose up -d
# 配置与数据库因 volume 保留
```

## 5. 环境变量

| 变量 | 默认 | 说明 |
|------|------|------|
| `FTS_HOST` | `0.0.0.0` | 监听地址 |
| `FTS_PORT` | `8790` | 容器内端口 |
| `TZ` | `Asia/Shanghai` | 时区 |

业务配置（限额、云盘、管理员）在 **`data/config.json`** / 管理后台，不靠环境变量。

## 6. 反向代理（HTTPS 推荐）

### Caddy 示例

```caddy
transfer.example.com {
    reverse_proxy 127.0.0.1:8790
}
```

### Nginx 示例

```nginx
server {
    listen 443 ssl http2;
    server_name transfer.example.com;

    client_max_body_size 512m;   # 与后台单文件上限对齐

    location / {
        proxy_pass http://127.0.0.1:8790;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 300s;
        proxy_request_buffering off;  # 大文件上传更友好
    }
}
```

反代后后台把 **公网地址** 设为 `https://transfer.example.com`。

## 7. 健康检查

```bash
curl -fsS http://127.0.0.1:8790/health
# {"ok":true,...}
```

Compose / Dockerfile 已内置 healthcheck。

## 8. 常用运维

```bash
# 状态
docker compose ps

# 进入容器
docker compose exec file-transfer sh

# 停止 / 启动
docker compose stop
docker compose start

# 完全删除容器（保留 volume 目录）
docker compose down

# 看镜像
docker images | grep file-transfer
```

## 9. 安全清单

1. 改默认管理员密码（后台）
2. 不要把 `data/config.json` 提交 Git / 打进镜像
3. 公网用 HTTPS 反代；防火墙只放行 80/443 或指定端口
4. 按需收紧扩展名、单文件大小、最长有效期
5. 移动云盘 Authorization 只填后台，勿写进 Dockerfile / compose 明文（可用运行后后台配置）

## 10. 故障排查

| 现象 | 处理 |
|------|------|
| 容器起不来 | `docker compose logs`；检查端口占用 |
| 上传 413 | 反代 `client_max_body_size` 太小 |
| 重启丢文件/配置 | 未挂载 `data`/`storage` volume |
| 权限错误 | 宿主机目录给 1000 用户写权限：`chown -R 1000:1000 data storage logs` |
| 健康检查失败 | 等 `start_period`；确认 `/health` 可访问 |

## 11. 镜像推送到仓库（可选）

```bash
docker tag file-transfer-system:latest ghcr.io/<你的用户名>/file-transfer-system:latest
docker push ghcr.io/<你的用户名>/file-transfer-system:latest
```

或 Docker Hub：

```bash
docker tag file-transfer-system:latest <dockerhub用户>/file-transfer-system:latest
docker push <dockerhub用户>/file-transfer-system:latest
```
