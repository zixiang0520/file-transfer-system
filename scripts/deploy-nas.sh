#!/usr/bin/env bash
# 一键部署 file-transfer-system 到飞牛 NAS（fnos / aarch64）
#
# 设计背景：
#   1. NAS 是 aarch64，开发机多为 x86_64 —— 直接 `docker compose build` 交叉架构会
#      在容器启动时报 `exec format error`，必须显式 `--platform linux/arm64` 交叉构建。
#   2. NAS 上直连 Docker Hub / PyPI 不稳定，`compose up --build` 经常失败；
#      在开发机构建好镜像再导入更可靠。
#   3. fnos-cli 的 `docker container` 没有 exec/logs 子命令，临时任务统一用
#      `docker.fnnas.com/library/docker:cli` + docker.sock 挂载的一次性容器完成。
#
# 用法：
#   ./scripts/deploy-nas.sh                 # 全流程：构建 + 同步源码 + 重建容器
#   ./scripts/deploy-nas.sh --no-source     # 只更新镜像，不动源码（快速发版）
#   ./scripts/deploy-nas.sh --no-build      # 复用最近的本地 arm64 镜像 tar
#   ./scripts/deploy-nas.sh --platform linux/amd64
#
# 依赖：docker（含 buildx）、curl、fnos-cli（可用 FNOS_CLI 指定路径）
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# ---------- 可配置项 ----------
NAS_HOST="${NAS_HOST:-nas.zx.al}"
NAS_PORT="${NAS_PORT:-5666}"
NAS_SCHEME="${NAS_SCHEME:-http}"
NAS_PROFILE="${NAS_PROFILE:-home}"
# NAS 上的部署目录与中转目录
NAS_APP_DIR="${NAS_APP_DIR:-/vol1/1000/docker/file-transfer-system}"
NAS_TMP_DIR="${NAS_TMP_DIR:-/vol1/1000/uploads}"
# 用于执行 docker load / compose 的临时容器
NAS_DOCKER_IMAGE="${NAS_DOCKER_IMAGE:-docker.fnnas.com/library/docker:cli}"
PROJECT_NAME="${PROJECT_NAME:-file-transfer-system}"
IMAGE_TAG="${IMAGE_TAG:-${PROJECT_NAME}:latest}"
# 目标架构：NAS 为 aarch64/u64 → linux/arm64
TARGET_PLATFORM="${TARGET_PLATFORM:-linux/arm64}"
# 对外健康检查地址（宿主机视角）
HEALTH_URL="${HEALTH_URL:-http://${NAS_HOST}:8790/health}"
# 本地镜像 tar 存放位置（--no-build 复用）
IMAGE_TAR="${IMAGE_TAR:-/tmp/${PROJECT_NAME}-${TARGET_PLATFORM//\//-}.tar}"

SKIP_SOURCE=0
SKIP_BUILD=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-source) SKIP_SOURCE=1 ;;
    --no-build)  SKIP_BUILD=1 ;;
    --platform)  TARGET_PLATFORM="$2"; shift ;;
    --health-url) HEALTH_URL="$2"; shift ;;
    -h|--help)
      sed -n '2,18p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *) echo "未知参数: $1" >&2; exit 2 ;;
  esac
  shift
done

# ---------- fnos-cli 定位 ----------
if [[ -z "${FNOS_CLI:-}" ]]; then
  if command -v fnos-cli >/dev/null 2>&1; then
    FNOS_CLI="$(command -v fnos-cli)"
  else
    for cand in \
      /root/.octop/agents/*/.octop/skills/fnos-cli/scripts/fnos-cli \
      "$HOME"/.octop/agents/*/.octop/skills/fnos-cli/scripts/fnos-cli
    do
      [[ -x "$cand" ]] && { FNOS_CLI="$cand"; break; }
    done
  fi
fi
if [[ -z "${FNOS_CLI:-}" || ! -x "$FNOS_CLI" ]]; then
  echo "错误：找不到 fnos-cli，请设置 FNOS_CLI=/path/to/fnos-cli" >&2
  exit 1
fi

CLI_ARGS=(--profile "$NAS_PROFILE" --host "$NAS_HOST" --port "$NAS_PORT" --scheme "$NAS_SCHEME")
if [[ "$NAS_SCHEME" == "http" ]]; then
  CLI_ARGS+=(--allow-insecure-http)
fi

nas() { "$FNOS_CLI" "${CLI_ARGS[@]}" "$@"; }

log()  { printf '\033[1;34m[deploy]\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m[ ok ]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[fail]\033[0m %s\n' "$*" >&2; exit 1; }

