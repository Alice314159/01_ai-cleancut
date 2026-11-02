#!/usr/bin/env bash
# Run from any working directory. Keep this terminal open; Ctrl+C stops both services.
set -Eeuo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="$ROOT/backend/.venv/bin/python"
RUN_DIR="$ROOT/.run"
BACKEND_PID=''
FRONTEND_PID=''

fail() { printf '错误：%s\n' "$*" >&2; exit 1; }
usage() { printf '用法：./start.sh [--check]\n  默认启动前后端，Ctrl+C 停止。\n  --check 仅检查依赖与端口。\n'; }
case "${1:-}" in
  '') ;;
  --check) ;;
  -h|--help) usage; exit 0 ;;
  *) usage; exit 1 ;;
esac
for command in npm node ffmpeg curl lsof; do
  command -v "$command" >/dev/null 2>&1 || fail "缺少 $command，请先安装。"
done
[[ -x "$PYTHON" ]] || fail '缺少 backend/.venv，请按 README 创建虚拟环境并安装后端依赖。'
[[ -d "$ROOT/frontend/node_modules/vite" ]] || fail '前端依赖未安装，请在 frontend 目录执行 npm ci。'
"$PYTHON" -c 'import uvicorn, fastapi, sqlalchemy, numpy, multipart' || fail '后端依赖不完整，请在 backend 虚拟环境安装 requirements.txt。'
for port in 8001 5173; do
  if lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
    lsof -nP -iTCP:"$port" -sTCP:LISTEN >&2
    fail "端口 $port 已被占用。若程序已启动，请直接打开 http://127.0.0.1:5173/；需要重启时先在原终端按 Ctrl+C。"
  fi
done
printf '依赖与端口检查通过。\n'
[[ "${1:-}" != '--check' ]] || exit 0
mkdir -p "$RUN_DIR"
# A directory lock prevents two launchers racing before the ports have been bound.
mkdir "$RUN_DIR/launcher.lock" 2>/dev/null || fail '已有启动脚本正在运行；若之前异常退出，请确认旧服务已停止，再删除 .run/launcher.lock 空目录。'
cleanup() {
  trap - EXIT INT TERM
  for pid in "$BACKEND_PID" "$FRONTEND_PID"; do
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
      kill -TERM "$pid" 2>/dev/null || true
    fi
  done
  for pid in "$BACKEND_PID" "$FRONTEND_PID"; do
    [[ -z "$pid" ]] || wait "$pid" 2>/dev/null || true
  done
  rmdir "$RUN_DIR/launcher.lock" 2>/dev/null || true
  printf '\n前后端已停止。\n'
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
(
  cd "$ROOT/backend"
  exec "$PYTHON" -m uvicorn app.main:app --host 127.0.0.1 --port 8001
) >"$RUN_DIR/backend.log" 2>&1 &
BACKEND_PID=$!
(
  cd "$ROOT/frontend"
  # Start Vite directly so this PID is the server, not an npm wrapper.
  exec node node_modules/vite/bin/vite.js --host 127.0.0.1 --port 5173 --strictPort
) >"$RUN_DIR/frontend.log" 2>&1 &
FRONTEND_PID=$!
printf '正在启动，日志：%s\n' "$RUN_DIR"
ready=0
for ((attempt=0; attempt<60; attempt++)); do
  if ! kill -0 "$BACKEND_PID" 2>/dev/null || ! kill -0 "$FRONTEND_PID" 2>/dev/null; then
    tail -n 20 "$RUN_DIR/backend.log" "$RUN_DIR/frontend.log" >&2
    fail '服务启动失败，请查看上面的日志。'
  fi
  if curl --noproxy '*' -fsS --max-time 1 http://127.0.0.1:8001/api/health >/dev/null 2>&1 &&
     curl --noproxy '*' -fsS --max-time 1 http://127.0.0.1:5173/ >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 1
done
[[ "$ready" == 1 ]] || fail '服务在 60 次检查后仍未就绪，请查看 .run 下的日志。'
printf '\nAI CleanCut 已启动：http://127.0.0.1:5173/\n保持此终端打开；按 Ctrl+C 同时停止前后端。\n'
while kill -0 "$BACKEND_PID" 2>/dev/null && kill -0 "$FRONTEND_PID" 2>/dev/null; do
  sleep 1
done
fail '有一个服务退出，正在停止另一服务。请查看 .run 下的日志。'
