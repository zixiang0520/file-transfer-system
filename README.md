# 文件流转系统 (File Transfer System)

深色主题的临时文件分享服务：多文件一次上传生成**一个提取码**，对方凭码查看文件列表，支持单下 / 勾选下载 / 全部 ZIP；可设有效期与提取次数，次数用尽或过期后自动销毁。

> 状态：**已完成**。支持四种可切换存储后端：**移动云盘 (yun139 personal_new)**、**OpenList**（AList 分支，聚合 OneDrive / 阿里云盘 / 115 / 移动云盘…）、**WebDAV**（Alist / 群晖 / Nextcloud / 坚果云…）、**本地磁盘**（NAS 本机盘 / 挂载目录）。已上传文件按各自后端读取，切换后端不影响旧包裹。

仓库：https://github.com/zixiang0520/file-transfer-system

## 核心功能

- 前台拖拽多文件上传 → 生成**一个**提取码
- 有效期：可选**小时 / 天**；后台可设**最长有效期（天）**
- 提取次数：`0=不限`，用尽后自动销毁
- 提取页：全选 / 复选框 / 「下载已选择的文件」 / 全部 ZIP / 单文件下载
- 管理后台：登录、改管理员账号密码、包裹管理、**上传 IP 记录 / 封禁**、上传限额、云盘绑定、清理过期包裹
- 前台 / 提取 / 后台均做**手机平板适配**
- **可切换存储后端**：移动云盘 / OpenList / WebDAV / 本地磁盘；后台一键测试连接
- **不做 QQ 机器人**（纯 Web）

## 存储后端

后台 → **存储后端** 选择，保存后新上传生效；旧文件仍按 `files.storage_backend` 读取。

| 后端 | 适用场景 | 上传方式 | 下载方式 |
|------|----------|----------|----------|
| `yun139` | 移动云盘账号，公网可用、不占服务器带宽 | 浏览器分片直传云盘（推荐）/ 服务器中转 | 302 跳转云盘直链 |
| `openlist` | 已部署 OpenList 并挂好网盘（OneDrive / 阿里云盘 / 115 / 移动云盘…） | 服务器流式中转（`PUT /api/fs/put`） | 302 跳转 `/d/…?sign=…` 直链，或服务端流式转发 |
| `webdav` | Alist、群晖、Nextcloud、坚果云、飞牛自建 | 服务器流式中转（`PUT`） | 服务端流式转发，支持 Range |
| `local` | NAS 本机盘、挂载共享目录、内网无云盘 | 服务器流式落盘 | 服务端流式转发，支持 Range |

- 本地存储目录：`storage.local.root`（默认 `/app/storage`，Docker 需挂载宿主机目录）
- OpenList / WebDAV / 本地文件统一按 `根目录/年/月/<uuid>_<文件名>` 存放，目录不存在自动创建
- OpenList 走官方 REST API：登录拿 token → `POST /api/fs/mkdir` 建目录 → `PUT /api/fs/put` 上传 →
  `POST /api/fs/get` 取 `sign` 生成 `/d/…` 直链 → `POST /api/fs/remove` 删除
- WebDAV / 本地下载都经服务端中转（WebDAV 需要 Basic 认证，不能直接暴露直链），支持断点续传/Range

## OpenList 后端

先在 OpenList 里把网盘挂载好（OpenList 负责驱动各种云盘），本系统只当「上传/下载/删除」的客户端：

| 字段 | 说明 |
|------|------|
| 地址 | OpenList 站点根地址，如 `http://192.168.1.10:5244`、`https://pan.example.com`（不要带 `/api`） |
| 用户名 / 密码 | OpenList 账号；系统自动 `POST /api/auth/login` 拿 token，401 时自动重登 |
| 两步验证码 | 账号开了 2FA 时填当前 6 位码 |
| Token | 可选，优先于账密；`Authorization: <token>`，默认 48 小时过期，仅填 token 时过期需手更 |
| 根目录 | 默认 `/文件流转`，不存在会自动创建 |
| 直链下载（302） | 默认开：下载直接 302 到 `OpenList/d/路径?sign=…`，不占本机带宽；服务器在内网时浏览器需能访问该地址，否则关掉改走中转 |
| 超时 | 单次 API / 上传超时，默认 300 秒 |

```bash
# 用 curl 验证 OpenList 侧是否通（把地址/账号换成你的）
TOKEN=$(curl -s -X POST http://127.0.0.1:5244/api/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"***"}' | jq -r .data.token)
curl -s -X POST http://127.0.0.1:5244/api/fs/list -H "Authorization: $TOKEN" \
  -H 'Content-Type: application/json' -d '{"path":"/","page":1,"per_page":10}'
```

