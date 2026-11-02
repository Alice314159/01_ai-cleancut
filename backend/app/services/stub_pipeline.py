"""录音分析、谱减法增强与声学候选复核；剪辑渲染在 rendering.py。"""
import asyncio
from weakref import WeakValueDictionary
from pathlib import Path

import numpy as np
from sqlalchemy import text

from app import config, db as db_module
from app.models import (
    AudioQualityReport,
    AudioVersion,
    EditDecision,
    EnhancementJob,
    LiveSignal,
    QualityEvent,
    RecordingSession,
    RoleAssignment,
    TranscriptionJob,
    VoiceProtectionMetric,
)
from app.models import Warning as WarningModel
from app.services import audio_analysis, storage, transcription
from app.ws.connection_manager import manager

# 终态化成功后自动排队的转写任务；保留强引用避免任务在完成前被 GC。
_auto_transcription_tasks: set[asyncio.Task] = set()

# 增强任务处于这几个状态时视为"活动中"；用于重启恢复和删除会话前的活动任务检查。
ACTIVE_ENHANCEMENT_STATUSES = ("QUEUED", "PROCESSING", "QUALITY_CHECKING")

SIGNAL_TO_ACTION = {
    "filler": "DELETE_FILLER",
    "stutter": "DELETE_STUTTER",
    "repetition": "DELETE_REPETITION",
}

# 保留离线流水线任务的强引用，并按 session 去重。WS stop、HTTP 兜底 stop 或客户端重试
# 都可能几乎同时触发；同一会话只能生成一套 original/quality/roles 数据。
_capture_stop_tasks: dict[str, asyncio.Task] = {}


def schedule_capture_stop(session_id: str) -> asyncio.Task:
    existing = _capture_stop_tasks.get(session_id)
    if existing is not None and not existing.done():
        return existing

    task = asyncio.create_task(on_capture_stop(session_id))
    _capture_stop_tasks[session_id] = task

    def _forget(done: asyncio.Task) -> None:
        if _capture_stop_tasks.get(session_id) is done:
            _capture_stop_tasks.pop(session_id, None)

    task.add_done_callback(_forget)
    return task


async def on_capture_stop(session_id: str) -> None:
    """
    Analyze persisted capture; publish readiness only for decodable audio.

    幂等发布：claim 阶段在写事务里原子地把 live_analysis_status 从"未开始/已失败"
    切到 FINALIZING，FINALIZED/FINALIZING 都直接返回。WS 断线兜底、HTTP /stop 兜底
    和导入接口都可能对同一 session_id 并发调用这个函数；不加这道锁的话，两个调用会各自
    解码一遍、各插入一套 AudioVersion/AudioQualityReport/角色，产生重复数据。
    `_capture_stop_tasks` 只在进程内对 WS 路径去重，覆盖不到直接调用的导入路径，
    所以这层保护必须做在函数内部、以数据库行锁为准。
    """
    db = db_module.SessionLocal()
    try:
        db.rollback()
        db.execute(text("BEGIN IMMEDIATE"))
        session = db.get(RecordingSession, session_id)
        if session is None or session.live_analysis_status in ("FINALIZED", "FINALIZING"):
            db.rollback()
            return
        session.capture_status = "STOPPED"
        session.live_analysis_status = "FINALIZING"
        session.server_merge_status = "MERGING"
        db.commit()
        await manager.send_json(session_id, {"type": "stage", "stage": "merge", "status": "MERGING"})

        samples = await asyncio.to_thread(audio_analysis.decode_pcm, Path(storage.raw_chunk_path(session_id)))
        metrics = await asyncio.to_thread(audio_analysis.analyze_samples, samples)
        duration_ms = int(len(samples) / audio_analysis.ANALYSIS_SAMPLE_RATE * 1000)
        original = AudioVersion(
            session_id=session_id, version_type="original", label="原始录音",
            duration_ms=duration_ms,
            sample_count=round(len(samples) * session.actual_sample_rate / audio_analysis.ANALYSIS_SAMPLE_RATE),
            sample_rate=session.actual_sample_rate, latency_ms=0,
            engine="capture", engine_version="1.0", determinism="deterministic",
            processing_locality="local", filename="raw.webm",
        )
        db.add(original)
        db.flush()
        for role in ("TIMEBASE", "ASR_SOURCE", "REVIEW_SOURCE", "RENDER_SOURCE", "EXPORT_SOURCE"):
            db.add(RoleAssignment(session_id=session_id, role=role, audio_version_id=original.id))
        db.add(AudioQualityReport(
            session_id=session_id, quality_grade=metrics.quality_grade, grade_reason=metrics.grade_reason,
            integrated_lufs=metrics.integrated_lufs, true_peak_dbtp=metrics.true_peak_dbtp,
            noise_floor_dbfs=metrics.noise_floor_dbfs, estimated_snr_db=metrics.estimated_snr_db,
            clipping_ratio=metrics.clipping_ratio, hum_probability=metrics.hum_probability,
            reverb_score=metrics.reverb_score, speech_coverage=metrics.speech_coverage,
            silence_coverage=metrics.silence_coverage, noise_types=metrics.noise_types,
            content_type="unknown", content_type_confidence=0.0, content_type_overridden_by_user=False,
        ))
        session.clock_drift = None
        session.total_duration_ms = duration_ms
        session.committed_duration_ms = duration_ms
        session.local_persistence_status = "NOT_SUPPORTED"
        session.upload_status = "COMPLETED"
        session.server_merge_status = "VERIFIED"
        session.live_analysis_status = "FINALIZED"
        session.processing_status = "NOT_STARTED"
        db.commit()
        await manager.send_json(session_id, {"type": "stage", "stage": "finalized", "status": "READY"})
        _auto_start_transcription(session_id, original.id)
    except Exception as exc:
        db.rollback()
        session = db.get(RecordingSession, session_id)
        if session is not None:
            session.capture_status = "STOPPED"
            session.live_analysis_status = "FAILED"
            session.server_merge_status = "FAILED"
            session.processing_status = "FAILED"
            db.add(WarningModel(
                session_id=session_id, scope="session", code="quality_analysis_failed",
                message=f"录音分析失败，未生成质量报告：{str(exc)[:300]}", stage="merge",
            ))
            db.commit()
            await manager.send_json(session_id, {"type": "stage", "stage": "finalized", "status": "FAILED"})
    finally:
        db.close()


