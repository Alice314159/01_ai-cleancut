"""说话人：起名字、手动更正某段是谁说的、重新分析。"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import AudioQualityReport, RecordingSession
from app.schemas.quality import AssignSpeakerIn, AudioQualityReportOut, SpeakerNamesIn
from app.services import diarization

router = APIRouter(prefix="/api/sessions/{session_id}/speakers", tags=["speakers"])


def quality_out(report: AudioQualityReport) -> AudioQualityReportOut:
    return AudioQualityReportOut.model_validate(report).model_copy(update={
        "speaker_model_available": diarization.available(),
        "speakers": diarization.speakers_in(report.speaker_segments),
    })


def _locked_report(db: Session, session_id: str) -> AudioQualityReport:
    db.rollback()
    db.execute(text("BEGIN IMMEDIATE"))
    if db.get(RecordingSession, session_id) is None:
        db.rollback()
        raise HTTPException(status_code=404, detail="session not found")
    report = db.get(AudioQualityReport, session_id)
    if report is None:
        db.rollback()
        raise HTTPException(status_code=409, detail="录音分析尚未完成")
    return report


@router.put("/names", response_model=AudioQualityReportOut)
def rename_speakers(session_id: str, body: SpeakerNamesIn, db: Session = Depends(get_db)):
    """整体替换名字表；空名字表示恢复“说话人 A”。"""
    report = _locked_report(db, session_id)
    names: dict[str, str] = {}
    for key, value in body.names.items():
        if not key.isdigit() or int(key) > 99:
            db.rollback()
            raise HTTPException(status_code=422, detail="说话人编号无效")
        value = " ".join(value.split())
        if len(value) > diarization.MAX_NAME_LENGTH:
            db.rollback()
            raise HTTPException(status_code=422, detail=f"名字最多 {diarization.MAX_NAME_LENGTH} 个字")
        if value:
            names[str(int(key))] = value
    if len(set(names.values())) != len(names):
        db.rollback()
        raise HTTPException(status_code=422, detail="不同说话人不能用同一个名字")
    report.speaker_names = names or None
    db.commit()
    return quality_out(report)


@router.post("/assign", response_model=AudioQualityReportOut)
def assign_speaker(session_id: str, body: AssignSpeakerIn, db: Session = Depends(get_db)):
    """把一段时间改成某位说话人；speaker 用一个新编号即为“新说话人”。"""
    report = _locked_report(db, session_id)
    session = db.get(RecordingSession, session_id)
    if report.speaker_analysis_status != "done" or report.speaker_segments is None:
        db.rollback()
        raise HTTPException(status_code=409, detail="还没有说话人分段，无法更正")
    if not 0 <= body.start_ms < body.end_ms or (session.total_duration_ms and body.end_ms > session.total_duration_ms):
        db.rollback()
        raise HTTPException(status_code=422, detail="时间范围无效")
    existing = diarization.speakers_in(report.speaker_segments)
    if body.speaker < 0 or body.speaker > (max(existing) + 1 if existing else 0):
        db.rollback()
        raise HTTPException(status_code=422, detail="说话人编号无效")
    report.speaker_segments = diarization.assign(report.speaker_segments, body.start_ms, body.end_ms, body.speaker)
    count = len(diarization.speakers_in(report.speaker_segments))
    diarization.refine_content_type(report, diarization.SpeakerCount(count, (), 0.0))
    db.commit()
    return quality_out(report)


@router.post("/analyze", response_model=AudioQualityReportOut, status_code=202)
async def reanalyze(session_id: str, db: Session = Depends(get_db)):
    """重新分析（之前失败、后来才装模型、或想推翻手动更正）。会覆盖手动更正，名字按编号保留。"""
    from app.services.stub_pipeline import schedule_speaker_analysis

    if not diarization.available():
        raise HTTPException(status_code=503, detail="本机未安装说话人区分模型，见 README“说话人区分”")
    report = _locked_report(db, session_id)
    if report.speaker_analysis_status in ("pending", "running"):
        db.rollback()
        raise HTTPException(status_code=409, detail="正在分析中")
    report.speaker_analysis_status = "pending"
    db.commit()
    schedule_speaker_analysis(session_id)
    return quality_out(report)
