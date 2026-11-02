"""Literal transcript rules, never automatic deletion or semantic judgments."""
import unicodedata

from sqlalchemy import text

from app.models import EditDecision, RecordingSession, TranscriptToken, TranscriptionJob
from app.services.transcription import ACTIVE_STATUSES, source_for_session


def candidates(tokens, duration_ms):
    normalized = [''.join(c for c in unicodedata.normalize('NFKC', t.text).casefold() if c.isalnum()) for t in tokens]

    def valid(start, end):
        group = tokens[start:end]
        return bool(group) and all(t.end_ms is not None and 0 <= t.start_ms < t.end_ms <= duration_ms for t in group) and all(
            0 <= b.start_ms - a.end_ms <= 1000 for a, b in zip(group, group[1:])) and group[-1].end_ms - group[0].start_ms <= 8000

    i = 0
    while i < len(tokens):
        best = None
        for size in range(1, min(12, (len(tokens) - i) // 2) + 1):
            unit = normalized[i:i + size]
            if not all(unit):
                continue
            copies = 1
            while normalized[i + copies * size:i + (copies + 1) * size] == unit and valid(i, i + (copies + 1) * size):
                copies += 1
            removed = size * (copies - 1)
            if copies > 1 and (best is None or removed > best[0]):
                best = removed, size * copies
        if best:
            removed, consumed = best
            reference = tokens[i + removed]
            yield dict(start_ms=tokens[i].start_ms, end_ms=tokens[i + removed - 1].end_ms,
                       action='DELETE_REPETITION', text=''.join(t.text for t in tokens[i:i + removed]),
                       reason=f'相邻文字重复，建议保留最后一次（{reference.start_ms / 1000:.3f} 秒起）。也可能是刻意强调，请试听确认。')
            i += consumed
            continue
        # faster-whisper transcribes the same hesitation sound with different homophone
        # characters (恩/唔 for "嗯", 额 for "呃"); match those too, not just one spelling.
        if normalized[i] in {'嗯', '恩', '唔', '呃', '额', 'um', 'uh', 'erm'} and valid(i, i + 1):
            yield dict(start_ms=tokens[i].start_ms, end_ms=tokens[i].end_ms, action='DELETE_FILLER',
                       text=tokens[i].text, reason='独立语气词规则命中；可能表达回应或犹豫，请结合上下文试听确认。')
        i += 1


def generate(db, session_id, job_id):
    # Serialize generation with transcript publication and other writers to avoid duplicate candidates.
    db.rollback()
    db.execute(text('BEGIN IMMEDIATE'))
    session = db.get(RecordingSession, session_id)
    if session is None or session.live_analysis_status != 'FINALIZED':
        raise ValueError('录音尚未就绪')
    if db.query(TranscriptionJob).filter_by(session_id=session_id).filter(TranscriptionJob.status.in_(ACTIVE_STATUSES)).first():
        raise ValueError('请等待当前转写完成')
    job = db.get(TranscriptionJob, job_id)
    source = source_for_session(db, session_id)
    tokens = db.query(TranscriptToken).filter_by(session_id=session_id).order_by(TranscriptToken.seq).all()
    if not job or job.session_id != session_id or job.status != 'COMPLETED' or not source or source.id != job.source_version_id or not tokens or any(t.job_id != job_id or t.end_ms is None for t in tokens):
        raise ValueError('转写已变化或缺少真实词级时间戳，请刷新后重试')
    added = add_candidates(db, session, tokens)
    db.commit()
    return added


def add_candidates(db, session, tokens, *, repetitions_only=False):
    """Stage suggestions in the caller's transaction, preserving reviewed choices."""
    session_id = session.id
    existing = db.query(EditDecision).filter_by(session_id=session_id).all()
    added = 0
    for candidate in candidates(tokens, session.total_duration_ms):
        if repetitions_only and candidate['action'] != 'DELETE_REPETITION':
            continue
        # Preserve every prior choice, including rejected suggestions and protected ranges.
        if any(d.start_ms < candidate['end_ms'] and candidate['start_ms'] < d.end_ms for d in existing):
            continue
        decision = EditDecision(session_id=session_id, source='transcript_rule', confidence=1.0,
                                accepted=None, review_required=True, hard_protected=False, **candidate)
        db.add(decision)
        existing.append(decision)
        added += 1
    return added
