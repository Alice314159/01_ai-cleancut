#!/usr/bin/env bash
# Run from any working directory. Keep this terminal open; Ctrl+C stops both services.
set -Eeuo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="$ROOT/backend/.venv/bin/python"
RUN_DIR="$ROOT/.run"
BACKEND_PID=''
FRONTEND_PID=''

fail() { printf '错误：%s\n' "$*" >&2; exit 1; }
usage() {
  printf '用法：./start.sh [--lan] [--check]\n'
  printf '  默认启动前后端，仅本机可访问，Ctrl+C 停止。\n'
  printf '  --lan   让同一 Wi-Fi 下的手机也能访问（HTTPS，自签名证书），用于手机测试。\n'
  printf '  --check 仅检查依赖与端口。\n'
}
CHECK_ONLY=0
LAN=0
for arg in "$@"; do
  case "$arg" in
    --check) CHECK_ONLY=1 ;;
    --lan) LAN=1 ;;
    -h|--help) usage; exit 0 ;;
    *) usage; exit 1 ;;
  esac
done

# 局域网地址：可用 CLEANCUT_LAN_IP 手动指定（多网卡时）。
lan_ip() {
  if [[ -n "${CLEANCUT_LAN_IP:-}" ]]; then printf '%s' "$CLEANCUT_LAN_IP"; return; fi
  local ip=''
  if command -v ipconfig >/dev/null 2>&1; then   # macOS
    ip="$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || true)"
  fi
  if [[ -z "$ip" ]] && command -v hostname >/dev/null 2>&1; then
    ip="$(hostname -I 2>/dev/null | awk '{print $1}' || true)"
  fi
  if [[ -z "$ip" ]] && command -v ip >/dev/null 2>&1; then
    ip="$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for (i=1;i<NF;i++) if ($i=="src") print $(i+1)}' || true)"
  fi
  printf '%s' "$ip"
}

# 自签名证书：手机浏览器只在 HTTPS 下允许录音。IP 变化时重新生成。
ensure_cert() {
  local dir="$RUN_DIR/lan-cert" ip="$1"
  mkdir -p "$dir"
  if [[ -f "$dir/cert.pem" && -f "$dir/key.pem" && "$(cat "$dir/ip" 2>/dev/null)" == "$ip" ]]; then return; fi
  openssl req -x509 -newkey rsa:2048 -nodes -sha256 -days 825 \
    -keyout "$dir/key.pem" -out "$dir/cert.pem" -subj "/CN=AI CleanCut ($ip)" \
    -addext "subjectAltName=IP:$ip,IP:127.0.0.1,DNS:localhost" >/dev/null 2>&1 \
    || fail '生成 HTTPS 证书失败，请确认已安装 openssl（1.1.1 及以上）。'
  printf '%s' "$ip" >"$dir/ip"
}

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
LAN_IP=''
if [[ "$LAN" == 1 ]]; then
  command -v openssl >/dev/null 2>&1 || fail '局域网模式需要 openssl。'
  LAN_IP="$(lan_ip)"
  [[ -n "$LAN_IP" ]] || fail '没有找到本机的局域网地址；请连上 Wi-Fi，或用 CLEANCUT_LAN_IP=192.168.x.x ./start.sh --lan 指定。'
fi
printf '依赖与端口检查通过。\n'
[[ "$CHECK_ONLY" != 1 ]] || exit 0
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
FRONT_SCHEME=http
if [[ "$LAN" == 1 ]]; then
  ensure_cert "$LAN_IP"
  FRONT_SCHEME=https
  (
    cd "$ROOT/frontend"
    # 手机通过前端地址访问；/api 与 /ws 由 Vite 代理到只监听本机的后端。
    export VITE_API_BASE='' CLEANCUT_HTTPS_KEY="$RUN_DIR/lan-cert/key.pem" CLEANCUT_HTTPS_CERT="$RUN_DIR/lan-cert/cert.pem"
    exec node node_modules/vite/bin/vite.js --host 0.0.0.0 --port 5173 --strictPort
  ) >"$RUN_DIR/frontend.log" 2>&1 &
else
(
  cd "$ROOT/frontend"
  # Start Vite directly so this PID is the server, not an npm wrapper.
  exec node node_modules/vite/bin/vite.js --host 127.0.0.1 --port 5173 --strictPort
) >"$RUN_DIR/frontend.log" 2>&1 &
fi
FRONTEND_PID=$!
printf '正在启动，日志：%s\n' "$RUN_DIR"
ready=0
for ((attempt=0; attempt<60; attempt++)); do
  if ! kill -0 "$BACKEND_PID" 2>/dev/null || ! kill -0 "$FRONTEND_PID" 2>/dev/null; then
    tail -n 20 "$RUN_DIR/backend.log" "$RUN_DIR/frontend.log" >&2
    fail '服务启动失败，请查看上面的日志。'
  fi
  if curl --noproxy '*' -fsS --max-time 1 http://127.0.0.1:8001/api/health >/dev/null 2>&1 &&
     curl --noproxy '*' -kfsS --max-time 1 "$FRONT_SCHEME://127.0.0.1:5173/" >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 1
done
[[ "$ready" == 1 ]] || fail '服务在 60 次检查后仍未就绪，请查看 .run 下的日志。'
if [[ "$LAN" == 1 ]]; then
  PHONE_URL="https://$LAN_IP:5173/"
  printf '\nAI CleanCut 已启动（局域网模式）\n'
  printf '  电脑：https://127.0.0.1:5173/\n'
  printf '  手机：%s   （手机与电脑需连同一个 Wi-Fi）\n\n' "$PHONE_URL"
  if command -v qrencode >/dev/null 2>&1; then qrencode -t ANSIUTF8 "$PHONE_URL"; fi
  printf '首次打开会提示“连接不是私密连接/证书不受信任”：这是本机生成的自签名证书，\n'
  printf '  · Android Chrome：点“高级” → “继续前往”；\n'
  printf '  · iPhone Safari：点“显示详细信息” → “访问此网站”。\n'
  printf '打开后可“添加到主屏幕”，之后像 App 一样从桌面启动。\n'
  printf '保持此终端打开；按 Ctrl+C 同时停止前后端。\n'
else
  printf '\nAI CleanCut 已启动：http://127.0.0.1:5173/\n保持此终端打开；按 Ctrl+C 同时停止前后端。\n'
fi
while kill -0 "$BACKEND_PID" 2>/dev/null && kill -0 "$FRONTEND_PID" 2>/dev/null; do
  sleep 1
done
fail '有一个服务退出，正在停止另一服务。请查看 .run 下的日志。'
