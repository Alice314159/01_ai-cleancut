"""Pipeline integration tests with real synthetic audio.
Verify measured quality, denoised files, role assignment and acoustic decisions.
WebSocket lifecycle and historical consistency are covered in separate regression suites.
"""
import asyncio
import shutil

import pytest

from app.models import (
    AudioQualityReport,
    AudioVersion,
    EditDecision,
    EnhancementJob,
    LiveSignal,
    RecordingSession,
    RoleAssignment,
    TranscriptionJob,
    Warning as WarningModel,
)
from app.services import audio_analysis, storage, transcription
from app.services.stub_pipeline import generate_decisions, on_capture_stop, run_enhancement


def _create_session(db_session_local, **overrides) -> str:
    db = db_session_local()
    session = RecordingSession(**overrides)
    db.add(session)
    db.commit()
    session_id = session.id
    db.close()
    return session_id


def _put_audio(session_id: str, source_path) -> None:
    raw_path = storage.raw_chunk_path(session_id)
    shutil.copy(source_path, raw_path)


class TestOnCaptureStopWithRealAudio:
    async def test_is_idempotent(self, isolated_backend, clean_webm_file):
        session_id = _create_session(isolated_backend)
        _put_audio(session_id, clean_webm_file)

        await on_capture_stop(session_id)
        await on_capture_stop(session_id)

        db = isolated_backend()
        assert db.query(AudioVersion).filter_by(session_id=session_id, version_type="original").count() == 1
        assert db.query(AudioQualityReport).filter_by(session_id=session_id).count() == 1
        assert db.query(RoleAssignment).filter_by(session_id=session_id).count() == 5
        db.close()

    async def test_produces_accurate_version_metadata(self, isolated_backend, noisy_webm_file):
        """
        回归测试：original 版本的 duration/sample_count 曾经只按 committed_duration_ms
        （录制中按 2.5 秒轮询节奏推算的时长）估算，跟真实音频对不上。现在应该和真实解码出的
        采样数完全一致，即使 committed_duration_ms 还停留在 0（比如音频发送得比一个轮询周期还快）。
        """
        session_id = _create_session(isolated_backend, committed_duration_ms=0)
        _put_audio(session_id, noisy_webm_file)

        await on_capture_stop(session_id)

        db = isolated_backend()
        session = db.get(RecordingSession, session_id)
        assert session.total_duration_ms == 3000  # noisy_webm_file 是 3 秒
        original = db.query(AudioVersion).filter_by(session_id=session_id, version_type="original").first()
        assert original is not None
        assert original.duration_ms == 3000
        assert original.sample_count == 3 * original.sample_rate
        db.close()

    async def test_produces_real_quality_report_not_placeholder(self, isolated_backend, noisy_webm_file):
        session_id = _create_session(isolated_backend)
        _put_audio(session_id, noisy_webm_file)

        await on_capture_stop(session_id)

        db = isolated_backend()
        report = db.get(AudioQualityReport, session_id)
        assert report is not None
        assert report.quality_grade in ("POOR", "FAIR")
        assert report.clipping_ratio > 0.01
        # 成功分析不应该产生失败警告
        warnings = db.query(WarningModel).filter_by(session_id=session_id).all()
        assert not any(w.code == "quality_analysis_failed" for w in warnings)
        db.close()

    async def test_creates_all_five_role_assignments(self, isolated_backend, clean_webm_file):
        session_id = _create_session(isolated_backend)
        _put_audio(session_id, clean_webm_file)

        await on_capture_stop(session_id)

        db = isolated_backend()
        roles = {r.role for r in db.query(RoleAssignment).filter_by(session_id=session_id).all()}
        assert roles == {"TIMEBASE", "ASR_SOURCE", "REVIEW_SOURCE", "RENDER_SOURCE", "EXPORT_SOURCE"}
        db.close()

    async def test_session_reaches_finalized_state(self, isolated_backend, clean_webm_file):
        session_id = _create_session(isolated_backend)
        _put_audio(session_id, clean_webm_file)

        await on_capture_stop(session_id)

        db = isolated_backend()
        session = db.get(RecordingSession, session_id)
        assert session.capture_status == "STOPPED"
        assert session.live_analysis_status == "FINALIZED"
        assert session.upload_status == "COMPLETED"
        assert session.server_merge_status == "VERIFIED"
        db.close()


