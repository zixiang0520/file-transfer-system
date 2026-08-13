# Docker / 生产部署说明

## 1. 前置要求
- Docker 24+（或 Docker Engine + Compose v2 插件），**或** 本机 Python 3.11+ + systemd
- 开放端口（默认 **8790**），或用反向代理（Nginx / Caddy / OpenResty）
- 移动云盘账号（见下文「云盘绑定」）

```bash
docker --version
docker compose version
```

### 国内拉取基础镜像较慢时
```bash
docker pull docker.m.daocloud.io/library/python:3.11-slim-bookworm
docker tag  docker.m.daocloud.io/library/python:3.11-slim-bookworm python:3.11-slim-bookworm
```

## 2. 最快上手（Compose）
```bash
git clone https://github.com/zixiang0520/file-transfer-system.git
cd file-transfer-system

mkdir -p data storage logs

docker compose up -d --build

# 看日志
docker compose logs -f --tail=100
```

访问：
- 前台上传：http://服务器IP:8790/
- 提取：http://服务器IP:8790/extract
- 管理后台：http://服务器IP:8790/admin

默认账号：`admin` / `admin123` → **登录后立刻改密**（后台「管理员账号」）。

公网地址请在后台填写 **公网地址**（`site.public_base_url`），用于生成提取链接。

## 3. 移动云盘绑定（必做 · 对齐 OpenList）

文档参考：[OpenList 中国移动云盘](https://doc.oplist.org.cn/guide/drivers/139)

本系统**只使用 yun139 新个人云**，文件不落本地盘。

### 3.1 长期推荐（不轻易失效）

在管理后台 → **移动云盘绑定** 填写：

| 字段 | 说明 |
|------|------|
| 启用云盘 | 勾选 |
| **邮箱 Cookie** | 登录 https://mail.10086.cn/ → F12 → Application/网络请求里复制 **Cookie** 头整行。格式：`key1=value1; key2=value2`。必须含 **`Os_SSo_Sid`**、**`RMKEY`** |
| **用户名** | 手机号或邮箱 |
| **密码** | 139 密码 |
| Authorization | 可**留空**；系统会自动登录生成并刷新 |
| 根文件夹 | `/` 或显示路径如 `/文件流转`（自动解析/创建） |

保存后点 **「测试连接 / 自动续期」**，成功即可上传。

**自动续期行为：**

1. 优先刷新已有 Authorization（`authTokenRefresh`）
2. 刷新失败或业务返回 `认证失败(05050006)` → 用 Cookie+账密重新登录
3. 新 token 写回 `data/config.json`，请求自动重试

### 3.2 仅 Authorization（快速但不持久）

登录 https://yun.139.com → 开发者工具找 `hcy/file/list` → 复制 `Authorization: Basic ` **后面**的内容填入后台。过期后需手动更新；若同时配了 Cookie+账密，过期会自动救回。

### 3.3 安全

- Cookie / 密码 / Authorization **只写后台或服务器 `data/config.json`**
- **禁止**提交 Git、打进镜像、写进 compose 明文、发到公开聊天

## 4. 仅构建 / 运行镜像
```bash
# 构建
docker build -t file-transfer-system:latest .

# 运行（务必挂载 data）
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

## 5. 本机 Python + systemd（无 Docker）

```bash
cd /opt/file-transfer-system   # 或你的路径
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt   # 需 cryptography
# 若无 pip：uv pip install --python .venv/bin/python -r requirements.txt
```

`/etc/systemd/system/file-transfer-system.service` 示例：

```ini
[Unit]
Description=File Transfer System (yun139)
After=network.target

[Service]
Type=simple
WorkingDirectory=/opt/file-transfer-system
ExecStart=/opt/file-transfer-system/.venv/bin/python main.py --host 0.0.0.0 --port 8790
Restart=always
RestartSec=3
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now file-transfer-system
curl -fsS http://127.0.0.1:8790/health
```

可选每日鉴权巡检（失败写日志）：

```bash
chmod +x scripts/refresh-139.sh
# crontab
0 0 * * * /opt/file-transfer-system/scripts/refresh-139.sh >> /opt/file-transfer-system/logs/139-refresh.log 2>&1
```

## 6. 数据持久化（重要）
| 挂载点 / 目录 | 内容 |
|--------|------|
| `data/` → `/app/data` | `config.json`（含云盘凭证）、SQLite `transfers.db` |
| `storage/` | 历史遗留目录；**当前不落本地文件** |
| `logs/` | 日志 / 鉴权巡检 |

**不要**把 `data/` 打进镜像；备份时优先拷贝 `data/`。

升级：
```bash
git pull
docker compose build --pull
docker compose up -d
# 或 systemd：
# git pull && systemctl restart file-transfer-system
```

## 7. 环境变量
| 变量 | 默认 | 说明 |
|------|------|------|
| `FTS_HOST` | `0.0.0.0` | 监听地址 |
| `FTS_PORT` | `8790` | 容器内端口 |
| `TZ` | `Asia/Shanghai` | 时区 |

业务配置（限额、云盘、管理员）在 **`data/config.json`** / 管理后台，不靠环境变量。

## 8. 反向代理（HTTPS 推荐）
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
        proxy_request_buffering off;
    }
}
```

反代后后台把 **公网地址** 设为 `https://transfer.example.com`。

## 9. 健康检查
```bash
curl -fsS http://127.0.0.1:8790/health
# {"ok":true,"service":"file-transfer-system"}
```

Compose / Dockerfile 已内置 healthcheck。

## 10. 常用运维
```bash
# Docker
docker compose ps
docker compose logs -f --tail=100
docker compose restart

# systemd
systemctl status file-transfer-system
journalctl -u file-transfer-system -n 100 --no-pager
systemctl restart file-transfer-system
```

## 11. 安全清单
1. 改默认管理员密码（后台）
2. 不要把 `data/config.json` 提交 Git / 打进镜像
3. 公网用 HTTPS 反代；防火墙只放行 80/443 或指定端口
4. 按需收紧扩展名、单文件大小、最长有效期
5. 发现危险文件：后台包裹点「封禁 IP」，该 IP 无法再上传
6. 云盘鉴权只用后台配置（Cookie+账密 或 Authorization），勿明文进仓库
7. 根目录可用显示路径 `/文件流转`（自动解析 parentFileId）

## 12. 故障排查
| 现象 | 处理 |
|------|------|
| 容器起不来 | `docker compose logs`；检查端口占用 |
| 上传 413 | 反代 `client_max_body_size` 太小 |
| 重启丢配置 | 未挂载 `data` volume |
| 权限错误 | `chown -R 1000:1000 data storage logs` |
| 健康检查失败 | 等 `start_period`；确认 `/health` |
| **认证失败(05050006)** | 配齐 OpenList 长期绑定（Cookie+用户名+密码）后点测试连接；或更新 Authorization |
| 测试连接缺 RMKEY | Cookie 不完整，从 mail.10086.cn 重新复制整段 |
| 服务进程挂了 | systemd `Restart=always`；查 `journalctl -u file-transfer-system` |

## 13. 镜像推送到仓库（可选）
```bash
docker tag file-transfer-system:latest ghcr.io/<你的用户名>/file-transfer-system:latest
docker push ghcr.io/<你的用户名>/file-transfer-system:latest
```

或 Docker Hub：
```bash
docker tag file-transfer-system:latest <dockerhub用户>/file-transfer-system:latest
docker push <dockerhub用户>/file-transfer-system:latest
```
