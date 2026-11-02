from typing import Literal
from fastapi.responses import Response
from fastapi import APIRouter, Depends, HTTPException, File, Form, UploadFile, BackgroundTasks
from pydantic import ValidationError
from sqlalchemy.orm import Session
from sqlalchemy import text

from app.db import get_db
from app.models import AudioVersion, EditDecision, RecordingSession, RenderOutput, TranscriptToken
from app.schemas import EditDecisionOut, TranscriptTokenOut, UpdateDecisionIn

from app.schemas.audio import AudioVersionOut
from app.schemas.decision import BatchDecisionsIn
from app.schemas.render import CreateManualDecisionIn, RenderOutputOut, RenderOptionsIn
from app.schemas.common import CamelModel
from app.services import rendering, suggestions, transcript_export, retakes, render_transcription
from app.schemas.transcription import CreateTranscriptionIn, TranscriptionJobOut

router = APIRouter(prefix="/api/sessions/{session_id}", tags=["review"])


@router.get("/decisions", response_model=list[EditDecisionOut])
def list_decisions(session_id: str, db: Session = Depends(get_db)):
    if db.get(RecordingSession, session_id) is None:
        raise HTTPException(status_code=404, detail="session not found")
    return db.query(EditDecision).filter_by(session_id=session_id).order_by(EditDecision.start_ms).all()


@router.patch("/decisions/{decision_id}", response_model=EditDecisionOut)
def update_decision(session_id: str, decision_id: str, body: UpdateDecisionIn, db: Session = Depends(get_db)):
    db.execute(text("BEGIN IMMEDIATE"))
    decision = db.get(EditDecision, decision_id)
    if decision is None or decision.session_id != session_id:
        raise HTTPException(status_code=404, detail="decision not found")
    if decision.hard_protected:
        raise HTTPException(status_code=409, detail="this decision is hard-protected and cannot be overridden")
    if body.expected_revision is not None and body.expected_revision != decision.revision:
        raise HTTPException(status_code=409, detail={
            "code": "decision_conflict", "message": "这条决策已被其他操作修改，请选择如何处理",
            "current": EditDecisionOut.model_validate(decision).model_dump(by_alias=True),
        })
    decision.accepted = body.accepted
    decision.revision += 1
    db.commit()
    db.refresh(decision)
    return decision


