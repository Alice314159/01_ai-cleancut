"""Store a retake and its replacement decision atomically, keeping the source intact."""
from pathlib import Path
import tempfile
import uuid

from sqlalchemy import text

from app.models import AudioVersion, EditDecision
from app.services import rendering, storage

MAX_UPLOAD_BYTES = 60 * 1024 * 1024
MAX_DURATION_MS = 5 * 60 * 1000


def validate(db, session_id, start_ms, end_ms, source_id, revisions):
    session = rendering.validate_session(db, session_id)
    if not 0 <= start_ms < end_ms <= session.total_duration_ms:
        raise rendering.RenderInputError('请选择录音时长内的有效重录区间')
    if rendering.render_source(db, session_id).id != source_id:
        raise rendering.RenderConflict('试听音频源已变化，请刷新并重新核对重录范围')
    decisions = db.query(EditDecision).filter_by(session_id=session_id).all()
    if {d.id: d.revision for d in decisions} != revisions:
        raise rendering.RenderConflict('剪辑选择已变化，重录尚未应用；请刷新并核对后重试')
    superseded = []
    for d in decisions:
        if d.start_ms >= end_ms or d.end_ms <= start_ms:
            continue
        if d.hard_protected or d.action == 'KEEP_PROTECTED':
            raise rendering.RenderConflict('重录范围包含保护片段，无法替换')
        if d.accepted is False:
            continue
        if d.start_ms < start_ms or d.end_ms > end_ms:
            raise rendering.RenderConflict('重录范围与已有剪辑部分重叠，请先拒绝该剪辑或调整范围')
        superseded.append(d)
    return superseded


def create(db, session_id, upload, start_ms, end_ms, source_id, revisions):
    validate(db, session_id, start_ms, end_ms, source_id, revisions)
    db.rollback()
    directory = Path(storage.session_dir(session_id))
    version_id = f'av_{uuid.uuid4().hex}'
    output_path = directory / f'retake_{version_id}.wav'
    published = False
    try:
        with tempfile.TemporaryDirectory(prefix='.retake-', dir=directory) as temp:
            uploaded = Path(temp) / 'upload'
            size = 0
            with uploaded.open('wb') as output:
                while chunk := upload.read(1024 * 1024):
                    size += len(chunk)
                    if size > MAX_UPLOAD_BYTES:
                        raise rendering.RenderInputError('单次重录音频不能超过 60 MB')
                    output.write(chunk)
            if size == 0:
                raise rendering.RenderInputError('重录音频为空，请重新录制')
            # Decode by content, never trusting the browser MIME type or uploaded filename.
            rendered = rendering.write_render(uploaded, output_path, [])
        duration = round(rendered.sample_count * 1000 / rendered.sample_rate)
        if not 0 < duration <= MAX_DURATION_MS:
            raise rendering.RenderInputError('单次重录需大于 0 秒且不超过 5 分钟')
        db.execute(text('BEGIN IMMEDIATE'))
        superseded = validate(db, session_id, start_ms, end_ms, source_id, revisions)
        for previous in superseded:
            previous.accepted = False
            previous.revision += 1
        version = AudioVersion(id=version_id, session_id=session_id, version_type='retake', label='重录片段',
                               duration_ms=duration, sample_count=rendered.sample_count, sample_rate=rendered.sample_rate,
                               filename=output_path.name, engine='browser_retake', engine_version='1.0')
        db.add(version)
        db.flush()
        decision = EditDecision(session_id=session_id, start_ms=start_ms, end_ms=end_ms, action='REPLACE_AUDIO',
                                text='重录替换', reason='用已试听确认的新录音替换此范围；拒绝此决策可恢复原片段。',
                                source='user', confidence=1.0, accepted=True, review_required=False,
                                replacement_version_id=version_id, replacement_duration_ms=duration)
        db.add(decision)
        db.commit()
        published = True
        db.refresh(decision)
        return decision
    except Exception:
        db.rollback()
        raise
    finally:
        if not published:
            output_path.unlink(missing_ok=True)