def _auto_start_transcription(session_id: str, source_version_id: str) -> None:
    """
    录制停止或导入终态化成功后自动排队一次本机转写（语言自动识别），不需要用户先点
    "开始本机转写"。没装 faster-whisper 依赖时静默跳过——复核页的按钮仍然可用，
    用户手动点击时会看到明确的 503 提示，这里不重复报错。
    """
    if not transcription.dependency_available():
        return
    with db_module.SessionLocal() as db:
        job = TranscriptionJob(session_id=session_id, source_version_id=source_version_id,
                               model=config.ASR_MODEL, requested_language="auto")
        db.add(job)
        db.commit()
        job_id = job.id
    task = asyncio.create_task(asyncio.to_thread(transcription.run_transcription, job_id))
    _auto_transcription_tasks.add(task)
    task.add_done_callback(_auto_transcription_tasks.discard)


async def run_enhancement(job_id: str) -> None:
    db = db_module.SessionLocal()
    try:
        job = db.get(EnhancementJob, job_id)
        if job is None or job.status != "QUEUED":
            return
        session_id = job.session_id

        job.status = "PROCESSING"
        session = db.get(RecordingSession, session_id)
        session.processing_status = "PROCESSING"
        chain = [] if job.mode == "off" else ["denoise"]
        job.requested_chain = chain
        db.commit()
        await manager.send_json(session_id, {"type": "enhancement_progress", "jobId": job_id, "progress": 0.1})

        raw_path = Path(storage.raw_chunk_path(session_id))
        try:
            samples = await asyncio.to_thread(audio_analysis.decode_pcm, raw_path)
            job.progress = 0.35
            db.commit()
            await manager.send_json(session_id, {"type": "enhancement_progress", "jobId": job_id, "progress": 0.35})

            # 强度越高，过减因子越大——真的会影响降噪算法的行为，不是只改个数字
            over_subtraction = {"light": 1.2, "balanced": 1.8, "strong": 2.4}.get(job.mode, 1.8)
            denoised_samples = samples.copy() if job.mode == "off" else await asyncio.to_thread(
                audio_analysis.spectral_subtract_denoise, samples, sample_rate=audio_analysis.ANALYSIS_SAMPLE_RATE, over_subtraction=over_subtraction
            )
            job.progress = 0.7
            db.commit()
            await manager.send_json(session_id, {"type": "enhancement_progress", "jobId": job_id, "progress": 0.7})

            vp = await asyncio.to_thread(
                audio_analysis.compute_voice_protection, samples, denoised_samples, sample_rate=audio_analysis.ANALYSIS_SAMPLE_RATE
            )

            denoised_path = Path(storage.session_dir(session_id)) / f"denoised_{job_id}.wav"
            await asyncio.to_thread(audio_analysis.encode_wav, denoised_samples, denoised_path, sample_rate=audio_analysis.ANALYSIS_SAMPLE_RATE)
        except audio_analysis.DecodeError as exc:
            raise RuntimeError(f"无法解码原始音频，未生成增强版本：{exc}") from exc

        job.status = "QUALITY_CHECKING"
        session.processing_status = "QUALITY_CHECKING"
        db.commit()
        await manager.send_json(session_id, {"type": "enhancement_progress", "jobId": job_id, "progress": 0.9})

        # 语音保护指标（ADR-044）：复合判据，不能只看降噪量。这些数值是真的从
        # 降噪前后两段音频算出来的，不是随机生成——见 audio_analysis.compute_voice_protection。
        metrics = [
            ("timbre_similarity", "音色相似度", vp.timbre_similarity, "cosine", ">=0.9", vp.timbre_similarity >= 0.9, True),
            (
                "spectral_centroid_shift",
                "频谱质心偏移",  # 不叫"基频偏移"：这不是逐帧基频跟踪，是更粗的频谱代理指标
                vp.spectral_centroid_shift_pct,
                "%",
                "<=15%",
                vp.spectral_centroid_shift_pct <= 15,
                True,
            ),
            (
                "dynamic_range_retained",
                "动态范围保留",
                vp.dynamic_range_retained,
                "ratio",
                ">=0.8",
                vp.dynamic_range_retained >= 0.8,
                True,
            ),
            ("artifact_score", "伪影评分", vp.artifact_score, "score", "<=0.15", vp.artifact_score <= 0.15, False),
        ]
        noise_reduction_db = vp.noise_reduction_db
        await generate_decisions(session_id)
        for key, label, value, unit, target, passed, hard in metrics:
            db.add(
                VoiceProtectionMetric(
                    job_id=job_id, key=key, label=label, value=value, unit=unit, target=target,
                    passed=passed, hard=hard,
                )
            )
        db.flush()

        hard_metrics_passed = all(m[5] for m in metrics if m[6])
        job.enhancement_success = hard_metrics_passed
        job.noise_reduction_db = noise_reduction_db
        job.actual_chain = chain

        # 产出一个新的音频版本，接管 RENDER_SOURCE / EXPORT_SOURCE 角色
        session = db.get(RecordingSession, session_id)
        denoised = AudioVersion(
            session_id=session_id,
            version_type="denoised",
            label="未降噪副本" if job.mode == "off" else "降噪版本",
            duration_ms=int(len(denoised_samples) / audio_analysis.ANALYSIS_SAMPLE_RATE * 1000),
            sample_count=len(denoised_samples),
            sample_rate=audio_analysis.ANALYSIS_SAMPLE_RATE,
            latency_ms=0,
            engine="pcm_passthrough" if job.mode == "off" else "spectral_subtraction",
            engine_version="1.0",
            determinism="deterministic",
            processing_locality="local",
            filename=denoised_path.name,
        )
        db.add(denoised)
        db.flush()

        job.result_version_id = denoised.id
        if not hard_metrics_passed:
            db.add(WarningModel(
                session_id=session_id, job_id=job_id, scope="enhancement",
                code="voice_protection_failed", stage="enhancement",
                message="人声保护检查未通过，保留原有输出音频；本次结果仅供试听。",
            ))
        for role in (("RENDER_SOURCE", "EXPORT_SOURCE") if hard_metrics_passed else ()):
            existing = db.query(RoleAssignment).filter_by(session_id=session_id, role=role).first()
            upgraded_from = existing.audio_version_id if existing else None
            if existing:
                db.delete(existing)
                db.flush()
            db.add(
                RoleAssignment(
                    session_id=session_id, role=role, audio_version_id=denoised.id, upgraded_from=upgraded_from
                )
            )

        job.status = "COMPLETED"
        job.progress = 1.0
        session.processing_status = "DONE"
        db.commit()

        await manager.send_json(session_id, {"type": "enhancement_progress", "jobId": job_id, "progress": 1.0})
        await manager.send_json(session_id, {"type": "enhancement_done", "jobId": job_id, "status": "COMPLETED"})

    except Exception as exc:
        db.rollback()
        job = db.get(EnhancementJob, job_id)
        if job is not None:
            job.status = "FAILED"
            job.enhancement_success = False
            job.error_message = str(exc)[:1000]
            session = db.get(RecordingSession, job.session_id)
            if session is not None:
                session.processing_status = "FAILED"
            db.commit()
            await manager.send_json(
                job.session_id,
                {"type": "enhancement_done", "jobId": job_id, "status": "FAILED"},
            )
    finally:
        db.close()


