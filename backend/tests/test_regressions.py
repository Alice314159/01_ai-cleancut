"""Regression coverage for version history, failure semantics and repeated processing."""
import asyncio
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text

from app import db as db_module
from app.models import (
    AudioQualityReport,
    AudioVersion,
    EditDecision,
    EnhancementJob,
    LiveSignal,
    RecordingSession,
    RoleAssignment,
    TranscriptToken,
    Warning as WarningModel,
)
from app.services import audio_analysis, storage, stub_pipeline
from app.services.stub_pipeline import generate_decisions, on_capture_stop, run_enhancement
from tests.test_pipeline import _create_session, _put_audio


def new_job(factory, session_id, mode="balanced"):
    with factory() as db:
        # 这些用例只验证降噪/版本行为；去削波另见 test_declip.py
        job = EnhancementJob(session_id=session_id, mode=mode, declip_mode="off")
        db.add(job)
        db.commit()
        return job.id


async def prepared_session(factory, audio):
    session_id = _create_session(factory)
    _put_audio(session_id, audio)
    await on_capture_stop(session_id)
    return session_id


async def test_repeated_enhancements_keep_earlier_audio(isolated_backend, noisy_webm_file):
    session_id = await prepared_session(isolated_backend, noisy_webm_file)
    first_id = new_job(isolated_backend, session_id, "light")
    await run_enhancement(first_id)
    with isolated_backend() as db:
        first = db.get(EnhancementJob, first_id)
        version = db.get(AudioVersion, first.result_version_id)
        first_path = Path(storage.session_dir(session_id)) / version.filename
        original_bytes = first_path.read_bytes()
    second_id = new_job(isolated_backend, session_id, "strong")
    await run_enhancement(second_id)
    with isolated_backend() as db:
        second = db.get(EnhancementJob, second_id)
        second_version = db.get(AudioVersion, second.result_version_id)
        second_path = Path(storage.session_dir(session_id)) / second_version.filename
        assert first_path != second_path
        assert first_path.read_bytes() == original_bytes
        assert second_path.read_bytes() != original_bytes


async def test_decode_failure_does_not_publish_version_or_replace_roles(isolated_backend, noisy_webm_file):
    session_id = await prepared_session(isolated_backend, noisy_webm_file)
    with isolated_backend() as db:
        before = {r.role: r.audio_version_id for r in db.query(RoleAssignment).all()}
    Path(storage.raw_chunk_path(session_id)).write_bytes(b"corrupt recording")
    job_id = new_job(isolated_backend, session_id)
    await run_enhancement(job_id)
    with isolated_backend() as db:
        job = db.get(EnhancementJob, job_id)
        assert job.status == "FAILED"
        assert job.enhancement_success is False
        assert job.result_version_id is None
        assert job.error_message
        assert db.query(AudioVersion).count() == 1
        assert {r.role: r.audio_version_id for r in db.query(RoleAssignment).all()} == before


async def test_failed_voice_protection_keeps_current_output(isolated_backend, noisy_webm_file, monkeypatch):
    session_id = await prepared_session(isolated_backend, noisy_webm_file)
    first_id = new_job(isolated_backend, session_id)
    await run_enhancement(first_id)
    with isolated_backend() as db:
        before = {r.role: r.audio_version_id for r in db.query(RoleAssignment).all()}
    monkeypatch.setattr(audio_analysis, "compute_voice_protection", lambda *a, **kw:
                        audio_analysis.VoiceProtectionResult(12.0, 0.1, 40.0, 0.2, 0.5))
    second_id = new_job(isolated_backend, session_id)
    await run_enhancement(second_id)
    with isolated_backend() as db:
        job = db.get(EnhancementJob, second_id)
        assert job.status == "COMPLETED"
        assert job.enhancement_success is False
        assert job.result_version_id is not None  # Candidate remains available for listening.
        assert {r.role: r.audio_version_id for r in db.query(RoleAssignment).all()} == before


async def test_off_mode_does_not_denoise(isolated_backend, noisy_webm_file, monkeypatch):
    session_id = await prepared_session(isolated_backend, noisy_webm_file)
    def unexpected(*args, **kwargs):
        raise AssertionError("off mode must not invoke denoising")
    monkeypatch.setattr(audio_analysis, "spectral_subtract_denoise", unexpected)
    job_id = new_job(isolated_backend, session_id, "off")
    await run_enhancement(job_id)
    with isolated_backend() as db:
        job = db.get(EnhancementJob, job_id)
        assert job.status == "COMPLETED"
        assert job.actual_chain == []
        assert job.noise_reduction_db == 0