# 一次性容器：创建 → 启动 → 等它跑完 → 删除。用于在 NAS 上执行 docker 命令。
run_once() {
  local name="$1"; shift
  local -a cmd_args=()
  for a in "$@"; do cmd_args+=(--cmd="$a"); done

  nas docker container rm "$name" --force --yes >/dev/null 2>&1 || true
  nas docker container create \
    --image "$NAS_DOCKER_IMAGE" \
    --name "$name" \
    --mount /var/run/docker.sock:/var/run/docker.sock \
    --mount "${NAS_TMP_DIR}:/uploads" \
    --mount "${NAS_APP_DIR}:${NAS_APP_DIR}" \
    "${cmd_args[@]}" --yes >/dev/null \
    || die "创建临时容器 $name 失败"
  nas docker container start "$name" --yes >/dev/null || die "启动临时容器 $name 失败"

  local status exit_code tries=0
  while :; do
    read -r status exit_code < <(
      nas docker container inspect "$name" 2>/dev/null | python3 -c '
import sys, json
d = json.load(sys.stdin)
c = d[0] if isinstance(d, list) else d
s = c.get("State", {})
print(s.get("Status", "unknown"), s.get("ExitCode", -1))
' 2>/dev/null || echo "unknown -1"
    )
    [[ "$status" == "exited" || "$status" == "dead" ]] && break
    tries=$((tries + 1))
    (( tries > 100 )) && { nas docker container rm "$name" --force --yes >/dev/null 2>&1 || true; die "临时容器 $name 超时"; }
    sleep 3
  done
  nas docker container rm "$name" --force --yes >/dev/null 2>&1 || true
  [[ "$exit_code" == "0" ]] || die "临时容器 $name 退出码 $exit_code"
}

# ---------- 1. 交叉构建镜像 ----------
if (( SKIP_BUILD )); then
  [[ -f "$IMAGE_TAR" ]] || die "--no-build 但镜像包不存在：$IMAGE_TAR"
  log "跳过构建，复用 $IMAGE_TAR"
else
  log "构建镜像 $IMAGE_TAG（platform=$TARGET_PLATFORM）"

  # 跨架构构建需要 binfmt 模拟器；缺失时尝试注册（失败不致命，本地同架构构建不需要）
  if [[ "$TARGET_PLATFORM" != "linux/$(docker info --format '{{.Architecture}}' 2>/dev/null || echo unknown)" ]]; then
    if ! ls /proc/sys/fs/binfmt_misc/ 2>/dev/null | grep -q 'aarch64\|arm64'; then
      log "注册 binfmt 模拟器（QEMU）"
      docker run --privileged --rm tonistiigi/binfmt --install arm64 >/dev/null 2>&1 \
        || log "警告：binfmt 注册失败，交叉构建可能不可用"
    fi
  fi

  # --provenance=false --sbom=false：避免导出带 attestation 的 manifest list，
  # 老版本 dockerd 加载后会选错平台条目，直接报 exec format error。
  docker buildx build \
    --platform "$TARGET_PLATFORM" \
    --provenance=false --sbom=false \
    -t "$IMAGE_TAG" \
    --output "type=docker,dest=${IMAGE_TAR}" \
    . || die "镜像构建失败"
  [[ -s "$IMAGE_TAR" ]] || die "镜像包为空：$IMAGE_TAR"
  ok "镜像已导出 $(du -h "$IMAGE_TAR" | cut -f1) → $IMAGE_TAR"
fi

# ---------- 2. 同步源码 ----------
if (( SKIP_SOURCE )); then
  log "跳过源码同步（--no-source）"
else
  log "打包并同步源码 → $NAS_APP_DIR"
  SRC_PKG="/tmp/${PROJECT_NAME}-src.tar"
  # 只打包运行所需文件；data/storage/logs 属于 NAS 端运行时数据，绝不覆盖
  tar -cf "$SRC_PKG" \
    --exclude=.git --exclude=.venv --exclude=__pycache__ --exclude='*.pyc' \
    --exclude=.ruff_cache --exclude=data --exclude=storage --exclude=logs \
    app main.py requirements.txt docker docs scripts README.md LICENSE \
    docker-compose.yml .env.example .dockerignore .gitignore \
    || die "源码打包失败"
  nas file upload "$NAS_TMP_DIR" "$SRC_PKG" --overwrite replace --yes >/dev/null \
    || die "源码上传失败"
  nas file extract "$NAS_TMP_DIR/$(basename "$SRC_PKG")" "$NAS_APP_DIR" \
    --overwrite replace --yes >/dev/null || die "NAS 端解包失败"
  nas file rm "$NAS_TMP_DIR/$(basename "$SRC_PKG")" --yes >/dev/null 2>&1 || true
  ok "源码已同步"
