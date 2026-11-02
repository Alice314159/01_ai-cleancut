"""Local, post-recording Whisper inference; publish complete transcripts atomically."""
from functools import lru_cache
import importlib.util
import logging
import math
from pathlib import Path
import threading

from sqlalchemy import text

from app import config, db as db_module
from app.models import EditDecision, AudioVersion, RecordingSession, RoleAssignment, TranscriptToken, TranscriptionJob

logger = logging.getLogger(__name__)

ACTIVE_STATUSES = ("QUEUED", "LOADING_MODEL", "TRANSCRIBING")
# Avoid multiple CPU model instances competing for memory. DB sessions stay in this worker thread.
_model_lock = threading.Lock()

# faster-whisper's per-word timestamps (segment.words[i].word) never carry punctuation —
# it only survives in the segment's own decoded text. Word-level text is what we store per
# token, so without this, the whole transcript comes back punctuation-free.
_SENTENCE_PUNCTUATION = set('，。！？；：、,.!?;:…—-""\'\'（）()【】[]《》<>')


def _restore_word_punctuation(segment_text: str, words) -> list[str]:
    """Re-attach punctuation from segment_text onto the words that precede it.

    Walks both in lockstep, matching each word's stripped text against segment_text in
    order; whatever falls in the gap before the next word (or end of segment) is kept only
    if it's actual punctuation, and appended to the word before it. If alignment ever fails
    (text and words disagree in some unexpected way), bail out and return the words
    untouched rather than risk corrupting them.
    """
    spans = []
    pos = 0
    for word in words:
        stripped = word.word.strip()
        if not stripped:
            spans.append(None)
            continue
        idx = segment_text.find(stripped, pos)
        if idx == -1:
            return [w.word for w in words]
        spans.append((idx, idx + len(stripped)))
        pos = idx + len(stripped)
    texts = []
    for i, word in enumerate(words):
        span = spans[i]
        if span is None:
            texts.append(word.word)
            continue
        next_start = next((s[0] for s in spans[i + 1:] if s is not None), len(segment_text))
        gap = segment_text[span[1]:next_start]
        punctuation = ''.join(ch for ch in gap if ch in _SENTENCE_PUNCTUATION)
        texts.append(word.word + punctuation)
    return texts


@lru_cache(maxsize=1)
def _zh_converter():
    """zhconv 是可选依赖：缺失时返回 None，转写照常完成，只是不做繁转简。"""
    try:
        import zhconv
    except ImportError:
        logger.warning("未安装 zhconv，中文转写结果不会转换为简体；请执行 pip install zhconv")
        return None
    return zhconv.convert


def to_simplified(content: str) -> str:
    convert = _zh_converter()
    return convert(content, "zh-hans") if convert else content


def dependency_available() -> bool:
    return importlib.util.find_spec("faster_whisper") is not None


@lru_cache(maxsize=1)
def get_model(model_name: str, model_dir: str, offline: bool):
    from faster_whisper import WhisperModel

    return WhisperModel(model_name, device="cpu", compute_type="int8", cpu_threads=4,
                        download_root=model_dir, local_files_only=offline)


def source_for_session(db, session_id: str):
    role = db.query(RoleAssignment).filter_by(session_id=session_id, role="ASR_SOURCE").first()
    source = db.get(AudioVersion, role.audio_version_id) if role else None
    if source is None or source.session_id != session_id or source.version_type == "master" or not source.filename:
        return None
    return source


def recover_interrupted_jobs() -> None:
    with db_module.SessionLocal() as db:
        db.query(TranscriptionJob).filter(TranscriptionJob.status.in_(ACTIVE_STATUSES)).update({
            TranscriptionJob.status: "FAILED",
            TranscriptionJob.error_message: "服务重启中断了转写，请重试。此前成功的文字已保留。",
        }, synchronize_session=False)
        db.commit()


