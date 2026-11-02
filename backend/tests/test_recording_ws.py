"""Exercise the actual WebSocket protocol and capture finalization."""
import time

import pytest
from fastapi import WebSocketDisconnect
from fastapi.testclient import TestClient

from app.models import AudioVersion, RecordingSession
from app.services import storage
from tests.test_pipeline import _create_session


@pytest.fixture()
def ws_client(isolated_backend):
    from app.main import app
    with TestClient(app) as client:
        yield client


def wait_for_finalized(factory, session_id):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        with factory() as db:
            session = db.get(RecordingSession, session_id)
            if session.live_analysis_status in ("FINALIZED", "FAILED"):
                return session.live_analysis_status
        time.sleep(0.01)
    raise AssertionError("capture never reached a terminal state")


def test_stop_flushes_last_chunk_and_rejects_late_bytes(ws_client, isolated_backend, clean_webm_file):
    session_id = _create_session(isolated_backend)
    payload = clean_webm_file.read_bytes()
    with ws_client.websocket_connect(f"/ws/recording/{session_id}") as ws:
        ws.send_json({"type": "control", "action": "start", "settings": {
            "sampleRate": 16000, "channelCount": 1, "echoCancellation": False,
            "noiseSuppression": True, "autoGainControl": False, "mimeType": "audio/webm;codecs=opus",
        }})
        assert ws.receive_json()["status"] == "RECORDING"
        ws.send_bytes(payload[:-300])
        ws.send_json({"type": "control", "action": "pause"})
        assert ws.receive_json()["status"] == "PAUSED"
        ws.send_bytes(payload[-300:])
        ws.send_json({"type": "control", "action": "stop"})
        while True:
            message = ws.receive_json()
            if message.get("stage") == "finalized":
                assert message["status"] == "READY"
                break
        ws.send_bytes(b"late data must not corrupt the finalized file")
        assert ws.receive_json()["code"] == "not_recording"
    assert open(storage.raw_chunk_path(session_id), "rb").read() == payload
    with isolated_backend() as db:
        session = db.get(RecordingSession, session_id)
        assert session.total_duration_ms == 3000
        assert session.actual_sample_rate == 16000
        assert session.capture_processing["echoCancellationActual"] == "false"
        assert db.query(AudioVersion).count() == 1


def test_disconnect_finalizes_received_audio(ws_client, isolated_backend, clean_webm_file):
    session_id = _create_session(isolated_backend)
    with ws_client.websocket_connect(f"/ws/recording/{session_id}") as ws:
        ws.send_json({"type": "control", "action": "start"})
        assert ws.receive_json()["status"] == "RECORDING"
        ws.send_bytes(clean_webm_file.read_bytes())
        ws.send_json({"type": "control", "action": "pause"})
        assert ws.receive_json()["status"] == "PAUSED"
    assert wait_for_finalized(isolated_backend, session_id) == "FINALIZED"
    with isolated_backend() as db:
        assert db.get(RecordingSession, session_id).capture_status == "STOPPED"


def test_second_writer_is_rejected_without_disrupting_first(ws_client, isolated_backend, clean_webm_file):
    session_id = _create_session(isolated_backend)
    with ws_client.websocket_connect(f"/ws/recording/{session_id}") as first:
        with pytest.raises(WebSocketDisconnect):
            with ws_client.websocket_connect(f"/ws/recording/{session_id}"):
                pass
        first.send_json({"type": "control", "action": "start"})
        assert first.receive_json()["status"] == "RECORDING"
        first.send_bytes(clean_webm_file.read_bytes())
        first.send_json({"type": "control", "action": "pause"})
        assert first.receive_json()["status"] == "PAUSED"
    assert wait_for_finalized(isolated_backend, session_id) == "FINALIZED"


def test_invalid_control_does_not_crash_and_idle_bytes_are_rejected(ws_client, isolated_backend):
    session_id = _create_session(isolated_backend)
    with ws_client.websocket_connect(f"/ws/recording/{session_id}") as ws:
        ws.send_json([])
        assert ws.receive_json()["code"] == "invalid_json"
        ws.send_bytes(b"unexpected")
        assert ws.receive_json()["code"] == "not_recording"
    with isolated_backend() as db:
        assert db.get(RecordingSession, session_id).capture_status == "IDLE"


def test_invalid_control_state_transitions_return_explicit_errors(ws_client, isolated_backend):
    session_id = _create_session(isolated_backend)
    with ws_client.websocket_connect(f"/ws/recording/{session_id}") as ws:
        ws.send_json({"type": "control", "action": "pause"})
        assert ws.receive_json() == {"type": "error", "code": "invalid_state", "action": "pause", "status": "IDLE"}

        ws.send_json({"type": "control", "action": "resume"})
        assert ws.receive_json() == {"type": "error", "code": "invalid_state", "action": "resume", "status": "IDLE"}

        ws.send_json({"type": "control", "action": "stop"})
        assert ws.receive_json() == {"type": "error", "code": "invalid_state", "action": "stop", "status": "IDLE"}

        ws.send_json({"type": "control", "action": "frobnicate"})
        assert ws.receive_json() == {"type": "error", "code": "invalid_action"}

        ws.send_json({"type": "control", "action": "start"})
        assert ws.receive_json()["status"] == "RECORDING"

        ws.send_json({"type": "control", "action": "resume"})
        assert ws.receive_json() == {
            "type": "error", "code": "invalid_state", "action": "resume", "status": "RECORDING",
        }

        ws.send_json({"type": "control", "action": "pause"})
        assert ws.receive_json()["status"] == "PAUSED"

        ws.send_json({"type": "control", "action": "pause"})
        assert ws.receive_json() == {
            "type": "error", "code": "invalid_state", "action": "pause", "status": "PAUSED",
        }

        ws.send_json({"type": "control", "action": "stop"})
        while True:
            message = ws.receive_json()
            if message.get("stage") == "finalized":
                break

        ws.send_json({"type": "control", "action": "stop"})
        assert ws.receive_json() == {
            "type": "error", "code": "invalid_state", "action": "stop", "status": "STOPPED",
        }
    with isolated_backend() as db:
        assert db.get(RecordingSession, session_id).capture_status == "STOPPED"


def test_stopped_session_cannot_be_reopened(ws_client, isolated_backend):
    session_id = _create_session(isolated_backend, capture_status="STOPPED")
    with pytest.raises(WebSocketDisconnect):
        with ws_client.websocket_connect(f"/ws/recording/{session_id}"):
            pass


def test_http_stop_and_ws_stop_do_not_duplicate_analysis(ws_client, isolated_backend, clean_webm_file):
    session_id = _create_session(isolated_backend)
    with ws_client.websocket_connect(f"/ws/recording/{session_id}") as ws:
        ws.send_json({"type": "control", "action": "start"})
        assert ws.receive_json()["status"] == "RECORDING"
        ws.send_bytes(clean_webm_file.read_bytes())
        # Pause acknowledgment guarantees the earlier binary frame has been persisted.
        ws.send_json({"type": "control", "action": "pause"})
        assert ws.receive_json()["status"] == "PAUSED"
        assert ws_client.post(f"/api/sessions/{session_id}/stop").status_code == 200
        ws.send_json({"type": "control", "action": "stop"})
        assert wait_for_finalized(isolated_backend, session_id) == "FINALIZED"
    with isolated_backend() as db:
        assert db.query(AudioVersion).count() == 1