_decision_locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()


async def generate_decisions(session_id: str) -> None:
    lock = _decision_locks.setdefault(session_id, asyncio.Lock())
    async with lock:
        await _generate_decisions(session_id)


async def _generate_decisions(session_id: str) -> None:
    """
    把录制期间收集的 LiveSignal 提升成正式的 EditDecision（ADR-053：实时信号只是提示，
    真正的裁剪判定在这一步、基于完整上下文重新计算）。

    对 repetition/stutter 这两类信号，这里会用真实的声学自相似度（见
    audio_analysis.find_best_self_similarity）做一次复核——检查目标片段前面是不是真的
    出现过一段声学上很像的音频，而不是原样相信模拟阶段生成的置信度。相似度不够高的
    会被标记失效，不会产生剪辑决策。语气词（filler）没法这样验证——判断"嗯"是不是语气词
    要靠语义，不是靠声学相似度，这里仍然是纯规则。

    候选文字不是 ASR 转写，不生成 TranscriptToken。
    """
    db = db_module.SessionLocal()
    try:
        signals = db.query(LiveSignal).filter_by(session_id=session_id).order_by(LiveSignal.start_ms).all()
        if not any(not sig.became_final_decision and not sig.invalidated for sig in signals):
            return

        samples = None
        try:
            samples = await asyncio.to_thread(audio_analysis.decode_pcm, Path(storage.raw_chunk_path(session_id)))
        except audio_analysis.DecodeError:
            db.add(
                WarningModel(
                    session_id=session_id,
                    scope="task",
                    code="acoustic_recheck_unavailable",
                    message="音频解码失败，无法验证的候选不会生成删除决策",
                    stage="decisions",
                )
            )

        LOOKBACK_MS = {"repetition": 4000, "stutter": 800}

        for sig in signals:
            if sig.became_final_decision or sig.invalidated:
                continue
            if sig.signal_level == "weak":
                # 弱信号在离线复核阶段被判定为误报，标记失效但不产生决策
                sig.invalidated = True
                sig.invalidation_reason = "离线复核未复现该信号"
                sig.became_final_decision = False
                continue

            confidence = sig.score
            reason = f"候选{sig.signal_type}，尚未验证语义，评分 {sig.score:.2f}"

            if samples is None:
                sig.invalidated = True
                sig.invalidation_reason = "音频不可用，无法验证候选"
                sig.became_final_decision = False
                continue

            if sig.signal_type in LOOKBACK_MS:
                result = await asyncio.to_thread(
                    audio_analysis.find_best_self_similarity, samples,
                    audio_analysis.ANALYSIS_SAMPLE_RATE,
                    sig.start_ms,
                    sig.end_ms,
                    lookback_ms=LOOKBACK_MS[sig.signal_type],
                )
                if result is None:
                    sig.invalidated = True
                    sig.invalidation_reason = "声学复核不可用：片段越界或缺少可比较的前文"
                    sig.became_final_decision = False
                    continue
                if result is not None:
                    similarity, offset_ms = result
                    if similarity < 0.5:
                        # 声学复核没有找到相似片段——原始信号大概率是误报，不产生决策
                        sig.invalidated = True
                        sig.invalidation_reason = f"声学复核相似度仅 {similarity:.2f}，未复现{sig.signal_type}模式"
                        sig.became_final_decision = False
                        db.commit()
                        continue
                    # 复核通过：用真实测得的相似度替换模拟置信度，更可信
                    confidence = round((sig.score + similarity) / 2, 2)
                    reason = f"检测到{sig.signal_type}，与 {offset_ms}ms 前的片段声学相似度 {similarity:.2f}（已声学复核）"

            action = SIGNAL_TO_ACTION.get(sig.signal_type, "DELETE_FILLER")
            hard_protected = sig.signal_type == "repetition" and confidence < 0.5
            db.add(
                EditDecision(
                    session_id=session_id,
                    start_ms=sig.start_ms,
                    end_ms=sig.end_ms,
                    action=action,
                    reason=reason,
                    text=sig.text,
                    confidence=confidence,
                    source="algorithm",
                    hard_protected=hard_protected,
                    review_required=True,
                    accepted=None,
                )
            )
            sig.became_final_decision = True

        db.commit()
    finally:
        db.close()


