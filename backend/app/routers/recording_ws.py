import asyncio
import json

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app import db as db_module
from app.models import RecordingSession
from app.services import storage
from app.services.stub_pipeline import schedule_capture_stop, simulate_capture_signals
from app.ws.connection_manager import manager

router = APIRouter(tags=["recording"])
_stop_events: dict[str, asyncio.Event] = {}
_signal_tasks: set[asyncio.Task] = set()

# ── 断线续传 ──
# 客户端在 start 时声明 resumable，并以带序号的二进制帧上传：
#   b"CCK1" + 4 字节大端序号 + 音频数据
# 服务端按序写入并回 {"type":"ack","seq":n}；重复帧只回 ack 不重写，跳号回 {"type":"resync","nextSeq":n}。
# 声明过 resumable 的会话断线后不立即结束，而是进入 RECONNECTING，等待 RESUME_GRACE_SECONDS；
# 客户端用 ?resume=1 重连并发送 {"action":"reconnect","state":"RECORDING"|"PAUSED"}，
# 服务端回 {"type":"resume_from","nextSeq":n}，客户端从该序号补传。超时未重连则按已收到的内容结束。
CHUNK_MAGIC = b"CCK1"
RESUME_GRACE_SECONDS = 300
ACTIVE_CAPTURE = ("RECORDING", "PAUSED", "RECONNECTING")
_resumable: set[str] = set()
_grace_tasks: dict[str, asyncio.Task] = {}


def _cancel_grace(session_id: str) -> None:
    task = _grace_tasks.pop(session_id, None)
    if task is not None and task is not asyncio.current_task():
        task.cancel()


async def _expire_after_grace(session_id: str, seconds: float) -> None:
    await asyncio.sleep(seconds)
    _grace_tasks.pop(session_id, None)
    with db_module.SessionLocal() as db:
        session = db.get(RecordingSession, session_id)
        if session is not None and session.capture_status == "RECONNECTING":
            stop_recording(db, session_id)


def stop_recording(db, session_id: str) -> None:
    db.expire_all()
    session = db.get(RecordingSession, session_id)
    event = _stop_events.pop(session_id, None)
    if event is not None:
        event.set()
    _cancel_grace(session_id)
    _resumable.discard(session_id)
    if session is not None and session.capture_status in ACTIVE_CAPTURE:
        session.capture_status = "STOPPED"
        db.commit()
        schedule_capture_stop(session_id)


def recover_reconnecting_sessions() -> None:
    """服务重启时，等待重连的会话已无法续传（等待计时在内存里），按已收到的内容结束。"""
    with db_module.SessionLocal() as db:
        ids = [row[0] for row in db.query(RecordingSession.id).filter(RecordingSession.capture_status == "RECONNECTING").all()]
        for session_id in ids:
            stop_recording(db, session_id)


@router.websocket("/ws/recording/{session_id}")
async def recording_socket(ws: WebSocket, session_id: str):
    db = db_module.SessionLocal()
    session = db.get(RecordingSession, session_id)
    # 录后处理会话只接受 POST /import 写入音频；不拒绝的话，导入过程中（甚至导入前的
    # IDLE 窗口）仍能被一路 WS 连上开始"录制"，和导入抢占的临时文件写到同一路径。
    if session is None or session.capture_status == "STOPPED" or session.capture_mode == "batch":
        await ws.close(code=4404 if session is None else 4409)
        db.close()
        return
    # 续传重连可以接管仍挂着的旧连接（手机切网时服务端往往还没发现旧连接已断）；
    # 普通连接仍然一会话一写入方，第二个直接拒绝。
    resuming = ws.query_params.get("resume") == "1" and session_id in _resumable and session.capture_status in ACTIVE_CAPTURE
    if not await manager.connect(session_id, ws, replace=resuming):
        db.close()
        return

    try:
        while True:
            message = await ws.receive()
            if message["type"] == "websocket.disconnect":
                break
            if message.get("bytes") is not None:
                db.expire_all()
                session = db.get(RecordingSession, session_id)
                data = message["bytes"]
                if session is None or session.capture_status not in ("RECORDING", "PAUSED"):
                    await manager.send_json(session_id, {"type": "error", "code": "not_recording"})
                    continue
                if data[:4] == CHUNK_MAGIC and len(data) >= 8:
                    seq = int.from_bytes(data[4:8], "big")
                    expected = session.received_chunks
                    if seq < expected:  # 重传的旧分片：已写过，只确认
                        await manager.send_json(session_id, {"type": "ack", "seq": seq})
                        continue
                    if seq > expected:  # 中间缺片：要求从缺口补传，绝不跳着写
                        await manager.send_json(session_id, {"type": "resync", "nextSeq": expected})
                        continue
                    await storage.append_chunk(session_id, data[8:])
                    session.received_chunks = expected + 1
                    session.upload_status = "RECEIVING"
                    db.commit()
                    await manager.send_json(session_id, {"type": "ack", "seq": seq})
                    continue
                await storage.append_chunk(session_id, data)
                if session.upload_status != "RECEIVING":
                    session.upload_status = "RECEIVING"
                    db.commit()
                continue
            if message.get("text") is not None:
                try:
                    payload = json.loads(message["text"])
                    if not isinstance(payload, dict):
                        raise ValueError("expected object")
                except (ValueError, TypeError):
                    await manager.send_json(session_id, {"type": "error", "code": "invalid_json"})
                    continue
                await _handle_control(db, session_id, payload)
    except WebSocketDisconnect:
        pass
    finally:
        # 已被续传连接接管的旧连接退出时什么都不做，录制由新连接继续。
        still_owner = manager.owns(session_id, ws)
        manager.disconnect(session_id, ws)
        try:
            if still_owner:
                db.expire_all()
                session = db.get(RecordingSession, session_id)
                if session_id in _resumable and session is not None and session.capture_status in ("RECORDING", "PAUSED"):
                    session.capture_status = "RECONNECTING"
                    db.commit()
                    _cancel_grace(session_id)
                    _grace_tasks[session_id] = asyncio.create_task(_expire_after_grace(session_id, RESUME_GRACE_SECONDS))
                elif session is None or session.capture_status != "RECONNECTING":
                    stop_recording(db, session_id)
        finally:
            db.close()


