from app.models import RenderTranscriptionJob
import os
import shutil

from fastapi import APIRouter, Depends, File, HTTPException, Query, Response, UploadFile
from fastapi.responses import FileResponse
from pathlib import Path
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import (
    AudioQualityReport,
    AudioVersion,
    EditDecision,
    EnhancementJob,
    LiveSignal,
    QualityEvent,
    RecordingSession,
    RenderOutput,
    RoleAssignment,
    TranscriptToken,
    TranscriptionJob,
    VoiceProtectionMetric,
    Warning as WarningModel,
)
from app.schemas import (
    AudioQualityReportOut,
    AudioVersionOut,
    CreateSessionIn,
    RecordingSessionListItemOut,
    RecordingSessionOut,
    RoleAssignmentOut,
    UpdateSessionIn,
    WarningOut,
)
from app.services import audio_analysis, storage, stub_pipeline, transcription
from app.routers.recording_ws import stop_recording

router = APIRouter(prefix="/api/sessions", tags=["sessions"])

# 本地单人工具，不需要精细限流；只是防止误传一个几个 GB 的视频文件占满磁盘/长时间占用 ffmpeg。
MAX_IMPORT_BYTES = 300 * 1024 * 1024


def _get_session_or_404(db: Session, session_id: str) -> RecordingSession:
    session = db.get(RecordingSession, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    return session


@router.post("", response_model=RecordingSessionOut, status_code=201)
def create_session(body: CreateSessionIn, db: Session = Depends(get_db)):
    session = RecordingSession(
        project_id=body.project_id,
        capture_mode=body.capture_mode,
        analysis_locality=body.analysis_locality,
        device_label=body.device_label,
        title=body.title,
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return _to_session_out(db, session)


@router.get("", response_model=list[RecordingSessionListItemOut])
def list_sessions(db: Session = Depends(get_db)):
    # 新会话在前。id 是随机 hex，按它排序会让新建会话出现在列表任意位置；
    # 旧库迁移的会话 created_at_ns 都是 0，再按 rowid 倒序保持插入顺序。
    return (
        db.query(RecordingSession)
        .order_by(RecordingSession.created_at_ns.desc(), text("recording_sessions.rowid DESC"))
        .all()
    )


@router.get("/{session_id}", response_model=RecordingSessionOut)
def get_session(session_id: str, db: Session = Depends(get_db)):
    session = _get_session_or_404(db, session_id)
    return _to_session_out(db, session)


@router.patch("/{session_id}", response_model=RecordingSessionOut)
def update_session(session_id: str, body: UpdateSessionIn, db: Session = Depends(get_db)):
    """重命名标题或者改到另一个项目。不改音频、决策或任何处理状态。"""
    session = _get_session_or_404(db, session_id)
    if body.title is not None:
        if not body.title.strip():
            raise HTTPException(status_code=422, detail="标题不能为空")
        session.title = body.title.strip()
    if body.project_id is not None:
        if not body.project_id.strip():
            raise HTTPException(status_code=422, detail="项目不能为空")
        session.project_id = body.project_id.strip()
    db.commit()
    db.refresh(session)
    return _to_session_out(db, session)


@router.delete("/{session_id}", status_code=204)
def delete_session(session_id: str, db: Session = Depends(get_db)):
    """
    彻底删除会话：清掉所有关联表的行（没有配置外键级联，SQLite 也没开外键约束，
    留着不删会变成孤儿数据）和磁盘上的音频目录。不可撤销，前端需要先让用户确认。

    活动任务期间禁止删除：录制/暂停/导入中，或终态化、增强、转写任一后台任务还在跑时，
    都拒绝。这些任务持有 session_id 引用、会在完成时继续写库或读写磁盘文件；
    若此时把行和目录删掉，任务收尾时会在 SQLite 未开外键约束的情况下插出孤儿数据，
    或者试图读取已被 rmtree 的音频文件而报错。检查和级联删除放在同一个写事务里，
    避免检查通过后、真正删除前又有新任务插进来的竞态窗口。
    """
    db.rollback()
    db.execute(text("BEGIN IMMEDIATE"))
    session = db.get(RecordingSession, session_id)
    if session is None:
        db.rollback()
        raise HTTPException(status_code=404, detail="session not found")
    if session.capture_status in ("RECORDING", "PAUSED", "IMPORTING"):
        db.rollback()
        raise HTTPException(status_code=409, detail="会话正在录制或导入中，请先停止/等待完成再删除")
    if session.live_analysis_status == "FINALIZING":
        db.rollback()
        raise HTTPException(status_code=409, detail="会话正在终态化处理中，请稍候再删除")
    if db.query(EnhancementJob.id).filter_by(session_id=session_id).filter(
        EnhancementJob.status.in_(stub_pipeline.ACTIVE_ENHANCEMENT_STATUSES)
    ).first():
        db.rollback()
        raise HTTPException(status_code=409, detail="会话有增强任务正在处理，请稍候再删除")
    if db.query(TranscriptionJob.id).filter_by(session_id=session_id).filter(
        TranscriptionJob.status.in_(transcription.ACTIVE_STATUSES)
    ).first():
        db.rollback()
        raise HTTPException(status_code=409, detail="会话有转写任务正在处理，请稍候再删除")
    if db.query(RenderTranscriptionJob.id).filter_by(session_id=session_id).filter(
        RenderTranscriptionJob.status.in_(transcription.ACTIVE_STATUSES)
    ).first():
        db.rollback()
        raise HTTPException(status_code=409, detail="成片正在转写，请等待完成再删除")
    db.query(RenderTranscriptionJob).filter_by(session_id=session_id).delete(synchronize_session=False)
    job_ids = [row[0] for row in db.query(EnhancementJob.id).filter_by(session_id=session_id).all()]
    if job_ids:
        db.query(VoiceProtectionMetric).filter(VoiceProtectionMetric.job_id.in_(job_ids)).delete(synchronize_session=False)
    db.query(WarningModel).filter_by(session_id=session_id).delete(synchronize_session=False)
    db.query(TranscriptToken).filter_by(session_id=session_id).delete(synchronize_session=False)
    db.query(RenderOutput).filter_by(session_id=session_id).delete(synchronize_session=False)
    db.query(TranscriptionJob).filter_by(session_id=session_id).delete(synchronize_session=False)
    db.query(EditDecision).filter_by(session_id=session_id).delete(synchronize_session=False)
    db.query(EnhancementJob).filter_by(session_id=session_id).delete(synchronize_session=False)
    db.query(AudioQualityReport).filter_by(session_id=session_id).delete(synchronize_session=False)
    db.query(RoleAssignment).filter_by(session_id=session_id).delete(synchronize_session=False)
    db.query(LiveSignal).filter_by(session_id=session_id).delete(synchronize_session=False)
    db.query(QualityEvent).filter_by(session_id=session_id).delete(synchronize_session=False)
    db.query(AudioVersion).filter_by(session_id=session_id).delete(synchronize_session=False)
    db.delete(session)
    db.commit()
    shutil.rmtree(storage.session_dir(session_id), ignore_errors=True)
    return Response(status_code=204)


@router.post("/{session_id}/stop", response_model=RecordingSessionOut)
async def stop_session(session_id: str, db: Session = Depends(get_db)):
    """
    HTTP 兜底：正常应通过 WS 的 {"type":"control","action":"stop"} 触发停止+离线流水线。
    这个端点用于客户端异常断线后，仍能把会话标记为已停止。
    """
    session = _get_session_or_404(db, session_id)
    stop_recording(db, session_id)
    return _to_session_out(db, session)


@router.post("/{session_id}/import", response_model=RecordingSessionOut, status_code=201)
async def import_audio(session_id: str, file: UploadFile = File(...), db: Session = Depends(get_db)):
    """
    录后处理会话导入已有音频文件，跳过实时录制阶段。写入后复用与 WS 停止录制
    完全相同的终态化流程（on_capture_stop）：解码、质量分析、生成原始版本与角色，
    解码失败会像正常录制一样标记 FAILED，不生成伪造的质量报告。

    抢占状态：真正开始接收上传前，先在写事务里把 capture_status 从 IDLE 原子切到
    IMPORTING，再提交、释放写锁（上传本身可能很慢，不能让它占着 SQLite 唯一的写锁）。
    第二个并发的导入请求会在这一步就看到非 IDLE 状态被拒绝，不会跟第一个请求同时
    写同一个文件。临时文件原子发布：上传内容先写到 raw.webm.importing，全部收到、
    大小校验通过后才用 os.replace 原子改名成 raw.webm；中途失败或异常，raw.webm
    要么不存在、要么还是上一次的内容，不会停在半写状态，并把状态退回 IDLE 允许重试。
    """
    db.rollback()
    db.execute(text("BEGIN IMMEDIATE"))
    session = db.get(RecordingSession, session_id)
    if session is None:
        db.rollback()
        raise HTTPException(status_code=404, detail="session not found")
    if session.capture_mode != "batch":
        db.rollback()
        raise HTTPException(status_code=409, detail="仅“录后处理”会话支持导入音频")
    if session.capture_status != "IDLE":
        db.rollback()
        raise HTTPException(status_code=409, detail="该会话已导入过音频，无法重复导入")
    session.capture_status = "IMPORTING"
    db.commit()

    final_path = Path(storage.raw_chunk_path(session_id))
    temp_path = final_path.parent / (final_path.name + ".importing")
    try:
        size = 0
        with temp_path.open("wb") as f:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_IMPORT_BYTES:
                    raise HTTPException(status_code=413, detail="音频文件超过导入大小上限（300MB）")
                f.write(chunk)
        if size == 0:
            raise HTTPException(status_code=422, detail="上传的文件为空")
    except Exception:
        temp_path.unlink(missing_ok=True)
        db.rollback()
        db.execute(text("BEGIN IMMEDIATE"))
        session = db.get(RecordingSession, session_id)
        if session is not None and session.capture_status == "IMPORTING":
            session.capture_status = "IDLE"
        db.commit()
        raise

    os.replace(temp_path, final_path)

    try:
        sample_rate, channels = audio_analysis.probe_format(final_path)
    except audio_analysis.DecodeError:
        pass  # 元数据展示用；真正的可用性判断交给下面的 on_capture_stop 解码
    else:
        if 8000 <= sample_rate <= 384000:
            session.actual_sample_rate = sample_rate
        if 1 <= channels <= 32:
            session.channel_count = channels
    if file.content_type:
        session.mime_type = file.content_type[:100]
    db.commit()

    await stub_pipeline.on_capture_stop(session_id)
    db.expire_all()
    return _to_session_out(db, _get_session_or_404(db, session_id))


@router.get("/{session_id}/versions", response_model=list[AudioVersionOut])
def list_versions(session_id: str, db: Session = Depends(get_db)):
    _get_session_or_404(db, session_id)
    return db.query(AudioVersion).filter_by(session_id=session_id).order_by(AudioVersion.id).all()


@router.get("/{session_id}/versions/{version_id}/audio")
def get_version_audio(session_id: str, version_id: str, download: bool = False, db: Session = Depends(get_db)):
    """
    真实的音频文件流——不是占位符。原始版本是浏览器录的 webm，
    降噪版本是谱减法算法实际处理出来的 wav，两者都能直接在 <audio> 里播放对比。
    """
    session = _get_session_or_404(db, session_id)
    version = db.get(AudioVersion, version_id)
    if version is None or version.session_id != session_id:
        raise HTTPException(status_code=404, detail="version not found")
    if not version.filename:
        raise HTTPException(status_code=404, detail="this version has no audio file")

    path = Path(storage.session_dir(session_id)) / version.filename
    if not path.exists():
        raise HTTPException(status_code=404, detail="audio file missing on disk")

    media_type = "audio/wav" if path.suffix == ".wav" else session.mime_type.split(";")[0]
    return FileResponse(
        path,
        media_type=media_type,
        filename=f"{version.version_type}{path.suffix}",
        content_disposition_type="attachment" if download else "inline",
    )


@router.get("/{session_id}/roles", response_model=list[RoleAssignmentOut])
def list_roles(session_id: str, db: Session = Depends(get_db)):
    _get_session_or_404(db, session_id)
    return db.query(RoleAssignment).filter_by(session_id=session_id).all()


@router.get("/{session_id}/quality", response_model=AudioQualityReportOut)
def get_quality_report(session_id: str, db: Session = Depends(get_db)):
    _get_session_or_404(db, session_id)
    report = db.get(AudioQualityReport, session_id)
    if report is None:
        raise HTTPException(status_code=404, detail="quality report not ready yet")
    return report


@router.get("/{session_id}/warnings", response_model=list[WarningOut])
def list_session_warnings(session_id: str, db: Session = Depends(get_db)):
    """
    复核页用这个端点展示"本次处理的提醒"，所以包含 session 和 task 两种 scope——
    前者是录制/合并阶段的警告，后者是决策生成阶段的警告（比如声学复核跳过了）。
    RecordingSessionOut.warnings 字段（会话详情里嵌的那份）只放 session scope，
    两处语义不同，不要合并成一个查询。
    """
    _get_session_or_404(db, session_id)
    return (
        db.query(WarningModel)
        .filter(WarningModel.session_id == session_id, WarningModel.scope.in_(["session", "task"]))
        .all()
    )


@router.get("/{session_id}/waveform")
def get_waveform(session_id: str, buckets: int = Query(default=200, ge=1, le=2000), db: Session = Depends(get_db)):
    """
    真实录音的振幅包络（从实际音频文件解码计算，不是占位曲线）。
    录制还没停止或文件还不存在时返回 404，前端应回落到占位波形。
    """
    _get_session_or_404(db, session_id)
    raw_path = Path(storage.raw_chunk_path(session_id))
    try:
        envelope = audio_analysis.compute_envelope(raw_path, buckets=buckets)
    except audio_analysis.DecodeError:
        raise HTTPException(status_code=404, detail="waveform not available yet")
    return {"envelope": envelope}


def _to_session_out(db: Session, session: RecordingSession) -> RecordingSessionOut:
    warnings = db.query(WarningModel).filter_by(session_id=session.id, scope="session").all()
    data = RecordingSessionOut.model_validate(session)
    data.warnings = [WarningOut.model_validate(w) for w in warnings]
    return data