@router.post("/decisions/confirm", response_model=list[EditDecisionOut])
def confirm_decisions(session_id: str, body: BatchDecisionsIn | None = None, db: Session = Depends(get_db)):
    """
    复核完成：把所有还没表态（accepted is null）的决策按算法默认（非 hard_protected 一律接受）落定，
    渲染为独立的 POST /render，避免确认操作暗中启动耗时处理。
    """
    session = db.get(RecordingSession, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")

    db.rollback()
    db.execute(text("BEGIN IMMEDIATE"))
    check_batch_snapshot(db, session_id, body)
    pending = db.query(EditDecision).filter_by(session_id=session_id, accepted=None).all()
    for d in pending:
        d.accepted = not d.hard_protected
        d.revision += 1
    db.commit()

    return db.query(EditDecision).filter_by(session_id=session_id).order_by(EditDecision.start_ms).all()


@router.get("/transcript", response_model=list[TranscriptTokenOut])
def get_transcript(session_id: str, db: Session = Depends(get_db)):
    if db.get(RecordingSession, session_id) is None:
        raise HTTPException(status_code=404, detail="session not found")
    return db.query(TranscriptToken).filter_by(session_id=session_id).order_by(TranscriptToken.seq).all()


@router.post("/decisions", response_model=EditDecisionOut, status_code=201)
def add_manual_decision(session_id: str, body: CreateManualDecisionIn, db: Session = Depends(get_db)):
    if db.get(RecordingSession, session_id) is None:
        raise HTTPException(status_code=404, detail="session not found")
    try:
        session = rendering.validate_session(db, session_id)
        if body.end_ms > session.total_duration_ms:
            raise rendering.RenderInputError("删除区间超出录音时长")
        existing = rendering.decision_snapshot(db, session_id)
        # Check protection without requiring unrelated pending candidates to be resolved yet.
        protections = [d for d in existing if d["hard_protected"] or d["action"] == "KEEP_PROTECTED"]
        rendering.accepted_cuts(protections + [dict(start_ms=body.start_ms, end_ms=body.end_ms,
                                action="DELETE_SELECTION", accepted=True, hard_protected=False)], session.total_duration_ms)
    except rendering.RenderConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except rendering.RenderInputError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    decision = EditDecision(
        session_id=session_id, start_ms=body.start_ms, end_ms=body.end_ms, action="DELETE_SELECTION",
        reason="手动选择删除区间", text="手动选段", confidence=1.0, source="user",
        hard_protected=False, review_required=False, accepted=True,
    )
    db.add(decision)
    db.commit()
    db.refresh(decision)
    return decision


@router.post("/render", response_model=RenderOutputOut, status_code=201)
def render_audio(session_id: str, background_tasks: BackgroundTasks, body: RenderOptionsIn | None = None, db: Session = Depends(get_db)):
    if db.get(RecordingSession, session_id) is None:
        raise HTTPException(status_code=404, detail="session not found")
    try:
        output = rendering.render_session(db, session_id, smooth_cuts=body.smooth_cuts if body else False)
    except rendering.RenderConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except rendering.RenderInputError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail="成片文件写入失败，原有版本已保留") from exc
    if any(d['action'] == 'REPLACE_AUDIO' and d['accepted'] for d in output.decisions):
        job, started = render_transcription.enqueue(db, session_id, output.version_id)
        if started:
            background_tasks.add_task(render_transcription.run, job.id)
    return _render_out(db, output)


@router.get("/render", response_model=RenderOutputOut)
def latest_render(session_id: str, db: Session = Depends(get_db)):
    if db.get(RecordingSession, session_id) is None:
        raise HTTPException(status_code=404, detail="session not found")
    output = db.query(RenderOutput).filter_by(session_id=session_id).order_by(RenderOutput.id.desc()).first()
    if output is None:
        raise HTTPException(status_code=404, detail="no rendered audio yet")
    return _render_out(db, output)


def _render_out(db: Session, output: RenderOutput) -> RenderOutputOut:
    return RenderOutputOut(
        version=AudioVersionOut.model_validate(db.get(AudioVersion, output.version_id)),
        source_version_id=output.source_version_id, removed_duration_ms=output.removed_duration_ms,
        cut_ranges=output.cut_ranges, stale=rendering.is_stale(db, output),
        transcription_job=render_transcription.latest(db, output.session_id, output.version_id),
        transcript_ready=bool((completed := render_transcription.latest(db, output.session_id, output.version_id, completed=True)) and completed.tokens),
        has_replacements=any(d["action"] == "REPLACE_AUDIO" and d["accepted"] for d in output.decisions),
    )


class SuggestDecisionsIn(CamelModel):
    transcription_job_id: str


@router.post("/decisions/suggest")
def suggest_decisions(session_id: str, body: SuggestDecisionsIn, db: Session = Depends(get_db)):
    try:
        added = suggestions.generate(db, session_id, body.transcription_job_id)
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"addedCount": added, "decisions": [EditDecisionOut.model_validate(d).model_dump(by_alias=True) for d in list_decisions(session_id, db)]}


@router.post("/decisions/reject-suggestions", response_model=list[EditDecisionOut])
def reject_pending_suggestions(session_id: str, body: BatchDecisionsIn | None = None, db: Session = Depends(get_db)):
    if db.get(RecordingSession, session_id) is None:
        raise HTTPException(status_code=404, detail="session not found")
    db.rollback()
    db.execute(text("BEGIN IMMEDIATE"))
    check_batch_snapshot(db, session_id, body)
    db.query(EditDecision).filter_by(
        session_id=session_id, source="transcript_rule", accepted=None, hard_protected=False,
    ).filter(EditDecision.action != "KEEP_PROTECTED").update(
        {EditDecision.accepted: False, EditDecision.revision: EditDecision.revision + 1}, synchronize_session=False,
    )
    db.commit()
    return list_decisions(session_id, db)


