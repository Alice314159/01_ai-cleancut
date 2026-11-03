"""时钟漂移：按浏览器计时与解码采样数比较；短录音不测；明显偏差给出警告。"""
import subprocess

import pytest

from app.models import RecordingSession, Warning as WarningModel
from app.services.stub_pipeline import clock_drift_report
from tests.test_pipeline import _create_session
from tests.test_recording_ws import wait_for_finalized, ws_client  # noqa: F401


def test_report_math_and_minimum_duration():
    assert clock_drift_report(16000 * 10, 10_000) is None            # 太短：测不准
    assert clock_drift_report(16000 * 40, None) is None
    exact = clock_drift_report(16000 * 40, 40_000)
    assert exact["clock_drift_ppm"] == 0 and exact["drift_remap_applied"] is False
    fast = clock_drift_report(16000 * 44, 40_000)                     # 多出 10%
    assert fast["clock_drift_ppm"] == pytest.approx(100_000)
    assert fast["expected_sample_count"] == 640_000 and fast["decoded_sample_count"] == 704_000


@pytest.fixture()
def long_webm(tmp_path):
    path = tmp_path / "long.webm"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=220:duration=32:sample_rate=48000",
                    "-af", "volume=0.3", "-c:a", "libopus", str(path)], check=True)
    return path.read_bytes()


def record(ws_client, factory, payload, recorded_ms):
    session_id = _create_session(factory)
    with ws_client.websocket_connect(f"/ws/recording/{session_id}") as ws:
        ws.send_json({"type": "control", "action": "start"})
        ws.receive_json()
        ws.send_bytes(payload)
        ws.send_json({"type": "control", "action": "stop", "recordedMs": recorded_ms})
        while ws.receive_json().get("stage") != "finalized":
            pass
    assert wait_for_finalized(factory, session_id) == "FINALIZED"
    return session_id


def test_matching_duration_reports_near_zero_drift(ws_client, isolated_backend, long_webm):
    session_id = record(ws_client, isolated_backend, long_webm, 32_000)
    detail = ws_client.get(f"/api/sessions/{session_id}").json()
    assert abs(detail["clockDrift"]["clockDriftPpm"]) < 5000           # 编码器首尾填充带来的少量偏差
    assert not [w for w in detail["warnings"] if w["code"] == "clock_drift"]


def test_large_mismatch_warns(ws_client, isolated_backend, long_webm):
    session_id = record(ws_client, isolated_backend, long_webm, 30_000)   # 浏览器只计了 30 秒，音频却有 32 秒
    detail = ws_client.get(f"/api/sessions/{session_id}").json()
    assert detail["clockDrift"]["clockDriftPpm"] == pytest.approx(66_667, rel=0.05)
    warning = next(w for w in detail["warnings"] if w["code"] == "clock_drift")
    assert "+6.7%" in warning["message"]


def test_short_or_missing_duration_is_not_measured(ws_client, isolated_backend, clean_webm_file):
    session_id = record(ws_client, isolated_backend, clean_webm_file.read_bytes(), 3000)
    assert ws_client.get(f"/api/sessions/{session_id}").json()["clockDrift"] is None
    with isolated_backend() as db:
        assert db.get(RecordingSession, session_id).client_recorded_ms == 3000
        assert db.query(WarningModel).filter_by(code="clock_drift").count() == 0