class TestOnCaptureStopDecodeFailure:
    async def test_missing_audio_fails_without_placeholder_report(self, isolated_backend):
        session_id = _create_session(isolated_backend)
        await on_capture_stop(session_id)
        with isolated_backend() as db:
            assert db.get(AudioQualityReport, session_id) is None
            assert db.query(AudioVersion).filter_by(session_id=session_id).count() == 0
            assert db.query(RoleAssignment).filter_by(session_id=session_id).count() == 0
            assert db.get(RecordingSession, session_id).live_analysis_status == "FAILED"
            assert db.query(WarningModel).filter_by(code="quality_analysis_failed").count() == 1


class TestAutoTranscriptionAfterFinalize:
    """
    录制/导入终态化成功后应自动排队一次转写，不需要用户先点"开始本机转写"。
    isolated_backend 默认把 dependency_available() 打成 False（否则几乎每个跑到
    终态化的测试都会在后台真的加载 faster-whisper 模型），这里显式打开，
    并把 run_transcription 换成一个只记录调用的桩，只验证触发逻辑本身，
    不重复测 run_transcription 的行为（已在 test_transcription.py 覆盖）。
    """

    async def test_queues_a_job_when_dependency_available(self, isolated_backend, clean_webm_file, monkeypatch):
        monkeypatch.setattr(transcription, "dependency_available", lambda: True)
        started_with = []
        monkeypatch.setattr(transcription, "run_transcription", lambda job_id: started_with.append(job_id))
        session_id = _create_session(isolated_backend)
        _put_audio(session_id, clean_webm_file)

        await on_capture_stop(session_id)
        # run_transcription runs via asyncio.to_thread on a worker thread; give the
        # scheduled task a moment to actually execute before asserting on it.
        for _ in range(50):
            if started_with:
                break
            await asyncio.sleep(0.02)

        with isolated_backend() as db:
            job = db.query(TranscriptionJob).filter_by(session_id=session_id).first()
            assert job is not None
            assert job.requested_language == "auto"
            assert started_with == [job.id]

    async def test_does_not_queue_when_dependency_unavailable(self, isolated_backend, clean_webm_file):
        session_id = _create_session(isolated_backend)
        _put_audio(session_id, clean_webm_file)

        await on_capture_stop(session_id)

        with isolated_backend() as db:
            assert db.query(TranscriptionJob).filter_by(session_id=session_id).count() == 0

    async def test_decode_failure_does_not_queue_a_transcription_job(self, isolated_backend, monkeypatch):
        monkeypatch.setattr(transcription, "dependency_available", lambda: True)
        session_id = _create_session(isolated_backend)  # no audio written -> decode fails

        await on_capture_stop(session_id)

        with isolated_backend() as db:
            assert db.query(TranscriptionJob).filter_by(session_id=session_id).count() == 0