@router.get("/transcript/export")
def export_transcript(session_id: str, transcriptionJobId: str, format: Literal["txt", "srt"] = "txt", db: Session = Depends(get_db)):
    session = db.get(RecordingSession, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    try:
        result = transcript_export.export(db, session, transcriptionJobId, format)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return Response(result, media_type="text/plain; charset=utf-8", headers={
        "Content-Disposition": f'attachment; filename="transcript-original.{format}"',
        "Cache-Control": "no-store",
    })


@router.get("/render/{version_id}/subtitles")
def export_render_subtitles(session_id: str, version_id: str, format: Literal["srt", "txt"] = "srt", db: Session = Depends(get_db)):
    output = db.query(RenderOutput).filter_by(session_id=session_id, version_id=version_id).first()
    if output is None:
        raise HTTPException(status_code=404, detail="render not found")
    version = db.get(AudioVersion, version_id)
    try:
        completed = render_transcription.latest(db, session_id, version_id, completed=True)
        if completed:
            result = render_transcription.export(completed, version, format)
        elif format == 'txt':
            raise ValueError('请先完成成片转写后导出 TXT')
        else:
            result = transcript_export.export_render(output, version)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return Response(result, media_type="text/plain; charset=utf-8", headers={
        "Content-Disposition": f'attachment; filename="transcript-master.{format}"', "Cache-Control": "no-store",
    })


def check_batch_snapshot(db, session_id, body):
    if body is None:  # Compatibility for older clients; UI always sends a snapshot.
        return
    current = {d.id: d.revision for d in db.query(EditDecision).filter_by(session_id=session_id).all()}
    if current != body.expected_revisions:
        raise HTTPException(status_code=409, detail="决策列表已变化，本次批量操作未保存。请刷新数据、核对后重试。")


@router.post("/retakes", response_model=EditDecisionOut, status_code=201)
def create_retake(session_id: str, file: UploadFile = File(...), startMs: int = Form(...),
                  endMs: int = Form(...), sourceVersionId: str = Form(...),
                  snapshot: str = Form(...), db: Session = Depends(get_db)):
    if db.get(RecordingSession, session_id) is None:
        raise HTTPException(status_code=404, detail="session not found")
    try:
        revisions = BatchDecisionsIn.model_validate_json(snapshot).expected_revisions
        return retakes.create(db, session_id, file.file, startMs, endMs, sourceVersionId, revisions)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail="剪辑快照格式无效，请刷新后重试") from exc
    except rendering.RenderConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except rendering.RenderInputError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail="重录保存失败，原音频已保留") from exc


@router.post("/render/{version_id}/transcription", response_model=TranscriptionJobOut, status_code=202)
def transcribe_render(session_id: str, version_id: str, body: CreateTranscriptionIn,
                      background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    try:
        job, started = render_transcription.enqueue(db, session_id, version_id, body.language)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if started:
        background_tasks.add_task(render_transcription.run, job.id)
    return job


@router.get("/render/{version_id}/transcript")
def get_render_transcript(session_id: str, version_id: str, db: Session = Depends(get_db)):
    if not db.query(RenderOutput).filter_by(session_id=session_id, version_id=version_id).first():
        raise HTTPException(status_code=404, detail="成片不存在")
    job = render_transcription.latest(db, session_id, version_id)
    completed = render_transcription.latest(db, session_id, version_id, completed=True)
    return {"versionId": version_id, "job": TranscriptionJobOut.model_validate(job).model_dump(by_alias=True) if job else None,
            "transcriptJobId": completed.id if completed else None,
            "tokens": [{"text": t["text"], "startMs": t["start_ms"], "endMs": t["end_ms"]} for t in (completed.tokens or [])] if completed else []}