async def test_decision_regeneration_preserves_review_and_does_not_fake_transcript(isolated_backend, repeat_timeline):
    samples, marks = repeat_timeline
    session_id = _create_session(isolated_backend)
    audio_analysis.encode_wav(samples, Path(storage.raw_chunk_path(session_id)))
    start, end = marks["phrase_x_2"]
    with isolated_backend() as db:
        db.add(LiveSignal(session_id=session_id, signal_type="repetition", signal_level="strong",
                          start_ms=int(start), end_ms=int(end), text="candidate", score=0.8))
        db.commit()
    await asyncio.gather(generate_decisions(session_id), generate_decisions(session_id))
    with isolated_backend() as db:
        decision = db.query(EditDecision).one()
        decision.accepted = False
        decision_id = decision.id
        db.commit()
    await generate_decisions(session_id)
    with isolated_backend() as db:
        assert db.query(EditDecision).one().id == decision_id
        assert db.query(EditDecision).one().accepted is False
        assert db.query(TranscriptToken).count() == 0


@pytest.mark.parametrize("start,end", [(0, 300), (2900, 4000)])
async def test_unavailable_acoustic_window_never_becomes_delete(isolated_backend, clean_webm_file, start, end):
    session_id = _create_session(isolated_backend)
    _put_audio(session_id, clean_webm_file)
    with isolated_backend() as db:
        db.add(LiveSignal(session_id=session_id, signal_type="repetition", signal_level="strong",
                          start_ms=start, end_ms=end, text="candidate", score=0.99))
        db.commit()
    await generate_decisions(session_id)
    with isolated_backend() as db:
        assert db.query(EditDecision).count() == 0
        assert db.query(LiveSignal).one().invalidated is True


async def test_slow_decode_does_not_block_event_loop(isolated_backend, noisy_webm_file, monkeypatch):
    session_id = await prepared_session(isolated_backend, noisy_webm_file)
    entered = threading.Event()
    released = threading.Event()
    real_decode = audio_analysis.decode_pcm
    def slow_decode(*args, **kwargs):
        entered.set()
        if not released.wait(timeout=2):
            raise AssertionError("event loop could not release decoder")
        return real_decode(*args, **kwargs)
    monkeypatch.setattr(audio_analysis, "decode_pcm", slow_decode)
    task = asyncio.create_task(run_enhancement(new_job(isolated_backend, session_id)))
    try:
        for _ in range(100):
            if entered.is_set():
                break
            await asyncio.sleep(0.005)
        assert entered.is_set()
        assert not task.done()
    finally:
        released.set()
        await task


def test_latest_job_uses_creation_order_not_random_id(isolated_backend):
    session_id = _create_session(isolated_backend)
    with isolated_backend() as db:
        db.add(EnhancementJob(id="job_zzzzzz", session_id=session_id, mode="light", status="COMPLETED", created_at_ns=1))
        db.add(EnhancementJob(id="job_aaaaaa", session_id=session_id, mode="strong", status="PROCESSING", created_at_ns=2))
        db.commit()
    from app.main import app
    with TestClient(app) as client:
        response = client.get(f"/api/sessions/{session_id}/enhancement")
        assert response.json()["id"] == "job_aaaaaa"
        assert client.get(f"/api/sessions/{session_id}/enhancement/job_zzzzzz").json()["id"] == "job_zzzzzz"