async def simulate_capture_signals(session_id: str, stop_event: asyncio.Event) -> None:
    """定期检测真实音频质量，不生成模拟语义候选。"""
    db = db_module.SessionLocal()
    t_ms = 0
    raw_path = Path(storage.raw_chunk_path(session_id))
    try:
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=2.5)
                break
            except asyncio.TimeoutError:
                pass

            if stop_event.is_set():
                break
            db.expire_all()
            session = db.get(RecordingSession, session_id)
            if session is None or session.capture_status == "STOPPED":
                break
            if session.capture_status != "RECORDING":
                continue  # 暂停期间不推进已确认时长，也不产生新信号
            t_ms += 2500
            session.committed_duration_ms = t_ms
            db.commit()

            # 尝试解码最近几秒的真实音频。刚开始录制、文件还没落盘完整时可能解码失败，
            # 失败就跳过这一轮检测（不用假数据顶替），电平表退回一个中性占位值。
            segment = None
            try:
                segment = await asyncio.to_thread(audio_analysis.decode_tail_pcm, raw_path, seconds=3.0)
            except audio_analysis.DecodeError:
                pass

            if stop_event.is_set():
                break
            if segment is not None and len(segment) > 0:
                rms = float(np.sqrt(np.mean(segment.astype(np.float64) ** 2) + 1e-12))
                peak = float(np.max(np.abs(segment)))
            else:
                rms, peak = 0.0, 0.0

            await manager.send_json(
                session_id,
                {
                    "type": "level",
                    "rms": round(min(rms, 1.0), 3),
                    "peak": round(min(peak, 1.0), 3),
                    "committedDurationMs": t_ms,
                },
            )

            if segment is not None and len(segment) > 0:
                for ev in audio_analysis.detect_realtime_quality_events(segment):
                    qe = QualityEvent(
                        session_id=session_id,
                        event_type=ev.event_type,
                        severity=ev.severity,
                        client_timestamp_ms=t_ms,
                        message=ev.message,
                        metrics=ev.metrics,
                    )
                    db.add(qe)
                    db.commit()
                    await manager.send_json(
                        session_id,
                        {
                            "type": "quality_event",
                            "id": qe.id,
                            "eventType": qe.event_type,
                            "severity": qe.severity,
                            "clientTimestampMs": qe.client_timestamp_ms,
                            "message": qe.message,
                            "metrics": qe.metrics,
                        },
                    )

    except asyncio.CancelledError:
        pass
    finally:
        db.close()