class TestRunEnhancement:
    async def _setup_session_with_original_version(self, isolated_backend, audio_path):
        session_id = _create_session(isolated_backend)
        _put_audio(session_id, audio_path)
        await on_capture_stop(session_id)  # 先跑完录制流水线，产出 original 版本
        return session_id

    async def test_produces_real_denoised_version_with_playable_file(
        self, isolated_backend, noisy_webm_file
    ):
        session_id = await self._setup_session_with_original_version(isolated_backend, noisy_webm_file)

        db = isolated_backend()
        job = EnhancementJob(session_id=session_id, mode="balanced", status="QUEUED", progress=0.0)
        db.add(job)
        db.commit()
        job_id = job.id
        db.close()

        await run_enhancement(job_id)

        db = isolated_backend()
        job = db.get(EnhancementJob, job_id)
        assert job.status == "COMPLETED"
        assert job.enhancement_success is True
        assert job.noise_reduction_db > 5, "噪声下降应该是真实测量出来的、有意义的数值"

        denoised = db.query(AudioVersion).filter_by(session_id=session_id, version_type="denoised").first()
        assert denoised is not None
        assert denoised.engine == "spectral_subtraction"
        assert denoised.filename is not None

        # 文件真的存在、真的能被解码——不是只有数据库记录，没有实际文件
        from pathlib import Path
        denoised_path = Path(storage.session_dir(session_id)) / denoised.filename
        assert denoised_path.exists()
        metrics = audio_analysis.analyze(denoised_path)
        assert metrics.duration_ms == denoised.duration_ms
        db.close()

    async def test_render_and_export_roles_upgrade_to_denoised(self, isolated_backend, noisy_webm_file):
        session_id = await self._setup_session_with_original_version(isolated_backend, noisy_webm_file)

        db = isolated_backend()
        job = EnhancementJob(session_id=session_id, mode="balanced", status="QUEUED", progress=0.0)
        db.add(job)
        db.commit()
        job_id = job.id
        db.close()

        await run_enhancement(job_id)

        db = isolated_backend()
        denoised = db.query(AudioVersion).filter_by(session_id=session_id, version_type="denoised").first()
        for role_name in ("RENDER_SOURCE", "EXPORT_SOURCE"):
            role = db.query(RoleAssignment).filter_by(session_id=session_id, role=role_name).first()
            assert role.audio_version_id == denoised.id
            assert role.upgraded_from is not None, "应该记录是从哪个版本升级过来的"
        # TIMEBASE 不应该被动过——增强不改变时间基准（ADR 里反复强调的不变量）
        timebase = db.query(RoleAssignment).filter_by(session_id=session_id, role="TIMEBASE").first()
        original = db.query(AudioVersion).filter_by(session_id=session_id, version_type="original").first()
        assert timebase.audio_version_id == original.id
        db.close()

    async def test_strong_mode_reports_only_implemented_processing(self, isolated_backend, noisy_webm_file):
        session_id = await self._setup_session_with_original_version(isolated_backend, noisy_webm_file)

        db = isolated_backend()
        job = EnhancementJob(session_id=session_id, mode="strong", status="QUEUED", progress=0.0)
        db.add(job)
        db.commit()
        job_id = job.id
        db.close()

        await run_enhancement(job_id)

        db = isolated_backend()
        job = db.get(EnhancementJob, job_id)
        assert job.actual_chain == ["denoise"]
        assert job.requested_chain == ["denoise"]
        db.close()


