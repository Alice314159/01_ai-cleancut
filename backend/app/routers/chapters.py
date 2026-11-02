from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import AudioVersion, Chapter, RecordingSession, RenderOutput
from app.schemas.chapter import ChapterOut, CreateChapterIn, MappedChapterOut, UpdateChapterIn
from app.services import chapters as chapter_service

router = APIRouter(prefix="/api/sessions/{session_id}", tags=["chapters"])

MAX_CHAPTERS = 200


def _session(db: Session, session_id: str) -> RecordingSession:
    session = db.get(RecordingSession, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    return session


def _check_start(db: Session, session: RecordingSession, start_ms: int, exclude_id: str | None = None) -> None:
    if session.total_duration_ms and start_ms >= session.total_duration_ms:
        raise HTTPException(status_code=422, detail="章节起点超出录音时长")
    clash = db.query(Chapter.id).filter_by(session_id=session.id, start_ms=start_ms)
    if exclude_id:
        clash = clash.filter(Chapter.id != exclude_id)
    if clash.first():
        raise HTTPException(status_code=409, detail="该时间点已有章节")


@router.get("/chapters", response_model=list[ChapterOut])
def list_chapters(session_id: str, db: Session = Depends(get_db)):
    _session(db, session_id)
    return chapter_service.list_for(db, session_id)


@router.post("/chapters", response_model=ChapterOut, status_code=201)
def create_chapter(session_id: str, body: CreateChapterIn, db: Session = Depends(get_db)):
    db.rollback()
    db.execute(text("BEGIN IMMEDIATE"))
    session = _session(db, session_id)
    if db.query(Chapter).filter_by(session_id=session_id).count() >= MAX_CHAPTERS:
        raise HTTPException(status_code=409, detail=f"每个会话最多 {MAX_CHAPTERS} 个章节")
    _check_start(db, session, body.start_ms)
    chapter = Chapter(session_id=session_id, start_ms=body.start_ms, title=body.title)
    db.add(chapter)
    db.commit()
    db.refresh(chapter)
    return chapter


@router.patch("/chapters/{chapter_id}", response_model=ChapterOut)
def update_chapter(session_id: str, chapter_id: str, body: UpdateChapterIn, db: Session = Depends(get_db)):
    db.rollback()
    db.execute(text("BEGIN IMMEDIATE"))
    session = _session(db, session_id)
    chapter = db.get(Chapter, chapter_id)
    if chapter is None or chapter.session_id != session_id:
        raise HTTPException(status_code=404, detail="chapter not found")
    if body.start_ms is not None:
        _check_start(db, session, body.start_ms, exclude_id=chapter_id)
        chapter.start_ms = body.start_ms
    if body.title is not None:
        chapter.title = body.title
    db.commit()
    db.refresh(chapter)
    return chapter


@router.delete("/chapters/{chapter_id}", status_code=204)
def delete_chapter(session_id: str, chapter_id: str, db: Session = Depends(get_db)):
    _session(db, session_id)
    chapter = db.get(Chapter, chapter_id)
    if chapter is None or chapter.session_id != session_id:
        raise HTTPException(status_code=404, detail="chapter not found")
    db.delete(chapter)
    db.commit()
    return Response(status_code=204)


def chapters_for_version(db: Session, session_id: str, version: AudioVersion) -> list[chapter_service.MappedChapter]:
    """某个音频版本时间轴上的章节：成片按其剪辑快照换算，原始/降噪版本原样使用，其余版本无章节。"""
    items = chapter_service.list_for(db, session_id)
    if not items:
        return []
    if version.version_type == "master":
        output = db.query(RenderOutput).filter_by(session_id=session_id, version_id=version.id).first()
        return chapter_service.map_to_render(items, output, version.duration_ms) if output else []
    if version.version_type in ("original", "denoised"):
        return chapter_service.unmapped(items, version.duration_ms)
    return []


@router.get("/versions/{version_id}/chapters")
def version_chapters(session_id: str, version_id: str, format: Literal["json", "txt"] = "json", db: Session = Depends(get_db)):
    _session(db, session_id)
    version = db.get(AudioVersion, version_id)
    if version is None or version.session_id != session_id:
        raise HTTPException(status_code=404, detail="version not found")
    mapped = chapters_for_version(db, session_id, version)
    if format == "txt":
        return Response(chapter_service.as_text(mapped), media_type="text/plain; charset=utf-8")
    return [MappedChapterOut.model_validate(c, from_attributes=True).model_dump(by_alias=True) for c in mapped]
