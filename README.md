# 文件流转系统 (File Transfer System)

深色主题的临时文件分享服务：多文件一次上传生成**一个提取码**，对方凭码查看文件列表，支持单下 / 勾选下载 / 全部 ZIP；可设有效期与提取次数，次数用尽或过期后自动销毁。

> 状态：**已完成**。仅 **移动云盘 (yun139 personal_new)** 存储，无本地落盘；鉴权对齐 [OpenList 139](https://doc.oplist.org.cn/guide/drivers/139)。

仓库：https://github.com/zixiang0520/file-transfer-system

## 核心功能

- 前台拖拽多文件上传 → 生成**一个**提取码
- 有效期：可选**小时 / 天**；后台可设**最长有效期（天）**
- 提取次数：`0=不限`，用尽后自动销毁
- 提取页：全选 / 复选框 / 「下载已选择的文件」 / 全部 ZIP / 单文件下载
- 管理后台：登录、改管理员账号密码、包裹管理、上传限额、云盘绑定、清理过期包裹
- 前台 / 提取 / 后台均做**手机平板适配**
- **不做 QQ 机器人**（纯 Web）

## 移动云盘

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

## 配置项

运行时：`data/config.json`（gitignore；也可后台改）。

| 项 | 说明 |
|-----|------|
| `site.public_base_url` | 公网地址，用于生成提取链接 |
| `upload.max_file_size_mb` | 单文件上限 |
| `upload.max_files_per_package` | 单次最多文件数 |
| `upload.default_expire_hours` | 默认有效期（小时） |
| `upload.max_expire_days` | 最长有效期（天） |
| `storage.backend` | 固定 `yun139` |
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
| POST | `/api/yun139/test` | 后台：测试连接 / 触发续期（需登录） |

## 目录结构

```
file-transfer-system/
├── main.py
├── requirements.txt          # 含 cryptography（登录加解密）
├── Dockerfile / docker-compose.yml
├── docs/DEPLOY.md
├── scripts/refresh-139.sh    # 定时校验云盘鉴权
├── app/
│   ├── config_store.py
│   ├── core/
│   │   ├── storage.py        # yun139 上传/下载/路径解析
│   │   ├── yun139_auth.py    # OpenList 式刷新 + 密码登录
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
| 服务挂了 | `systemctl status file-transfer-system` / `Restart=always` |

## 许可证

MIT License