async def _handle_control(db, session_id: str, payload: dict) -> None:
    if payload.get("type") != "control":
        return
    action = payload.get("action")
    db.expire_all()
    session = db.get(RecordingSession, session_id)
    if session is None:
        return
    if action == "start":
        if session.capture_status != "IDLE":
            await manager.send_json(session_id, {"type": "error", "code": "already_started"})
            return
        session.capture_status = "RECORDING"
        settings = payload.get("settings", {})
        if isinstance(settings, dict) and settings.get("resumable") is True:
            _resumable.add(session_id)
        if isinstance(settings, dict):
            for key, attr, low, high in (("sampleRate", "actual_sample_rate", 8000, 384000),
                                          ("channelCount", "channel_count", 1, 32)):
                value = settings.get(key)
                if type(value) is int and low <= value <= high:
                    setattr(session, attr, value)
            mime = settings.get("mimeType")
            if isinstance(mime, str) and mime.startswith("audio/") and len(mime) <= 100:
                session.mime_type = mime
            processing = dict(session.capture_processing)
            for key in ("echoCancellation", "noiseSuppression", "autoGainControl"):
                value = settings.get(key)
                processing[key + "Actual"] = str(value).lower() if type(value) is bool else "unknown"
            processing["unknown"] = any(processing[key + "Actual"] == "unknown" for key in
                                        ("echoCancellation", "noiseSuppression", "autoGainControl"))
            processing["applied"] = any(processing[key + "Actual"] == "true" for key in
                                        ("echoCancellation", "noiseSuppression", "autoGainControl"))
            session.capture_processing = processing
        db.commit()
        stop_event = asyncio.Event()
        _stop_events[session_id] = stop_event
        task = asyncio.create_task(simulate_capture_signals(session_id, stop_event))
        _signal_tasks.add(task)
        task.add_done_callback(_signal_tasks.discard)
    elif action == "reconnect":
        if session_id not in _resumable or session.capture_status not in ACTIVE_CAPTURE:
            await manager.send_json(session_id, {"type": "error", "code": "not_resumable", "status": session.capture_status})
            return
        _cancel_grace(session_id)
        session.capture_status = "PAUSED" if payload.get("state") == "PAUSED" else "RECORDING"
        db.commit()
        await manager.send_json(session_id, {"type": "resume_from", "nextSeq": session.received_chunks})
    elif action == "pause":
        if session.capture_status != "RECORDING":
            await manager.send_json(session_id, {"type": "error", "code": "invalid_state", "action": action, "status": session.capture_status})
            return
        session.capture_status = "PAUSED"
        db.commit()
    elif action == "resume":
        if session.capture_status != "PAUSED":
            await manager.send_json(session_id, {"type": "error", "code": "invalid_state", "action": action, "status": session.capture_status})
            return
        session.capture_status = "RECORDING"
        db.commit()
    elif action == "stop":
        if session.capture_status not in ("RECORDING", "PAUSED"):
            await manager.send_json(session_id, {"type": "error", "code": "invalid_state", "action": action, "status": session.capture_status})
            return
        recorded = payload.get("recordedMs")
        if type(recorded) is int and 0 < recorded <= 24 * 3600 * 1000:
            session.client_recorded_ms = recorded
            db.commit()
        stop_recording(db, session_id)
    else:
        await manager.send_json(session_id, {"type": "error", "code": "invalid_action"})
        return
    await manager.send_json(session_id, {"type": "stage", "stage": "capture", "status": session.capture_status})