fi

# ---------- 3. 上传镜像并以 root 权限 load ----------
log "上传镜像 → $NAS_TMP_DIR"
nas file upload "$NAS_TMP_DIR" "$IMAGE_TAR" --overwrite replace --yes >/dev/null \
  || die "镜像上传失败"

log "在 NAS 上导入镜像"
run_once "${PROJECT_NAME}-loader" load -i "/uploads/$(basename "$IMAGE_TAR")"
# 保证 compose 用的 latest 指向刚导入的镜像
run_once "${PROJECT_NAME}-retag" tag "$IMAGE_TAG" "${PROJECT_NAME}:latest"
nas file rm "$NAS_TMP_DIR/$(basename "$IMAGE_TAR")" --yes >/dev/null 2>&1 || true
ok "镜像已导入并打上 latest"

# ---------- 4. 用新镜像重建容器 ----------
# 关键：不带 --build。镜像已在本地导入，compose 只做容器重建，不会触发 NAS 端构建。
log "重建容器（compose up -d，无 --build）"
# 旧容器若不是 compose 创建的（缺 com.docker.compose.project 标签），compose up
# 会直接报 "container name is already in use" 并退出码 1。镜像已在上一步导入，
# 先删掉这个游离容器，交给 compose 按 compose 文件重建。
existing_kind=$(nas docker container inspect "$PROJECT_NAME" 2>/dev/null | python3 -c '
import sys, json
try:
    d = json.load(sys.stdin)
except Exception:
    print("absent"); raise SystemExit
c = d[0] if isinstance(d, list) else d
labels = (c.get("Config") or {}).get("Labels") or {}
print("compose" if labels.get("com.docker.compose.project") else "foreign")
' 2>/dev/null || echo "absent")
if [[ "$existing_kind" == "foreign" ]]; then
  log "旧容器 $PROJECT_NAME 不是 compose 管理的，先删除再交给 compose 重建"
  run_once "${PROJECT_NAME}-cleanup" container rm --force "$PROJECT_NAME" \
    || die "删除旧容器 $PROJECT_NAME 失败"
fi
run_once "${PROJECT_NAME}-deployer" \
  compose --project-directory "$NAS_APP_DIR" \
  -f "$NAS_APP_DIR/docker-compose.yml" up -d

# ---------- 5. 健康校验 ----------
log "等待服务健康检查通过"
# 以容器自身的 docker healthcheck（镜像里定义，探测 127.0.0.1:8790）为准。
# 不要用 curl http://$NAS_HOST:8790：那条路落在 fts-tls 上，即使后端 502
# 它也会返回 301/502 页面，curl -f 对非 5xx 的 301 会误判为健康。
healthy=0
for _ in $(seq 1 40); do
  sleep 3
  st=$(nas docker container inspect "$PROJECT_NAME" 2>/dev/null | python3 -c '
import sys, json
try:
    d = json.load(sys.stdin)
    c = d[0] if isinstance(d, list) else d
    print(((c.get("State") or {}).get("Health") or {}).get("Status") or "none")
except Exception:
    print("none")
' 2>/dev/null || echo none)
  if [[ "$st" == "healthy" ]]; then
    healthy=1
    break
  fi
done
[[ "$healthy" == "1" ]] || {
  echo "--- 容器状态 ---" >&2
  nas docker container inspect "$PROJECT_NAME" 2>/dev/null | python3 -c '
import sys, json
d = json.load(sys.stdin)
c = d[0] if isinstance(d, list) else d
s = c.get("State", {})
h = s.get("Health") or {}
print("Image:", c.get("Image"))
print("Status:", s.get("Status"), "ExitCode:", s.get("ExitCode"))
print("Health:", h.get("Status"))
' >&2 || true
  die "健康检查失败：$HEALTH_URL"
}

# ---------- 6. 汇总 ----------
log "部署完成"
nas docker container inspect "$PROJECT_NAME" 2>/dev/null | python3 -c '
import sys, json
d = json.load(sys.stdin)
c = d[0] if isinstance(d, list) else d
s = c.get("State", {})
h = s.get("Health") or {}
name = (c.get("Name") or "").lstrip("/")
img = (c.get("Image") or "")[:19]
print("  容器: " + name)
print("  镜像: " + img)
print("  状态: {} / {}".format(s.get("Status"), h.get("Status")))
print("  重启: {}".format(c.get("RestartCount")))
' || true
ok "健康检查通过：$HEALTH_URL"
