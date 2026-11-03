import asyncio

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import EnhancementJob, RecordingSession, VoiceProtectionMetric
from app.models import Warning as WarningModel
from app.schemas import CreateEnhancementIn, EnhancementJobOut, VoiceProtectionMetricOut, WarningOut
from app.services.stub_pipeline import run_enhancement

router = APIRouter(prefix="/api/sessions/{session_id}/enhancement", tags=["enhancement"])

# 防止 asyncio task 被 GC 提前回收
_background_tasks: set[asyncio.Task] = set()


def _spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


@router.post("", response_model=EnhancementJobOut, status_code=202)
async def trigger_enhancement(session_id: str, body: CreateEnhancementIn, db: Session = Depends(get_db)):
    session = db.get(RecordingSession, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    if session.live_analysis_status != "FINALIZED":
        raise HTTPException(status_code=409, detail="recording analysis is not finalized")

    active_job = (
        db.query(EnhancementJob)
        .filter(
            EnhancementJob.session_id == session_id,
            EnhancementJob.status.in_(["QUEUED", "PROCESSING", "QUALITY_CHECKING"]),
        )
        .first()
    )
    if active_job is not None:
        raise HTTPException(status_code=409, detail="an enhancement job is already running")

    mode_map = {"auto": "balanced", "conservative": "light", "aggressive": "strong"}
    resolved_mode = mode_map.get(body.mode, body.mode)

    job = EnhancementJob(session_id=session_id, mode=resolved_mode, status="QUEUED", progress=0.0, declip_mode=body.declip, dereverb=body.dereverb)
    db.add(job)
    session.processing_status = "QUEUED"
    db.commit()
    db.refresh(job)

    _spawn(run_enhancement(job.id))

    return _to_job_out(db, job)


@router.get("", response_model=EnhancementJobOut)
def get_latest_enhancement(session_id: str, db: Session = Depends(get_db)):
    job = (
        db.query(EnhancementJob)
        .filter_by(session_id=session_id)
        .order_by(EnhancementJob.created_at_ns.desc())
        .first()
    )
    if job is None:
        raise HTTPException(status_code=404, detail="no enhancement job for this session yet")
    return _to_job_out(db, job)


@router.get("/{job_id}", response_model=EnhancementJobOut)
def get_enhancement(session_id: str, job_id: str, db: Session = Depends(get_db)):
    job = db.get(EnhancementJob, job_id)
    if job is None or job.session_id != session_id:
        raise HTTPException(status_code=404, detail="job not found")
    return _to_job_out(db, job)


def _to_job_out(db: Session, job: EnhancementJob) -> EnhancementJobOut:
    metrics = db.query(VoiceProtectionMetric).filter_by(job_id=job.id).all()
    warnings = db.query(WarningModel).filter_by(job_id=job.id, scope="enhancement").all()
    data = EnhancementJobOut.model_validate(job)
    data.voice_protection = [VoiceProtectionMetricOut.model_validate(m) for m in metrics]
    data.warnings = [WarningOut.model_validate(w) for w in warnings]
    return data
