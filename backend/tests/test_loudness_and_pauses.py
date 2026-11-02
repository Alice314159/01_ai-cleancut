"""响度标准化与长停顿压缩：纯算法 + 经由 REST 的端到端渲染。"""
from pathlib import Path
import wave

import numpy as np
import pytest

from app.models import AudioVersion, EditDecision, RecordingSession, RoleAssignment
from app.services import loudness, pauses, storage
from tests.test_rendering import add_decision, client  # noqa: F401  复用 fixture

RATE = 48000


def tone(seconds, amp=0.3, f=220.0, rate=RATE):
    t = np.arange(int(seconds * rate)) / rate
    return amp * np.sin(2 * np.pi * f * t)


def write_wav(path: Path, mono: np.ndarray, rate=RATE):
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(rate)
        audio.writeframes(np.rint(np.clip(mono, -1, 1) * 32767).astype("<i2").tobytes())


def read_wav(path: Path) -> np.ndarray:
    with wave.open(str(path)) as audio:
        return np.frombuffer(audio.readframes(audio.getnframes()), dtype="<i2")


# ---------- 停顿检测 ----------

def speech_with_gaps(rate=16000):
    noise = np.random.default_rng(0).normal(0, 0.001, rate * 10)
    signal = noise.copy()
    # 0-2s 说话，2-5s 停顿 3 秒，5-6s 说话，6-6.8s 短停顿，6.8-10s 说话
    for start, end in ((0, 2), (5, 6), (6.8, 10)):
        a, b = int(start * rate), int(end * rate)
        signal[a:b] += tone(end - start, rate=rate)[: b - a]
    return signal


def test_finds_only_long_pauses_and_keeps_breathing_room():
    found = pauses.find_long_pauses(speech_with_gaps(), 16000, min_pause_ms=1500, keep_ms=500)
    assert len(found) == 1
    pause = found[0]
    assert abs(pause.start_ms - 2000) <= 20 and abs(pause.end_ms - 5000) <= 20
    assert pause.cut_start_ms == pause.start_ms + 250
    assert pause.cut_end_ms == pause.end_ms - 250


def test_leading_and_trailing_silence_trimmed_on_speech_side_only():
    rate = 16000
    signal = np.concatenate([np.zeros(rate * 2), tone(2, rate=rate), np.zeros(rate * 2)])
    signal += np.random.default_rng(1).normal(0, 0.0005, len(signal))
    found = pauses.find_long_pauses(signal, rate, min_pause_ms=1500, keep_ms=400)
    assert [(p.cut_start_ms, p.cut_end_ms) for p in found][0][0] == 0
    assert found[0].cut_end_ms == found[0].end_ms - 200
    assert found[-1].cut_end_ms == len(signal) * 1000 // rate
    assert found[-1].cut_start_ms == found[-1].start_ms + 200


def test_no_suggestions_without_clear_speech_noise_separation():
    noisy = np.random.default_rng(2).normal(0, 0.2, 16000 * 5)
    assert pauses.find_long_pauses(noisy, 16000) == []
    assert pauses.find_long_pauses(np.zeros(16000 * 5), 16000) == []


# ---------- 响度 ----------

def test_plan_gain_respects_peak_ceiling_and_max_gain():
    assert loudness.plan_gain(-26.0, -15.0, "podcast") == (10.0, None)
    gain, limited = loudness.plan_gain(-20.0, -3.0, "podcast")
    assert gain == pytest.approx(1.5) and limited == "peak"
    assert loudness.plan_gain(-60.0, -40.0, "podcast") == (20.0, "max_gain")
    assert loudness.plan_gain(None, None, "broadcast") == (0.0, None)


def test_normalize_hits_target_and_preserves_length(tmp_path):
    source, out = tmp_path / "in.wav", tmp_path / "out.wav"
    write_wav(source, tone(5, amp=0.05))
    result = loudness.normalize(source, out, "podcast")
    assert result.limited_by is None
    assert len(read_wav(out)) == len(read_wav(source))
    measured, _ = loudness.measure(out)
    assert measured == pytest.approx(-16.0, abs=0.5)
    assert result.output_lufs == pytest.approx(-16.0, abs=0.01)


