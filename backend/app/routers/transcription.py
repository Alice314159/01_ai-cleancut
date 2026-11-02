import asyncio
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from app import config
from app.db import get_db
from app.models import RecordingSession, TranscriptionJob
from app.schemas.transcription import CreateTranscriptionIn, TranscriptionJobOut
from app.services import transcription

router = APIRouter(prefix="/api/sessions/{session_id}/transcription", tags=["transcription"])
_tasks: set[asyncio.Task] = set()


@router.post("", response_model=TranscriptionJobOut, status_code=202)
async def start_transcription(session_id: str, body: CreateTranscriptionIn, db: Session = Depends(get_db)):
    db.execute(text("BEGIN IMMEDIATE"))
    session = db.get(RecordingSession, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    if session.capture_status != "STOPPED" or session.live_analysis_status != "FINALIZED":
        raise HTTPException(status_code=409, detail="请先完成录制分析")
    if db.query(TranscriptionJob).filter(TranscriptionJob.session_id == session_id,
                                        TranscriptionJob.status.in_(transcription.ACTIVE_STATUSES)).first():
        raise HTTPException(status_code=409, detail="该会话已有转写任务，请等待完成")
    source = transcription.source_for_session(db, session_id)
    if source is None or not (Path(config.AUDIO_DIR) / session_id / source.filename).is_file():
        raise HTTPException(status_code=409, detail="转写音频源尚未就绪")
    if not transcription.dependency_available():
        raise HTTPException(status_code=503, detail="未安装本机转写依赖，请在后端环境执行 pip install -r requirements-asr.txt 后重试")
    job = TranscriptionJob(session_id=session_id, source_version_id=source.id,
                           model=config.ASR_MODEL, requested_language=body.language)
    db.add(job)
    db.commit()
    db.refresh(job)
    task = asyncio.create_task(asyncio.to_thread(transcription.run_transcription, job.id))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return job


@router.get("", response_model=TranscriptionJobOut)
def latest_transcription(session_id: str, db: Session = Depends(get_db)):
    job = db.query(TranscriptionJob).filter_by(session_id=session_id).order_by(TranscriptionJob.created_at_ns.desc()).first()
    if job is None:
        raise HTTPException(status_code=404, detail="no transcription yet")
    return job


@router.get("/{job_id}", response_model=TranscriptionJobOut)
def get_transcription(session_id: str, job_id: str, db: Session = Depends(get_db)):
    job = db.get(TranscriptionJob, job_id)
    if job is None or job.session_id != session_id:
        raise HTTPException(status_code=404, detail="transcription not found")
    return job
