"""Importing an existing audio file into a 'batch' session, as an alternative to live WS capture.
Covers the same finalization path as recording (on_capture_stop), reached via file upload instead.
"""
import asyncio
import io
import wave
from pathlib import Path

import numpy as np
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.datastructures import UploadFile

from tests.test_pipeline import _create_session

RATE = 44100


def _wav_bytes(seconds: float = 1.5, rate: int = RATE, channels: int = 2) -> bytes:
    import io

    t = np.linspace(0, seconds, int(rate * seconds), endpoint=False)
    tone = (0.2 * np.sin(2 * np.pi * 220 * t) * 32767).astype("<i2")
    frames = np.column_stack([tone] * channels) if channels > 1 else tone.reshape(-1, 1)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as f:
        f.setnchannels(channels)
        f.setsampwidth(2)
        f.setframerate(rate)
        f.writeframes(frames.astype("<i2").tobytes())
    return buf.getvalue()


@pytest.fixture()
def client(isolated_backend):
    from app.main import app

    with TestClient(app) as c:
        yield c


def create_batch_session(client, title="导入测试"):
    resp = client.post("/api/sessions", json={"title": title, "captureMode": "batch"})
    assert resp.status_code == 201
    return resp.json()


class TestImportAudio:
    def test_import_finalizes_a_real_wav_saved_under_the_webm_name(self, client):
        """
        存储层把所有原始音频统一叫 raw.webm（跟录制流程一致，见 storage.raw_chunk_path），
        真正解码靠 ffmpeg 按内容嗅探格式，不看扩展名——这里特意上传一个真正的 .wav
        容器，验证这条路径确实不依赖文件名后缀。
        """
        session = create_batch_session(client)
        resp = client.post(
            f"/api/sessions/{session['id']}/import",
            files={"file": ("clip.wav", _wav_bytes(channels=2), "audio/wav")},
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["captureStatus"] == "STOPPED"
        assert body["liveAnalysisStatus"] == "FINALIZED"
        assert body["uploadStatus"] == "COMPLETED"
        assert body["mimeType"] == "audio/wav"
        assert body["actualSampleRate"] == RATE
        assert body["channelCount"] == 2
        assert body["totalDurationMs"] > 0

        quality = client.get(f"/api/sessions/{session['id']}/quality")
        assert quality.status_code == 200

        versions = client.get(f"/api/sessions/{session['id']}/versions").json()
        assert len(versions) == 1
        assert versions[0]["versionType"] == "original"

        roles = {r["role"]: r["audioVersionId"] for r in client.get(f"/api/sessions/{session['id']}/roles").json()}
        assert set(roles) == {"TIMEBASE", "ASR_SOURCE", "REVIEW_SOURCE", "RENDER_SOURCE", "EXPORT_SOURCE"}
        assert roles["TIMEBASE"] == versions[0]["id"]

    def test_second_import_on_the_same_session_is_rejected(self, client):
        session = create_batch_session(client)
        first = client.post(f"/api/sessions/{session['id']}/import", files={"file": ("a.wav", _wav_bytes(), "audio/wav")})
        assert first.status_code == 201
        second = client.post(f"/api/sessions/{session['id']}/import", files={"file": ("b.wav", _wav_bytes(), "audio/wav")})
        assert second.status_code == 409

    def test_live_mode_session_cannot_import(self, client):
        session = client.post("/api/sessions", json={"title": "在线", "captureMode": "live"}).json()
        resp = client.post(f"/api/sessions/{session['id']}/import", files={"file": ("a.wav", _wav_bytes(), "audio/wav")})
        assert resp.status_code == 409

    def test_unknown_session_404s(self, client):
        resp = client.post("/api/sessions/rs_missing/import", files={"file": ("a.wav", _wav_bytes(), "audio/wav")})
        assert resp.status_code == 404

    def test_empty_file_is_rejected(self, client):
        session = create_batch_session(client)
        resp = client.post(f"/api/sessions/{session['id']}/import", files={"file": ("empty.wav", b"", "audio/wav")})
        assert resp.status_code == 422

    def test_undecodable_upload_marks_the_session_failed_not_a_fake_report(self, client):
        """
        与录制失败同一套语义（audio_analysis.DecodeError -> FAILED）：不是 HTTP 错误，
        而是终态化流程本身把会话标成失败，不产出质量报告或版本。
        """
        session = create_batch_session(client)
        resp = client.post(
            f"/api/sessions/{session['id']}/import",
            files={"file": ("not_audio.wav", b"this is not a real audio file" * 100, "audio/wav")},
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["liveAnalysisStatus"] == "FAILED"

        quality = client.get(f"/api/sessions/{session['id']}/quality")
        assert quality.status_code == 404
        versions = client.get(f"/api/sessions/{session['id']}/versions").json()
        assert versions == []

    def test_imported_session_can_be_enhanced_like_a_recorded_one(self, client):
        session = create_batch_session(client)
        client.post(f"/api/sessions/{session['id']}/import", files={"file": ("clip.wav", _wav_bytes(), "audio/wav")})
        resp = client.post(f"/api/sessions/{session['id']}/enhancement", json={"mode": "auto"})
        assert resp.status_code == 202

    async def test_concurrent_imports_on_the_same_session_only_one_claims_it(self, isolated_backend):
        """
        HTTP 层的两个真实请求会被 TestClient 单事件循环的 portal 串行化执行，测不出
        真正的并发竞态（实测：即使去掉抢占逻辑，线程版请求也会因为串行调度而"恰好"
        一成一败）。这里直接并发调用路由函数本身，各自带独立的 db Session——就像
        两个真实请求各自的 Depends(get_db)——用 asyncio.gather 强制两者在同一个
        事件循环里交替执行，才能验证写事务里的原子声明真的互斥：一个从 IDLE 抢到
        IMPORTING，另一个必须在 claim 阶段就看到非 IDLE 而拒绝，不能两个都通过、
        各写一份 original 版本到同一个文件。
        """
        from app.routers import sessions as sessions_router

        session_id = _create_session(isolated_backend, capture_mode="batch")

        async def attempt():
            db = isolated_backend()
            upload = UploadFile(io.BytesIO(_wav_bytes()), filename="a.wav")
            try:
                await sessions_router.import_audio(session_id, file=upload, db=db)
                return 201
            except HTTPException as exc:
                return exc.status_code
            finally:
                db.close()

        results = await asyncio.gather(attempt(), attempt())
        assert sorted(results) == [201, 409]
        with isolated_backend() as db:
            from app.models import AudioVersion
            assert db.query(AudioVersion).filter_by(session_id=session_id).count() == 1

    def test_failed_upload_reverts_to_idle_and_allows_retry(self, client, monkeypatch):
        """
        临时文件原子发布：超限的上传应该清理掉 raw.webm.importing、把 capture_status
        退回 IDLE，而不是把会话卡死在 IMPORTING——重试一次正常大小的文件必须能成功，
        且最终的 raw.webm 必须是重试那次的内容，不是半截的失败内容。
        """
        from app.routers import sessions as sessions_router

        monkeypatch.setattr(sessions_router, "MAX_IMPORT_BYTES", 10)
        session = create_batch_session(client)

        failed = client.post(f"/api/sessions/{session['id']}/import",
                             files={"file": ("too_big.wav", _wav_bytes(), "audio/wav")})
        assert failed.status_code == 413
        mid = client.get(f"/api/sessions/{session['id']}").json()
        assert mid["captureStatus"] == "IDLE"

        from app.services import storage
        temp_path = Path(storage.raw_chunk_path(session["id"]) + ".importing")
        assert not temp_path.exists()

        monkeypatch.setattr(sessions_router, "MAX_IMPORT_BYTES", 300 * 1024 * 1024)
        retried = client.post(f"/api/sessions/{session['id']}/import",
                              files={"file": ("retry.wav", _wav_bytes(), "audio/wav")})
        assert retried.status_code == 201
        assert retried.json()["captureStatus"] == "STOPPED"

    def test_batch_session_rejects_live_websocket_recording(self, client):
        """
        录后处理会话只应该通过 /import 写入音频；WS 连接（包括 IDLE、导入进行中的
        IMPORTING 窗口）一律拒绝，避免和导入抢占的临时文件写同一个目标路径。
        """
        session = create_batch_session(client)
        with pytest.raises(Exception):
            with client.websocket_connect(f"/ws/recording/{session['id']}"):
                pass