## 移动云盘（yun139）

### 根目录

支持两种写法：

- 真实 `parentFileId`（如 `/`）
- **显示路径**：`/文件流转`、`文件流转/子目录` —— 按名逐级查找，不存在则自动创建，并缓存 `resolved_folder_id`

### 鉴权（对齐 OpenList，推荐长期方案）

参考：[OpenList 中国移动云盘](https://doc.oplist.org.cn/guide/drivers/139)

| 字段 | 必填 | 说明 |
|------|------|------|
| **邮箱 Cookie** | 长期推荐 | 登录 [mail.10086.cn](https://mail.10086.cn/) 后，复制浏览器 **Cookie 头**（一行 `k=v; k2=v2`）。必须含 **`Os_SSo_Sid`**、**`RMKEY`** |
| **用户名** | 长期推荐 | 手机号或邮箱 |
| **密码** | 长期推荐 | 139 账号密码 |
| Authorization | 可选 | yun.139.com 抓包 Basic **后面**的内容；有 Cookie+账密时可留空，由系统自动生成 |

**自动续期逻辑（与 OpenList 一致）：**

1. 有 Authorization 时先尝试 `authTokenRefresh` 刷新  
2. 刷新失败 / 上传返回 `认证失败(05050006)` → 用 **邮箱 Cookie + 用户名 + 密码** 三步登录（mail 登录 → getArtifact → yun thirdlogin）重新拿 token  
3. 新 Authorization / Cookie 写回 `data/config.json`，业务自动重试  

也可只填 Authorization（最快上手），但失效后需手动更新。

### 后台怎么填

1. 打开 `/admin` → **移动云盘绑定**  
2. 勾选启用  
3. 粘贴邮箱 Cookie、用户名、密码（Authorization 可空）  
4. 根目录填 `/` 或 `/文件流转`  
5. 保存 → 点 **「测试连接 / 自动续期」**

敏感信息只存在服务器 `data/config.json`（已 gitignore），**不要**写进仓库 / Docker 镜像 / 聊天记录。

## 快速启动（本机 Python）

```bash
git clone https://github.com/zixiang0520/file-transfer-system.git
cd file-transfer-system
python3 -m venv .venv
# 无 pip 时可用: uv pip install --python .venv/bin/python -r requirements.txt
.venv/bin/pip install -r requirements.txt
.venv/bin/python main.py --host 0.0.0.0 --port 8790
```

访问：

- 前台：http://127.0.0.1:8790  
- 提取：http://127.0.0.1:8790/extract  
- 管理后台：http://127.0.0.1:8790/admin  

默认管理员：`admin` / `admin123`（**上线请立刻改密**）。

### systemd 保活（生产建议）

```bash
# 示例 unit：/etc/systemd/system/file-transfer-system.service
# WorkingDirectory=/path/to/file-transfer-system
# ExecStart=/path/to/file-transfer-system/.venv/bin/python main.py --host 0.0.0.0 --port 8790
# Restart=always
sudo systemctl enable --now file-transfer-system
```

可选每日校验鉴权：

```bash
# scripts/refresh-139.sh — 调用 test_connection，失败 exit 1
0 0 * * * /path/to/file-transfer-system/scripts/refresh-139.sh >> /path/to/logs/139-refresh.log 2>&1
```

## Docker 部署

详细见 **[docs/DEPLOY.md](docs/DEPLOY.md)**。

```bash
git clone https://github.com/zixiang0520/file-transfer-system.git
cd file-transfer-system
mkdir -p data storage logs
docker compose up -d --build
```

公网地址在后台填 **公网地址**（`site.public_base_url`）。

### 部署到 arm64 NAS（飞牛 fnos 等）

NAS 多为 aarch64，开发机多为 x86_64，直接在 NAS 上 `--build` 容易在拉取依赖阶段失败，
且跨架构镜像会报 `exec format error`。用内置脚本在开发机构建 arm64 镜像再导入：

```bash
./scripts/deploy-nas.sh              # 构建 + 同步源码 + 导入镜像 + 重建容器
./scripts/deploy-nas.sh --no-source  # 只发版镜像
```

详见 [docs/DEPLOY.md 第 5 节](docs/DEPLOY.md)。

## 配置项

运行时：`data/config.json`（gitignore；也可后台改）。

| 项 | 说明 |
|-----|------|
| `site.public_base_url` | 公网地址，用于生成提取链接 |
| `upload.max_file_size_mb` | 单文件上限 |
| `upload.max_files_per_package` | 单次最多文件数 |
| `upload.default_expire_hours` | 默认有效期（小时） |
| `upload.max_expire_days` | 最长有效期（天） |
| `storage.backend` | 存储后端：`yun139` / `openlist` / `webdav` / `local`（默认 `yun139`） |
| `storage.openlist.enabled` / `url` / `username` / `password` / `otp_code` | OpenList 连接配置 |
| `storage.openlist.token` / `token_saved_at` | OpenList 登录后缓存的 token（自动写入） |
| `storage.openlist.root` / `direct_link` / `verify_ssl` / `timeout` | OpenList 根目录、直链开关、证书校验、超时 |
| `storage.local.enabled` / `root` | 本地磁盘开关与目录 |
| `storage.webdav.enabled` / `url` / `username` / `password` / `root` / `verify_ssl` / `timeout` | WebDAV 连接配置 |
| `storage.yun139.enabled` | 是否启用云盘 |
| `storage.yun139.authorization` | Basic 后 token（可自动生成/刷新） |
| `storage.yun139.mail_cookies` | 邮箱 Cookie |
| `storage.yun139.username` / `password` | 密码登录回退 |
| `storage.yun139.root_folder_id` | `/` 或显示路径如 `/文件流转` |
| `storage.yun139.resolved_folder_id` | 自动缓存的真实 parentFileId |
| `proxy.http` / `https` | 出站代理（httpx `proxy=`） |

## API 摘要

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/health` | 健康检查 |
| POST | `/api/upload` | multipart `files` + `expire_hours` + `max_extracts` |
| GET | `/api/package/{code}` | 包裹公开信息 |
| GET | `/api/download/{code}/{file_id}` | 单文件 |
| GET | `/api/download-selected/{code}` | 勾选 ZIP |
| GET | `/api/download-all/{code}` | 全部 ZIP |
| GET | `/api/upload-mode` | 前端：当前后端与是否支持浏览器直传 |
| POST | `/api/yun139/test` | 后台：测试连接 / 触发续期（需登录） |
| POST | `/api/storage/openlist/test` | 后台：测试 OpenList 连接（登录 + 列目录 + 写探针，需登录） |
| POST | `/api/storage/webdav/test` | 后台：测试 WebDAV 连接（需登录） |
| POST | `/api/storage/local/test` | 后台：测试本地目录可写（需登录） |
| GET | `/api/packages` | 后台：包裹列表（含 `uploader_ip`） |
| POST | `/api/packages/{id}/ban-ip` | 后台：封禁该包裹上传 IP，可选删包裹 |
| GET/POST/DELETE | `/api/banned-ips` | 后台：封禁名单（DELETE `?ip=`） |

## 目录结构

```
file-transfer-system/
├── main.py
├── requirements.txt          # 含 cryptography（登录加解密）
├── Dockerfile / docker-compose.yml
├── docs/DEPLOY.md
├── scripts/refresh-139.sh    # 定时校验云盘鉴权
├── scripts/deploy-nas.sh     # 一键部署到 arm64 NAS（交叉构建 + 导入 + 重建）
├── app/
│   ├── config_store.py
│   ├── core/
│   │   ├── storage.py        # 存储后端分派 + yun139 上传/下载
│   │   ├── yun139_auth.py    # OpenList 式刷新 + 密码登录
│   │   ├── openlist.py       # OpenList 后端（REST API 上传/直链/删除）
│   │   ├── webdav.py         # WebDAV 后端（流式上传/下载）
│   │   ├── localstore.py     # 本地磁盘后端（流式落盘/下载）
│   │   ├── transfer.py
│   │   └── db.py
│   └── web/templates/        # index / extract / admin（移动端）
├── data/                     # config.json + transfers.db（勿提交）
└── logs/
```

## 故障排查

| 现象 | 处理 |
|------|------|
| `认证失败(05050006)` | 配齐 Cookie+账密后点测试连接；或更新 Authorization |
| 测试连接提示缺 RMKEY | Cookie 不完整，从 mail.10086.cn 重新复制整段 Cookie |
| 上传 413 | 反代 `client_max_body_size` 过小 |
| 容器重启、日志见 `exec format error` | 镜像架构与 NAS 不符，用 `--platform linux/arm64` 重建（见 DEPLOY 第 5 节） |
| 服务挂了 | `systemctl status file-transfer-system` / `Restart=always` |

## 许可证

MIT License
