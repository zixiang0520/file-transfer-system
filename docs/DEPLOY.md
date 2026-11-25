# Docker / 生产部署说明

## 1. 前置要求
- Docker 24+（或 Docker Engine + Compose v2 插件），**或** 本机 Python 3.11+ + systemd
- 开放端口（默认 **8790**），或用反向代理（Nginx / Caddy / OpenResty）
- 任一存储后端：移动云盘账号（见 3.1）、OpenList 服务（3.2）、WebDAV 服务（3.3）或本地磁盘（3.4）

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

## 3. 存储后端配置（四选一）

后台 → **存储后端** 选择 `yun139` / `openlist` / `webdav` / `local`，保存后新上传生效；旧文件仍按各自后端读取。

### 3.1 移动云盘（yun139 · 对齐 OpenList）

文档参考：[OpenList 中国移动云盘](https://doc.oplist.org.cn/guide/drivers/139)

#### 3.1.1 长期推荐（不轻易失效）

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

#### 3.1.2 仅 Authorization（快速但不持久）

登录 https://yun.139.com → 开发者工具找 `hcy/file/list` → 复制 `Authorization: Basic ` **后面**的内容填入后台。过期后需手动更新；若同时配了 Cookie+账密，过期会自动救回。

#### 3.1.3 安全

- Cookie / 密码 / Authorization **只写后台或服务器 `data/config.json`**
- **禁止**提交 Git、打进镜像、写进 compose 明文、发到公开聊天

### 3.2 OpenList

适用于已经跑着 **OpenList**（[doc.oplist.org.cn](https://doc.oplist.org.cn/)）的机器：网盘驱动、多账号、目录缓存全交给 OpenList，本系统只用它的 REST API 做「建目录 / 上传 / 取直链 / 删除」。

| 字段 | 说明 |
|------|------|
| 启用 OpenList | 勾选 |
| 地址 | 站点根地址，如 `http://192.168.1.10:5244`、`https://pan.example.com`（**不要带 `/api`**，反代有子路径时一并写上） |
| 用户名 / 密码 | OpenList 账号，系统自动登录拿 token，401 自动重登 |
| 两步验证码 | 账号开了 2FA 时填当前 6 位码（可留空） |
| Token | 可选，优先于账密；OpenList 浏览器 localStorage 里的 `token`，默认 48h 过期，只填 token 时需手更 |
| 根目录 | 默认 `/文件流转`，不存在自动创建 |
| 直链下载（302） | 默认开：下载直接 302 到 `OpenList/d/路径?sign=…`，不占本机带宽；若 OpenList 只在内网可达，请关掉改走服务端中转 |
| 校验 HTTPS 证书 | 自签证书内网可关闭 |
| 超时 | 大文件上传建议 300s+ |

保存后点 **「测试 OpenList」**（登录 → 列根目录 → 写探针文件再删除）。文件按 `根目录/年/月/<uuid>_<文件名>` 存放。

对接的接口（与 [OpenList API 文档](https://doc.oplist.org.cn/api/apidocs) 一致）：

| 用途 | 接口 |
|------|------|
| 登录换 token | `POST /api/auth/login`（`username` / `password` / `otp_code`） |
| 建目录 | `POST /api/fs/mkdir` |
| 上传 | `PUT /api/fs/put`，头 `File-Path: <URL 编码后的完整路径>` |
| 取直链 sign | `POST /api/fs/get` → `data.sign` → `GET /d/<路径>?sign=<sign>` |
| 列目录 | `POST /api/fs/list` |
| 删除 | `POST /api/fs/remove`（`dir` + `names`） |

> OpenList 4.x 的 `/d/` 直链必须带 `sign`，不再允许匿名裸链；本系统会在每次下载前实时调 `fs/get` 取签名。

### 3.3 WebDAV

适用于 Alist / 群晖 / Nextcloud / 坚果云 / 飞牛自建 WebDAV。

| 字段 | 说明 |
|------|------|
| 启用 WebDAV | 勾选 |
| 地址 | 完整 DAV 入口，如 `http://IP:5244/dav`、`https://dav.jianguoyun.com/dav` |
| 用户名 / 密码 | WebDAV 账号；坚果云用**应用密码** |
| 根目录 | 默认 `/文件流转`，不存在会自动创建 |
| 校验 HTTPS 证书 | 自签证书内网可关闭 |
| 超时 | 大文件上传建议 300s+ |

保存后点 **「测试 WebDAV 连接」**（会先写探针文件再删除）。文件按 `根目录/年/月/` 存放，下载经服务端流式转发并支持 Range。

### 3.4 本地磁盘

适用于 NAS 本机盘 / 挂载共享目录 / 内网无云盘场景。

| 字段 | 说明 |
|------|------|
| 启用本地存储 | 勾选 |
| 存储目录 | 容器内路径，默认 `/app/storage`；需可写 |

Docker 部署时把宿主机目录挂到该路径，例如 `-v /vol1/1000/fts-storage:/app/storage`。下载经服务端流式转发并支持 Range。

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

## 5. 远程部署到飞牛 NAS（fnos / ARM64）

NAS 通常是 **aarch64**，而开发机多为 **x86_64**。直接 `docker compose up -d --build` 有两个坑：

1. 交叉架构：在 x86 机器上构建出的镜像到 NAS 上启动会直接报
   `exec /entrypoint.sh: exec format error`。
2. NAS 直连 Docker Hub / PyPI 不稳定，`--build` 常在 `pip install` 阶段失败。

因此推荐**在开发机构建 arm64 镜像 → 导入 NAS → 只重建容器**。仓库内置脚本封装了全流程：

```bash
# 全流程：交叉构建 + 同步源码 + 导入镜像 + 重建容器 + 健康校验
./scripts/deploy-nas.sh

# 只换镜像不改源码（快速发版）
./scripts/deploy-nas.sh --no-source

# 复用上次构建的镜像包，跳过构建
./scripts/deploy-nas.sh --no-build
```

脚本用到的可配置环境变量（都有默认值，按需覆盖）：

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `NAS_HOST` | `nas.zx.al` | NAS 地址 |
| `NAS_PORT` | `5666` | fnos 服务端口 |
| `NAS_APP_DIR` | `/vol1/1000/docker/file-transfer-system` | NAS 上的部署目录 |
| `NAS_TMP_DIR` | `/vol1/1000/uploads` | NAS 上的中转目录 |
| `TARGET_PLATFORM` | `linux/arm64` | NAS 架构；x86 NAS 改 `linux/amd64` |
| `HEALTH_URL` | `http://<NAS_HOST>:8790/health` | 健康检查地址 |
| `FNOS_CLI` | 自动探测 | `fnos-cli` 可执行文件路径 |

依赖：`docker`（含 buildx）、`curl`、`fnos-cli`。

### 关键技术点

- **必须显式 `--platform`**：脚本按 `TARGET_PLATFORM` 交叉构建。x86 开发机首次跨架构构建需要 QEMU，脚本会自动尝试注册 binfmt；失败时手工执行：
  ```bash
  docker run --privileged --rm tonistiigi/binfmt --install arm64
  ```
- **构建必须带 `--provenance=false --sbom=false`**：否则 `docker save` 产出带 attestation 的
  manifest list，老版本 dockerd `docker load` 后可能选中错误平台条目，同样报
  `exec format error`。
- **镜像导入与 compose 重建在 NAS 端执行**：`fnos-cli` 的 `docker container` 没有 `exec` / `logs`
  子命令，脚本借用 `docker.fnnas.com/library/docker:cli` 镜像挂载 `docker.sock` 起一次性容器完成。
- **重建时绝不加 `--build`**：镜像已由 `docker load` 导入，compose 只做容器重建。
- **源码同步不会覆盖运行时数据**：`data/`、`storage/`、`logs/` 被显式排除，SQLite 与配置保持不变。

### 手工等价步骤

脚本出问题时可逐步手工执行：

```bash
# 1. 构建 arm64 镜像并导出 tar
docker buildx build --platform linux/arm64 --provenance=false --sbom=false \
  -t file-transfer-system:latest \
  --output type=docker,dest=/tmp/fts-arm64.tar .

# 2. 上传镜像
fnos-cli file upload /vol1/1000/uploads /tmp/fts-arm64.tar --overwrite replace --yes

# 3. 在 NAS 上 docker load（借一次性容器）
#    需要挂载 /var/run/docker.sock、/vol1/1000/uploads 和部署目录
#    命令：load -i /uploads/fts-arm64.tar  →  再 tag 成 file-transfer-system:latest

# 4. 重建容器（注意不要带 --build）
docker compose -f /vol1/1000/docker/file-transfer-system/docker-compose.yml \
  --project-directory /vol1/1000/docker/file-transfer-system up -d

# 5. 验证
curl http://<NAS_HOST>:8790/health
```

### 排障：容器反复重启且 `ExitCode 255`

```bash
docker logs --tail 50 file-transfer-system
```

如果看到 `exec /entrypoint.sh: exec format error`，就是**镜像架构与 NAS 不匹配**，
用上面的 `--platform` 重建即可。确认镜像架构：

```bash
docker image inspect file-transfer-system:latest --format '{{.Os}}/{{.Architecture}}'
# NAS 上应输出 linux/arm64
```

## 6. 本机 Python + systemd（无 Docker）

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

## 7. 数据持久化（重要）
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

## 8. 环境变量
| 变量 | 默认 | 说明 |
|------|------|------|
| `FTS_HOST` | `0.0.0.0` | 监听地址 |
| `FTS_PORT` | `8790` | 容器内端口 |
| `TZ` | `Asia/Shanghai` | 时区 |

业务配置（限额、云盘、管理员）在 **`data/config.json`** / 管理后台，不靠环境变量。

## 9. 反向代理（HTTPS 推荐）
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

## 10. 健康检查
```bash
curl -fsS http://127.0.0.1:8790/health
# {"ok":true,"service":"file-transfer-system"}
```

Compose / Dockerfile 已内置 healthcheck。

## 11. 常用运维
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

## 12. 安全清单
1. 改默认管理员密码（后台）
2. 不要把 `data/config.json` 提交 Git / 打进镜像
3. 公网用 HTTPS 反代；防火墙只放行 80/443 或指定端口
4. 按需收紧扩展名、单文件大小、最长有效期
5. 发现危险文件：后台包裹点「封禁 IP」，该 IP 无法再上传
6. 云盘鉴权只用后台配置（Cookie+账密 或 Authorization），勿明文进仓库
7. 根目录可用显示路径 `/文件流转`（自动解析 parentFileId）

## 13. 故障排查
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

## 14. 镜像推送到仓库（可选）
```bash
docker tag file-transfer-system:latest ghcr.io/<你的用户名>/file-transfer-system:latest
docker push ghcr.io/<你的用户名>/file-transfer-system:latest
```

或 Docker Hub：
```bash
docker tag file-transfer-system:latest <dockerhub用户>/file-transfer-system:latest
docker push <dockerhub用户>/file-transfer-system:latest
```
