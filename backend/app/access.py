"""
局域网访问配对码（--lan / --app 模式）。

设置了 CLEANCUT_ACCESS_TOKEN 时，所有 /api 与 /ws 请求都必须带配对码：
请求头 X-CleanCut-Token，或查询参数 token（<audio>/<img>/下载链接/WebSocket 无法加请求头时）。
未设置时（默认只在本机访问）不做任何校验。

配对码比较前去掉连字符与空白并转大写，用常量时间比较。
一分钟内错误超过 FAIL_LIMIT 次，接下来 LOCKOUT_S 秒内拒绝所有未带正确配对码的请求（已配对设备不受影响）；
经前端代理访问时所有请求来源都是 127.0.0.1，按来源 IP 限流没有意义，所以按全局失败次数计。
"""
from collections import deque
import hmac
import logging
import os
import re
import time

FAIL_WINDOW_S = 60
FAIL_LIMIT = 20
LOCKOUT_S = 60
OPEN_PATHS = {"/api/health"}

_failures: deque[float] = deque()
_locked_until = 0.0


def normalize(code: str | None) -> str:
    return re.sub(r"[\s-]", "", code or "").upper()


def configured_token() -> str:
    return normalize(os.environ.get("CLEANCUT_ACCESS_TOKEN"))


def enabled() -> bool:
    return bool(configured_token())


def presented(headers, query_params) -> str:
    return normalize(headers.get("x-cleancut-token") or query_params.get("token"))


def check(code: str) -> str:
    """返回 "ok" / "denied" / "locked"。"""
    global _locked_until
    expected = configured_token()
    if not expected:
        return "ok"
    if code and hmac.compare_digest(code.encode(), expected.encode()):
        return "ok"
    now = time.monotonic()
    if now < _locked_until:
        return "locked"
    _failures.append(now)
    while _failures and now - _failures[0] > FAIL_WINDOW_S:
        _failures.popleft()
    if len(_failures) > FAIL_LIMIT:
        _locked_until = now + LOCKOUT_S
        _failures.clear()
        return "locked"
    return "denied"


def reset_throttle() -> None:
    global _locked_until
    _failures.clear()
    _locked_until = 0.0


class RedactTokenFilter(logging.Filter):
    """访问日志里的 token=… 替换成 ***，配对码不落进日志文件。"""
    _pattern = re.compile(r"(token=)[^&\s\"']+", re.IGNORECASE)

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(self._pattern.sub(r"\1***", a) if isinstance(a, str) else a for a in record.args)
        if isinstance(record.msg, str):
            record.msg = self._pattern.sub(r"\1***", record.msg)
        return True