def timed_words(segment, duration_ms: int):
    words = getattr(segment, "words", None)
    if words:
        texts = _restore_word_punctuation(getattr(segment, "text", "") or "", words)
    else:
        words = [segment]
        texts = [getattr(segment, "text", "")]
    for word, content in zip(words, texts):
        start, end = float(word.start), float(word.end)
        if not content.strip() or not math.isfinite(start) or not math.isfinite(end):
            continue
        start_ms, end_ms = max(0, round(start * 1000)), min(duration_ms, round(end * 1000))
        if end_ms > start_ms:
            yield dict(text=content, start_ms=start_ms, end_ms=end_ms)


def run_transcription(job_id: str) -> None:
    with _model_lock, db_module.SessionLocal() as db:
        job = db.get(TranscriptionJob, job_id)
        if job is None or job.status != "QUEUED":
            return
        try:
            source = source_for_session(db, job.session_id)
            session = db.get(RecordingSession, job.session_id)
            if source is None or source.id != job.source_version_id or session.live_analysis_status != "FINALIZED":
                raise ValueError("转写音频源已变化或尚未就绪，请重试")
            path = Path(config.AUDIO_DIR) / job.session_id / source.filename
            if not path.is_file():
                raise ValueError("转写音频文件缺失")
            duration_ms = session.total_duration_ms
            job.status = "LOADING_MODEL"
            db.commit()
            model = get_model(job.model, str(config.ASR_MODEL_DIR), config.ASR_LOCAL_FILES_ONLY)
            job.status = "TRANSCRIBING"
            db.commit()
            segments, info = model.transcribe(
                str(path), language=None if job.requested_language == "auto" else job.requested_language,
                beam_size=5, word_timestamps=True, vad_filter=True, condition_on_previous_text=False,
            )
            # faster-whisper's multilingual models default to Traditional-leaning characters for
            # Chinese regardless of the requested language (verified: even language="zh" explicit
            # still comes back Traditional) — convert to Simplified so it matches what a
            # Simplified-Chinese speaker actually typed/said, not a script mismatch bug.
            simplify_chinese = info.language == "zh"
            tokens = []
            for segment in segments:
                for word in timed_words(segment, duration_ms):
                    if simplify_chinese:
                        word["text"] = to_simplified(word["text"])
                    tokens.append(word)
                job.progress = max(job.progress, min(0.99, max(0.0, segment.end * 1000 / max(1, duration_ms))))
                db.commit()
            # Lock only for final publication; a failed attempt never removes the last successful text.
            db.rollback()
            db.execute(text("BEGIN IMMEDIATE"))
            job = db.get(TranscriptionJob, job_id)
            if job.status != "TRANSCRIBING":
                raise ValueError("转写任务已被中断，请重试")
            source = source_for_session(db, job.session_id)
            if source is None or source.id != job.source_version_id:
                raise ValueError("转写期间音频源发生变化，请重试")
            # Only unreviewed rule candidates expire; explicit user choices remain intact.
            db.query(EditDecision).filter_by(session_id=job.session_id, source="transcript_rule", accepted=None).delete(synchronize_session=False)
            db.query(TranscriptToken).filter_by(session_id=job.session_id).delete(synchronize_session=False)
            for seq, token in enumerate(sorted(tokens, key=lambda token: (token["start_ms"], token["end_ms"]))):
                db.add(TranscriptToken(session_id=job.session_id, job_id=job.id, seq=seq,
                                       state="committed", cut=False, **token))
            # Publish repetition hints together with the text so polling cannot miss them.
            from app.services.suggestions import add_candidates
            db.flush()
            published = db.query(TranscriptToken).filter_by(session_id=job.session_id).order_by(TranscriptToken.seq).all()
            add_candidates(db, session, published, repetitions_only=True)
            job.status = "COMPLETED"
            job.progress = 1.0
            job.detected_language = info.language
            db.commit()
        except Exception as exc:
            db.rollback()
            job = db.get(TranscriptionJob, job_id)
            if job is not None:
                job.status = "FAILED"
                job.error_message = f"本机转写失败：{str(exc)[:600]}"
                db.commit()
