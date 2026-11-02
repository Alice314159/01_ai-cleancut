"""把 pauses.find_long_pauses 的结果落成待确认的 COMPRESS_SILENCE 决策。"""
from pathlib import Path

from sqlalchemy import text

from app.models import EditDecision, RecordingSession
from app.services import audio_analysis, pauses, rendering, storage

SOURCE = "silence_rule"
# "拒绝待定建议"批量操作覆盖的自动建议来源；用户手动添加的决策不受影响。
SUGGESTION_SOURCES = ("transcript_rule", SOURCE)


def generate(db, session_id: str, *, min_pause_ms: int = 1500, keep_ms: int = 500) -> int:
    session = db.get(RecordingSession, session_id)
    if session is None or session.live_analysis_status != "FINALIZED":
        raise ValueError("录音尚未就绪")
    try:
        source = rendering.render_source(db, session_id)
    except rendering.RenderConflict as exc:
        raise ValueError(str(exc)) from exc
    source_id = source.id
    path = Path(storage.session_dir(session_id)) / source.filename
    duration_ms = session.total_duration_ms
    # 解码可能较慢，不占着写锁；发布前再校验音频源没有变化。
    db.rollback()
    try:
        samples = audio_analysis.decode_pcm(path)
    except audio_analysis.DecodeError as exc:
        raise ValueError("剪辑源音频无法解码") from exc
    found = pauses.find_long_pauses(samples, audio_analysis.ANALYSIS_SAMPLE_RATE,
                                    min_pause_ms=min_pause_ms, keep_ms=keep_ms)

    db.execute(text("BEGIN IMMEDIATE"))
    try:
        current = rendering.render_source(db, session_id)
    except rendering.RenderConflict as exc:
        raise ValueError(str(exc)) from exc
    if current.id != source_id:
        raise ValueError("分析期间音频源已变化，请重试")
    existing = db.query(EditDecision).filter_by(session_id=session_id).all()
    added = 0
    for pause in found:
        start, end = pause.cut_start_ms, min(pause.cut_end_ms, duration_ms)
        if end - start < pauses.FRAME_MS:
            continue
        # 已有任何决策（含已拒绝、保护区间）的区间都不再重复建议。
        if any(d.start_ms < end and start < d.end_ms for d in existing):
            continue
        decision = EditDecision(
            session_id=session_id, start_ms=start, end_ms=end, action="COMPRESS_SILENCE",
            text="", confidence=1.0, source=SOURCE, accepted=None, review_required=True, hard_protected=False,
            reason=(f"停顿 {pause.duration_ms / 1000:.1f} 秒，建议缩短为约 {(pause.duration_ms - (end - start)) / 1000:.1f} 秒。"
                    "依据音频能量判断，可能是有意的停顿，请试听确认。"),
        )
        db.add(decision)
        existing.append(decision)
        added += 1
    db.commit()
    return added
