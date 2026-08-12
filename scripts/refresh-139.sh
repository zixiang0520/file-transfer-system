#!/usr/bin/env bash
# 主动刷新/校验 yun139 Authorization（对齐 OpenList refresh + password fallback）
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
mkdir -p logs
export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"
PY="${ROOT}/.venv/bin/python"
if [[ ! -x "$PY" ]]; then
  PY=python3
fi
"$PY" - <<'PY'
from datetime import datetime
from app.core.storage import Yun139Client

print(datetime.now().isoformat(), "yun139 refresh check start")
c = Yun139Client()
if not c.enabled:
    print("SKIP: yun139 not enabled or credentials missing")
    raise SystemExit(0)
r = c.test_connection()
print(r)
if not r.get("ok"):
    raise SystemExit(1)
print("OK")
PY
