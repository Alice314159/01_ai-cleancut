"""断线续传：带序号分片、确认、补传、断线保留、重连接管、超时结束、重启恢复。"""
import time

import pytest

from app.models import RecordingSession
from app.routers import recording_ws
from app.services import storage
from tests.test_pipeline import _create_session
from tests.test_recording_ws import wait_for_finalized, ws_client  # noqa: F401


def frame(seq: int, data: bytes) -> bytes:
    return b"CCK1" + seq.to_bytes(4, "big") + data


def start_resumable(ws):
    ws.send_json({"type": "control", "action": "start", "settings": {"resumable": True}})
    assert ws.receive_json()["status"] == "RECORDING"


def receive_until(ws, kind):
    while True:
        message = ws.receive_json()
        if message.get("type") == kind:
            return message


def status(factory, session_id):
    with factory() as db:
        return db.get(RecordingSession, session_id).capture_status


def wait_status(factory, session_id, expected, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if status(factory, session_id) == expected:
            return
        time.sleep(0.01)
    raise AssertionError(f"status stayed {status(factory, session_id)}, expected {expected}")


def chunks_of(payload: bytes, size: int = 4096):
    return [payload[i:i + size] for i in range(0, len(payload), size)]


def test_sequenced_chunks_ack_dedupe_and_resync(ws_client, isolated_backend, clean_webm_file):
    session_id = _create_session(isolated_backend)
    parts = chunks_of(clean_webm_file.read_bytes())
    with ws_client.websocket_connect(f"/ws/recording/{session_id}") as ws:
        start_resumable(ws)
        ws.send_bytes(frame(0, parts[0]))
        assert receive_until(ws, "ack")["seq"] == 0
        ws.send_bytes(frame(0, parts[0]))           # 重复：只确认，不重写
        assert receive_until(ws, "ack")["seq"] == 0
        ws.send_bytes(frame(2, parts[2]))           # 跳号：要求从 1 补传
        assert receive_until(ws, "resync")["nextSeq"] == 1
        for seq in range(1, len(parts)):
            ws.send_bytes(frame(seq, parts[seq]))
            assert receive_until(ws, "ack")["seq"] == seq
        ws.send_json({"type": "control", "action": "stop"})
    assert wait_for_finalized(isolated_backend, session_id) == "FINALIZED"
    assert open(storage.raw_chunk_path(session_id), "rb").read() == b"".join(parts)


def test_disconnect_keeps_session_open_and_reconnect_resumes(ws_client, isolated_backend, clean_webm_file):
    session_id = _create_session(isolated_backend)
    parts = chunks_of(clean_webm_file.read_bytes())
    half = len(parts) // 2
    with ws_client.websocket_connect(f"/ws/recording/{session_id}") as ws:
        start_resumable(ws)
        for seq in range(half):
            ws.send_bytes(frame(seq, parts[seq]))
            receive_until(ws, "ack")
    # 断线：不结束，等待重连
    wait_status(isolated_backend, session_id, "RECONNECTING")
    with isolated_backend() as db:
        assert db.get(RecordingSession, session_id).live_analysis_status != "FINALIZED"

    with ws_client.websocket_connect(f"/ws/recording/{session_id}?resume=1") as ws:
        ws.send_json({"type": "control", "action": "reconnect", "state": "RECORDING"})
        assert receive_until(ws, "resume_from")["nextSeq"] == half
        # 客户端不确定最后一片是否送达，从更早处重发也不会重复写
        for seq in range(half - 1, len(parts)):
            ws.send_bytes(frame(seq, parts[seq]))
            receive_until(ws, "ack")
        ws.send_json({"type": "control", "action": "stop"})
    assert wait_for_finalized(isolated_backend, session_id) == "FINALIZED"
    assert open(storage.raw_chunk_path(session_id), "rb").read() == b"".join(parts)


def test_resume_takes_over_half_open_connection(ws_client, isolated_backend, clean_webm_file):
    """手机切网时服务端常常还没发现旧连接断了；续传连接可以接管，旧连接退出不会结束录制。"""
    session_id = _create_session(isolated_backend)
    parts = chunks_of(clean_webm_file.read_bytes())
    with ws_client.websocket_connect(f"/ws/recording/{session_id}") as old:
        start_resumable(old)
        old.send_bytes(frame(0, parts[0]))
        receive_until(old, "ack")
        with ws_client.websocket_connect(f"/ws/recording/{session_id}?resume=1") as new:
            new.send_json({"type": "control", "action": "reconnect", "state": "PAUSED"})
            assert receive_until(new, "resume_from")["nextSeq"] == 1
            assert status(isolated_backend, session_id) == "PAUSED"
            new.send_json({"type": "control", "action": "resume"})
            receive_until(new, "stage")
            for seq in range(1, len(parts)):
                new.send_bytes(frame(seq, parts[seq]))
                receive_until(new, "ack")
            new.send_json({"type": "control", "action": "stop"})
            assert wait_for_finalized(isolated_backend, session_id) == "FINALIZED"
    assert open(storage.raw_chunk_path(session_id), "rb").read() == b"".join(parts)


def test_plain_second_writer_still_rejected_and_legacy_disconnect_finalizes(ws_client, isolated_backend, clean_webm_file):
    session_id = _create_session(isolated_backend)
    with ws_client.websocket_connect(f"/ws/recording/{session_id}") as ws:
        ws.send_json({"type": "control", "action": "start"})          # 未声明 resumable
        ws.send_bytes(clean_webm_file.read_bytes())
        receive_until(ws, "stage")
        # 未声明续传的会话，?resume=1 也不能接管
        with pytest.raises(Exception):
            with ws_client.websocket_connect(f"/ws/recording/{session_id}?resume=1") as intruder:
                intruder.receive_json()
    assert wait_for_finalized(isolated_backend, session_id) == "FINALIZED"


def test_grace_expiry_finalizes_received_audio(ws_client, isolated_backend, clean_webm_file, monkeypatch):
    monkeypatch.setattr(recording_ws, "RESUME_GRACE_SECONDS", 0.2)
    session_id = _create_session(isolated_backend)
    with ws_client.websocket_connect(f"/ws/recording/{session_id}") as ws:
        start_resumable(ws)
        ws.send_bytes(frame(0, clean_webm_file.read_bytes()))
        receive_until(ws, "ack")
    assert wait_for_finalized(isolated_backend, session_id) == "FINALIZED"
    assert status(isolated_backend, session_id) == "STOPPED"
    # 结束后不能再续传
    with pytest.raises(Exception):
        with ws_client.websocket_connect(f"/ws/recording/{session_id}?resume=1") as late:
            late.receive_json()


def test_http_stop_and_restart_recovery_end_reconnecting_sessions(ws_client, isolated_backend, clean_webm_file):
    session_id = _create_session(isolated_backend)
    with ws_client.websocket_connect(f"/ws/recording/{session_id}") as ws:
        start_resumable(ws)
        ws.send_bytes(frame(0, clean_webm_file.read_bytes()))
        receive_until(ws, "ack")
    wait_status(isolated_backend, session_id, "RECONNECTING")
    assert ws_client.delete(f"/api/sessions/{session_id}").status_code == 409   # 等待重连时不能删除
    assert ws_client.post(f"/api/sessions/{session_id}/stop").status_code == 200
    assert wait_for_finalized(isolated_backend, session_id) == "FINALIZED"

    # 进程重启：内存里的等待计时没了，残留的 RECONNECTING 会话按已收到内容结束
    other = _create_session(isolated_backend)
    with isolated_backend() as db:
        session = db.get(RecordingSession, other)
        session.capture_status = "RECONNECTING"
        db.commit()
    storage.raw_chunk_path(other)
    with open(storage.raw_chunk_path(other), "wb") as f:
        f.write(clean_webm_file.read_bytes())
    from app.main import app
    from fastapi.testclient import TestClient
    with TestClient(app):
        assert wait_for_finalized(isolated_backend, other) == "FINALIZED"
