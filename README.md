# 文件流转系统 (File Transfer System)

深色主题的临时文件分享服务：多文件一次上传生成**一个提取码**，对方凭码查看文件列表，支持单下 / 勾选下载 / 全部 ZIP；可设有效期与提取次数，次数用尽或过期后自动销毁。

> 状态：**开发中 / 夜间稳定化前的可运行骨架**。本地存储可跑通；移动云盘直传下载仍在完善。

## 功能

- 前台拖拽多文件上传 → 生成提取码
- 有效期：小时 / 天；后台可设**最长有效期（天）**
- 提取次数：`0=不限`，用尽后自动销毁
- 提取页：全选 / 复选框 / 下载已选择 / 全部 ZIP / 单文件下载
- 管理后台：登录、包裹管理、上传限额、云盘绑定、清理过期
- QQ 机器人上传/查询接口（预留，需配置 AppID/Secret）

## 快速启动

```bash
cd file-transfer-system
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python main.py --host 0.0.0.0 --port 8790
```

- 前台：http://127.0.0.1:8790  
- 提取：http://127.0.0.1:8790/extract  
- 后台：http://127.0.0.1:8790/admin  

默认管理员：`admin` / `admin123`（首次启动自动写入 `data/config.json`，**上线请立刻改密**）。

## 配置

运行时配置在 `data/config.json`（已 gitignore）：

| 项 | 说明 |
|----|------|
| `site.public_base_url` | 公网地址，用于生成提取链接 |
| `upload.max_file_size_mb` | 单文件上限 |
| `upload.max_files_per_package` | 单次最多文件数 |
| `upload.default_expire_hours` | 默认有效期（小时） |
| `upload.max_expire_days` | 最长有效期（天） |
| `storage.backend` | `local` 或 `yun139` |
| `storage.yun139.authorization` | 移动云盘 Basic 后的 token |
| `qq.*` | QQ 机器人凭证 |

### 移动云盘（进行中）

参考 OpenList / 139 挂载：登录 [yun.139.com](https://yun.139.com) → 开发者工具抓 `hcy/file/list` 请求头 `Authorization`，只填 **Basic 后面**的内容。  
当前版本以**本地可跑通**为主；纯云盘直传/直下仍在迭代（计划：不落本地、上传直写 139、下载走云盘链接）。

## API 摘要

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/upload` | multipart `files` + `expire_hours` + `max_extracts` |
| GET | `/api/package/{code}` | 包裹公开信息 |
| GET | `/api/download/{code}/{file_id}` | 单文件 |
| GET | `/api/download-selected/{code}?ids=1,2` | 勾选 ZIP |
| GET | `/api/download-all/{code}` | 全部 ZIP |
| POST | `/api/bot/upload` | QQ 侧上传（需 token） |

## 目录结构

```
file-transfer-system/
├── main.py                 # FastAPI 入口
├── requirements.txt
├── app/
│   ├── config_store.py
│   ├── db.py               # SQLite 包裹/文件
│   ├── core/
│   │   ├── storage.py      # 本地 + 139 hooks
│   │   └── transfer.py     # 上传/提取/销毁
│   └── web/templates/      # 前台 / 提取 / 后台
├── data/                   # 运行时（不入库）
└── storage/                # 本地文件（不入库）
```

## 安全提示

- 不要把 `data/config.json`、真实 Authorization、QQ Secret 提交进仓库
- 公网部署请改管理员密码、限制端口/反代 HTTPS、按需收敛扩展名与大小
- 本项目用于临时文件中转，请遵守当地法律法规与运营商 TOS

## License

MIT