def test_existing_database_migrates_without_losing_jobs(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE enhancement_jobs (id TEXT PRIMARY KEY)"))
        connection.execute(text("INSERT INTO enhancement_jobs (id) VALUES ('job_z'), ('job_a')"))
    monkeypatch.setattr(db_module, "engine", engine)
    db_module.init_db()
    db_module.init_db()  # Repeated startup is safe.
    assert "created_at_ns" in {c["name"] for c in inspect(engine).get_columns("enhancement_jobs")}
    with engine.connect() as connection:
        assert connection.execute(text("SELECT id FROM enhancement_jobs ORDER BY created_at_ns DESC")).scalars().all() == ["job_a", "job_z"]
    engine.dispose()


async def test_completed_job_cannot_overwrite_its_result(isolated_backend, noisy_webm_file):
    session_id = await prepared_session(isolated_backend, noisy_webm_file)
    job_id = new_job(isolated_backend, session_id)
    await run_enhancement(job_id)
    with isolated_backend() as db:
        version_id = db.get(EnhancementJob, job_id).result_version_id
        count = db.query(AudioVersion).count()
    await run_enhancement(job_id)
    with isolated_backend() as db:
        assert db.query(AudioVersion).count() == count
        assert db.get(EnhancementJob, job_id).result_version_id == version_id


@pytest.mark.parametrize("decoder", [audio_analysis.decode_pcm, audio_analysis.decode_tail_pcm])
def test_decoder_timeout_is_reported_as_decode_error(tmp_path, monkeypatch, decoder):
    import subprocess
    path = tmp_path / 'recording.webm'
    path.write_bytes(b'input')
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired('ffmpeg', 10)
    monkeypatch.setattr(audio_analysis.subprocess, 'run', timeout)
    with pytest.raises(audio_analysis.DecodeError, match='超时'):
        decoder(path)


async def test_concurrent_capture_stop_does_not_duplicate_publish(isolated_backend, clean_webm_file):
    """
    WS 断线兜底、HTTP /stop 兜底、导入接口都可能对同一 session_id 并发调用
    on_capture_stop；这里模拟两个几乎同时发生的调用，确认只有一个真正跑完解码
    发布，另一个在 claim 阶段就看到 FINALIZING 而原样返回，不会各插一套
    AudioVersion/AudioQualityReport/角色。
    """
    session_id = _create_session(isolated_backend)
    _put_audio(session_id, clean_webm_file)
    await asyncio.gather(on_capture_stop(session_id), on_capture_stop(session_id))
    with isolated_backend() as db:
        assert db.query(AudioVersion).filter_by(session_id=session_id).count() == 1
        assert db.query(AudioQualityReport).filter_by(session_id=session_id).count() == 1
        assert db.query(RoleAssignment).filter_by(session_id=session_id).count() == 5
        session = db.get(RecordingSession, session_id)
        assert session.live_analysis_status == "FINALIZED"
        assert session.capture_status == "STOPPED"


def test_recover_interrupted_finalizations_marks_stuck_finalizing_as_failed(isolated_backend):
    """服务重启时，上次卡在 FINALIZING 的会话不该永远卡住等一个不会再来的完成。"""
    session_id = _create_session(isolated_backend, capture_status="STOPPED", live_analysis_status="FINALIZING")
    stub_pipeline.recover_interrupted_finalizations()
    with isolated_backend() as db:
        session = db.get(RecordingSession, session_id)
        assert session.live_analysis_status == "FAILED"
        assert session.capture_status == "STOPPED"
        assert session.server_merge_status == "FAILED"
        assert session.processing_status == "FAILED"
        assert db.query(WarningModel).filter_by(session_id=session_id).count() == 1


def test_recover_interrupted_finalizations_resets_stuck_importing_to_idle(isolated_backend):
    """
    导入的上传/发布阶段被中断时还没调用过 on_capture_stop（否则早就不是 IMPORTING
    了），raw.webm 还不存在或仍是旧内容，所以直接退回 IDLE 允许重新导入，而不是
    标记失败；顺带清掉残留的临时文件，不让它们跨重启一直堆积。
    """
    session_id = _create_session(isolated_backend, capture_mode="batch", capture_status="IMPORTING")
    temp_path = Path(storage.raw_chunk_path(session_id) + ".importing")
    temp_path.write_bytes(b"partial upload")

    stub_pipeline.recover_interrupted_finalizations()

    with isolated_backend() as db:
        session = db.get(RecordingSession, session_id)
        assert session.capture_status == "IDLE"
    assert not temp_path.exists()


async def test_closed_notification_socket_does_not_fail_capture(isolated_backend, clean_webm_file):
    from fastapi import WebSocketDisconnect
    from app.ws.connection_manager import manager
    class ClosedSocket:
        async def accept(self):
            pass
        async def send_json(self, payload):
            raise WebSocketDisconnect()
    session_id = _create_session(isolated_backend)
    _put_audio(session_id, clean_webm_file)
    socket = ClosedSocket()
    await manager.connect(session_id, socket)
    try:
        await on_capture_stop(session_id)
        with isolated_backend() as db:
            assert db.get(RecordingSession, session_id).live_analysis_status == 'FINALIZED'
    finally:
        manager.disconnect(session_id, socket)


def test_legacy_sessions_table_gains_created_at(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy_sessions.db'}")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE recording_sessions (id TEXT PRIMARY KEY, title TEXT)"))
        connection.execute(text("INSERT INTO recording_sessions (id, title) VALUES ('rs_zz', 'a'), ('rs_aa', 'b')"))
    monkeypatch.setattr(db_module, "engine", engine)
    db_module.init_db()
    db_module.init_db()
    with engine.connect() as connection:
        rows = connection.execute(text("SELECT id, created_at_ns FROM recording_sessions ORDER BY rowid")).all()
    assert rows == [("rs_zz", 0), ("rs_aa", 0)]
    engine.dispose()
