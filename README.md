# 文件流转系统 (File Transfer System)

深色主题的临时文件分享服务：多文件一次上传生成**一个提取码**，对方凭码查看文件列表，支持单下 / 勾选下载 / 全部 ZIP；可设有效期与提取次数，次数用尽或过期后自动销毁。

> 状态：**已完成**。支持移动云盘直传下载，无本地存储。

## 核心功能

- 前台拖拽多文件上传 → 生成**一个**提取码
- 有效期：可选**小时 / 天**；后台可设**最长有效期（天）**
- 提取次数：`0=不限`，用尽后自动销毁
- 提取页：全选 / 复选框 / 「下载已选择的文件」 / 全部 ZIP / 单文件下载
- 管理后台：登录、改管理员账号密码、包裹管理、上传限额、云盘绑定、清理过期包裹

## 移动云盘（已完成）

**根目录支持两种写法**：
- 真实 `parentFileId`（如 `/`）
- **显示路径**：`/文件流转`、`文件流转/子目录` —— **系统会自动按文件夹名查找，不存在则自动创建**，并返回真实 parentFileId 缓存

登录 [yun.139.com](https://yun.139.com) → 开发者工具抓 `hcy/file/list` 的 **Authorization**，只填 **Basic 后面的内容**。

## 快速启动（本机 Python）

```bash
cd file-transfer-system
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python main.py --host 0.0.0.0 --port 8790
```

访问：
- 前台：http://127.0.0.1:8790
- 提取：http://127.0.0.1:8790/extract
- 管理后台：http://127.0.0.1:8790/admin

默认管理员：`admin` / `admin123`（**上线请立刻在后台改密**）。

## Docker 部署（推荐）

详细见 **[docs/DEPLOY.md](docs/DEPLOY.md)**。

### 一键 Compose

```bash
git clone https://github.com/zixiang0520/file-transfer-system.git
cd file-transfer-system
mkdir -p data storage logs
docker compose up -d --build
```

公网地址请在后台填写 **公网地址**（`site.public_base_url`）。

## 配置

运行时配置在 `data/config.json`（已 gitignore，也可在管理后台改）。

| 项 | 说明 |
|-----|------|
| `site.public_base_url` | 公网地址，用于生成提取链接 |
| `upload.max_file_size_mb` | 单文件上限 |
| `upload.max_files_per_package` | 单次最多文件数 |
| `upload.default_expire_hours` | 默认有效期（小时） |
| `upload.max_expire_days` | 最长有效期（天） |
| `storage.backend` | `yun139`（已完成） |
| `storage.yun139.authorization` | 移动云盘 Basic 后的 token |
| `storage.yun139.root_folder_id` | 根目录（支持 `/` 或 `/文件流转` 等路径） |

## API 摘要

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/health` | 健康检查 |
| POST | `/api/upload` | multipart `files` + `expire_hours` + `max_extracts` |
| GET | `/api/package/{code}` | 包裹公开信息 |
| GET | `/api/download/{code}/{file_id}` | 单文件 |
| GET | `/api/download-selected/{code}` | 勾选 ZIP |
| GET | `/api/download-all/{code}` | 全部 ZIP |

## 目录结构

```
file-transfer-system/
├── main.py
├── Dockerfile / docker-compose.yml / docker/entrypoint.sh
├── app/
│   ├── core/ (storage.py, transfer.py, db.py)
│   ├── bot/qq_bot.py (预留)
│   ├── web/templates/ (index.html, admin.html, extract.html)
├── data/ (配置+数据库)
├── storage/ (云盘文件)
└── logs/ (日志)
```

## 许可证

MIT License