def recover_interrupted_enhancements():
    """Single-worker startup cleanup; keep published files and role assignments."""
    with db_module.SessionLocal() as db:
        jobs = db.query(EnhancementJob).filter(EnhancementJob.status.in_(ACTIVE_ENHANCEMENT_STATUSES)).all()
        for job in jobs:
            job.status = "FAILED"
            job.error_message = "服务重启中断了增强，请重试。此前音频版本已保留。"
            session = db.get(RecordingSession, job.session_id)
            if session:
                session.processing_status = "FAILED"
        db.commit()


def recover_interrupted_finalizations():
    """
    服务重启时清理两类只应该在单次请求/任务生命周期内短暂存在、不该跨进程重启存活的
    中间状态，否则前端会看到一个永远不会自己变化的会话：

    - live_analysis_status == FINALIZING：on_capture_stop 的解码/质量分析被异常中断；
      raw 音频已经是最终内容，按现有解码失败语义标记 FAILED，用户可重新录制/导入。
    - capture_status == IMPORTING：导入接口的上传或发布阶段被异常中断；这一步还没有
      调用过 on_capture_stop（否则 capture_status 早被改成 STOPPED 了），
      真正发布的 raw.webm 还不存在或仍是旧内容，直接退回 IDLE 允许重新导入，
      顺带清掉可能残留的临时文件。
    """
    with db_module.SessionLocal() as db:
        finalizing = db.query(RecordingSession).filter(RecordingSession.live_analysis_status == "FINALIZING").all()
        for session in finalizing:
            session.capture_status = "STOPPED"
            session.live_analysis_status = "FAILED"
            session.server_merge_status = "FAILED"
            session.processing_status = "FAILED"
            db.add(WarningModel(
                session_id=session.id, scope="session", code="quality_analysis_failed",
                message="录音分析在上次服务重启时被中断，请重新录制或重新导入。", stage="merge",
            ))

        importing = db.query(RecordingSession).filter(RecordingSession.capture_status == "IMPORTING").all()
        for session in importing:
            session.capture_status = "IDLE"
            temp_path = Path(storage.raw_chunk_path(session.id) + ".importing")
            temp_path.unlink(missing_ok=True)

        db.commit()
