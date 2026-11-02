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


def stop_recording(db, session_id: str) -> None:
    db.expire_all()
    session = db.get(RecordingSession, session_id)
    event = _stop_events.pop(session_id, None)
    if event is not None:
        event.set()
    if session is not None and session.capture_status in ("RECORDING", "PAUSED"):
        session.capture_status = "STOPPED"
        db.commit()
        schedule_capture_stop(session_id)


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
    if not await manager.connect(session_id, ws):
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
                if session is not None and session.capture_status in ("RECORDING", "PAUSED"):
                    await storage.append_chunk(session_id, message["bytes"])
                    if session.upload_status != "RECEIVING":
                        session.upload_status = "RECEIVING"
                        db.commit()
                else:
                    await manager.send_json(session_id, {"type": "error", "code": "not_recording"})
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
        manager.disconnect(session_id, ws)
        try:
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
        stop_recording(db, session_id)
    else:
        await manager.send_json(session_id, {"type": "error", "code": "invalid_action"})
        return
    await manager.send_json(session_id, {"type": "stage", "stage": "capture", "status": session.capture_status})
