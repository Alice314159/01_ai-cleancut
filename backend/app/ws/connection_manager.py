"""One recording writer per session; notification failures never abort processing."""
from fastapi import WebSocket, WebSocketDisconnect


class RecordingConnectionManager:
    def __init__(self) -> None:
        self._active: dict[str, WebSocket] = {}

    async def connect(self, session_id: str, ws: WebSocket, replace: bool = False) -> bool:
        previous = self._active.get(session_id)
        if previous is not None and not replace:
            await ws.close(code=4409)
            return False
        if previous is not None:
            # 续传接管：先登记新连接，再关掉旧连接；旧连接的收尾逻辑据此知道自己已不是写入方。
            self._active.pop(session_id, None)
            try:
                await previous.close(code=4000)
            except Exception:
                pass
        self._active[session_id] = ws
        try:
            await ws.accept()
        except Exception:
            self.disconnect(session_id, ws)
            raise
        return True

    def disconnect(self, session_id: str, ws: WebSocket | None = None) -> None:
        if ws is None or self._active.get(session_id) is ws:
            self._active.pop(session_id, None)

    async def send_json(self, session_id: str, payload: dict) -> None:
        ws = self._active.get(session_id)
        if ws is not None:
            try:
                await ws.send_json(payload)
            except (WebSocketDisconnect, RuntimeError, OSError):
                # The receive loop owns disconnect/finalization and removes this writer.
                pass

    def owns(self, session_id: str, ws: WebSocket) -> bool:
        return self._active.get(session_id) is ws

    def is_connected(self, session_id: str) -> bool:
        return session_id in self._active


manager = RecordingConnectionManager()