def test_silence_is_left_untouched(tmp_path):
    source, out = tmp_path / "in.wav", tmp_path / "out.wav"
    write_wav(source, np.zeros(RATE))
    result = loudness.normalize(source, out, "broadcast")
    assert result.input_lufs is None and result.gain_db == 0
    assert not read_wav(out).any()


# ---------- REST 端到端 ----------

@pytest.fixture()
def gappy_recording(isolated_backend):
    mono = np.concatenate([tone(1.5), np.zeros(RATE * 3), tone(1.5)])
    mono += np.random.default_rng(3).normal(0, 0.0005, len(mono))
    with isolated_backend() as db:
        session = RecordingSession(capture_status="STOPPED", live_analysis_status="FINALIZED",
                                   total_duration_ms=6000, actual_sample_rate=RATE, channel_count=1, mime_type="audio/wav")
        db.add(session)
        db.flush()
        path = Path(storage.session_dir(session.id)) / "source.wav"
        write_wav(path, mono)
        version = AudioVersion(session_id=session.id, version_type="original", label="原始", duration_ms=6000,
                               sample_count=len(mono), sample_rate=RATE, filename=path.name)
        db.add(version)
        db.flush()
        for role in ("TIMEBASE", "RENDER_SOURCE", "REVIEW_SOURCE", "EXPORT_SOURCE"):
            db.add(RoleAssignment(session_id=session.id, role=role, audio_version_id=version.id))
        db.commit()
        return session.id


def test_pause_suggestions_render_and_can_be_bulk_rejected(client, gappy_recording, isolated_backend):
    url = f"/api/sessions/{gappy_recording}"
    response = client.post(url + "/decisions/suggest-pauses", json={"minPauseMs": 1500, "keepMs": 600})
    assert response.status_code == 200, response.text
    assert response.json()["addedCount"] == 1
    decision = response.json()["decisions"][0]
    assert decision["action"] == "COMPRESS_SILENCE" and decision["accepted"] is None
    assert decision["source"] == "silence_rule"
    assert abs(decision["startMs"] - 1800) <= 20 and abs(decision["endMs"] - 4200) <= 20

    # 再次生成不会重复
    assert client.post(url + "/decisions/suggest-pauses", json={}).json()["addedCount"] == 0

    # 待定时不能渲染；接受后按区间剪掉
    assert client.post(url + "/render").status_code == 409
    assert client.patch(url + f"/decisions/{decision['id']}", json={"accepted": True}).status_code == 200
    output = client.post(url + "/render").json()
    assert output["removedDurationMs"] == decision["endMs"] - decision["startMs"]

    # 新建议可被"拒绝待定建议"批量拒绝，手动决策不受影响
    with isolated_backend() as db:
        db.query(EditDecision).delete()
        db.commit()
    manual = add_decision(isolated_backend, gappy_recording, 100, 200, accepted=None)
    client.post(url + "/decisions/suggest-pauses", json={})
    decisions = client.post(url + "/decisions/reject-suggestions").json()
    by_id = {d["id"]: d for d in decisions}
    assert by_id[manual]["accepted"] is None
    assert all(d["accepted"] is False for d in decisions if d["source"] == "silence_rule")


def test_suggest_pauses_validates_parameters(client, gappy_recording):
    url = f"/api/sessions/{gappy_recording}/decisions/suggest-pauses"
    assert client.post(url, json={"minPauseMs": 800, "keepMs": 900}).status_code == 422
    assert client.post(url, json={"minPauseMs": 100}).status_code == 422
    assert client.post("/api/sessions/missing/decisions/suggest-pauses", json={}).status_code == 404


def test_render_with_loudness_target_reports_result(client, gappy_recording):
    url = f"/api/sessions/{gappy_recording}/render"
    plain = client.post(url).json()
    assert plain["loudness"] is None
    response = client.post(url, json={"loudnessTarget": "broadcast"})
    assert response.status_code == 201, response.text
    output = response.json()
    assert output["loudness"]["target"] == "broadcast"
    assert output["loudness"]["outputLufs"] == pytest.approx(-23.0, abs=0.01)
    assert output["version"]["sampleCount"] == plain["version"]["sampleCount"]
    assert client.get(url).json()["loudness"]["target"] == "broadcast"
    assert client.post(url, json={"loudnessTarget": "loud"}).status_code == 422
