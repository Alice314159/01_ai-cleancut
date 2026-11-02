#!/usr/bin/env bash
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
"$ROOT/start.sh"
result=$?
if [[ "$result" != 0 && "$result" != 130 && "$result" != 143 ]]; then
  printf '\n按回车关闭窗口…'
  read -r _ || true
fi
exit "$result"