class TestGenerateDecisionsAcousticRecheck:
    @staticmethod
    def _write_samples_as_raw(session_id: str, samples) -> None:
        """
        raw_chunk_path 固定以 .webm 结尾，但 encode_wav 写的是 WAV 格式字节——
        ffmpeg 靠内容嗅探格式，不是靠扩展名，所以直接写过去也能被 decode_pcm 正常解码，
        不需要真的转成 webm 容器。
        """
        from pathlib import Path

        audio_analysis.encode_wav(samples, Path(storage.raw_chunk_path(session_id)))

    async def test_confirms_genuine_repeat_and_creates_decision(
        self, isolated_backend, repeat_timeline
    ):
        samples, marks = repeat_timeline
        session_id = _create_session(isolated_backend)
        self._write_samples_as_raw(session_id, samples)

        start_ms, end_ms = marks["phrase_x_2"]
        db = isolated_backend()
        sig = LiveSignal(
            session_id=session_id, signal_level="medium", signal_type="repetition",
            start_ms=int(start_ms), end_ms=int(end_ms), score=0.6, text="这句话这句话",
            invalidated=False,
        )
        db.add(sig)
        db.commit()
        sig_id = sig.id
        db.close()

        await generate_decisions(session_id)

        db = isolated_backend()
        sig = db.get(LiveSignal, sig_id)
        assert sig.invalidated is False
        assert sig.became_final_decision is True

        decisions = db.query(EditDecision).filter_by(session_id=session_id).all()
        assert len(decisions) == 1
        assert "已声学复核" in decisions[0].reason
        db.close()

    async def test_rejects_isolated_signal_with_no_real_repeat(
        self, isolated_backend, repeat_timeline
    ):
        samples, marks = repeat_timeline
        session_id = _create_session(isolated_backend)
        self._write_samples_as_raw(session_id, samples)

        start_ms, end_ms = marks["phrase_y"]
        db = isolated_backend()
        sig = LiveSignal(
            session_id=session_id, signal_level="medium", signal_type="repetition",
            start_ms=int(start_ms), end_ms=int(end_ms), score=0.6, text="孤立内容",
            invalidated=False,
        )
        db.add(sig)
        db.commit()
        sig_id = sig.id
        db.close()

        await generate_decisions(session_id)

        db = isolated_backend()
        sig = db.get(LiveSignal, sig_id)
        assert sig.invalidated is True
        assert "声学复核" in sig.invalidation_reason
        assert sig.became_final_decision is False

        decisions = db.query(EditDecision).filter_by(session_id=session_id).all()
        assert len(decisions) == 0
        db.close()

    async def test_weak_signals_always_invalidated_without_acoustic_check(self, isolated_backend):
        session_id = _create_session(isolated_backend)
        db = isolated_backend()
        sig = LiveSignal(
            session_id=session_id, signal_level="weak", signal_type="filler",
            start_ms=0, end_ms=300, score=0.3, text="嗯", invalidated=False,
        )
        db.add(sig)
        db.commit()
        sig_id = sig.id
        db.close()

        await generate_decisions(session_id)

        db = isolated_backend()
        sig = db.get(LiveSignal, sig_id)
        assert sig.invalidated is True
        assert sig.invalidation_reason == "离线复核未复现该信号"
        db.close()

    async def test_decode_failure_skips_unverifiable_candidate_with_warning(self, isolated_backend):
        """没有音频时不生成无法验证的删除决策。"""
        session_id = _create_session(isolated_backend)
        db = isolated_backend()
        sig = LiveSignal(
            session_id=session_id, signal_level="strong", signal_type="repetition",
            start_ms=1000, end_ms=1300, score=0.7, text="测试", invalidated=False,
        )
        db.add(sig)
        db.commit()
        db.close()

        await generate_decisions(session_id)

        db = isolated_backend()
        decisions = db.query(EditDecision).filter_by(session_id=session_id).all()
        assert decisions == []

        warnings = db.query(WarningModel).filter_by(session_id=session_id).all()
        assert any(w.code == "acoustic_recheck_unavailable" for w in warnings)
        db.close()

    async def test_filler_signals_never_get_acoustic_recheck(self, isolated_backend, clean_webm_file):
        """filler 没法用声学相似度验证——判断"嗯"是不是语气词要靠语义，不靠频谱形状。"""
        session_id = _create_session(isolated_backend)
        _put_audio(session_id, clean_webm_file)

        db = isolated_backend()
        sig = LiveSignal(
            session_id=session_id, signal_level="strong", signal_type="filler",
            start_ms=500, end_ms=800, score=0.8, text="嗯", invalidated=False,
        )
        db.add(sig)
        db.commit()
        db.close()

        await generate_decisions(session_id)

        db = isolated_backend()
        decisions = db.query(EditDecision).filter_by(session_id=session_id).all()
        assert len(decisions) == 1
        assert "已声学复核" not in decisions[0].reason
        db.close()
