"""Recognize final audio including retakes, without modifying source words or edit decisions."""
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import text

from app import config, db as db_module
from app.models import AudioVersion, RenderOutput, RenderTranscriptionJob
from app.services import transcription, transcript_export


def latest(db, session_id, version_id, *, completed=False):
    query = db.query(RenderTranscriptionJob).filter_by(session_id=session_id, source_version_id=version_id)
    if completed:
        query = query.filter_by(status='COMPLETED')
    return query.order_by(RenderTranscriptionJob.created_at_ns.desc(), RenderTranscriptionJob.id.desc()).first()


def enqueue(db, session_id, version_id, language='auto'):
    db.rollback()
    db.execute(text('BEGIN IMMEDIATE'))
    output = db.query(RenderOutput).filter_by(session_id=session_id, version_id=version_id).first()
    if output is None:
        raise ValueError('成片不存在')
    running = db.query(RenderTranscriptionJob).filter_by(session_id=session_id, source_version_id=version_id).filter(
        RenderTranscriptionJob.status.in_(transcription.ACTIVE_STATUSES)).first()
    if running:
        db.commit()
        return running, False
    job = RenderTranscriptionJob(session_id=session_id, source_version_id=version_id, model=config.ASR_MODEL,
                                 requested_language=language)
    if not transcription.dependency_available():
        job.status = 'FAILED'
        job.error_message = '未安装本机转写依赖，请在后端安装 requirements-asr.txt 后重试成片转写。'
    db.add(job)
    db.commit()
    db.refresh(job)
    return job, job.status == 'QUEUED'


def recover_interrupted_jobs():
    with db_module.SessionLocal() as db:
        db.query(RenderTranscriptionJob).filter(RenderTranscriptionJob.status.in_(transcription.ACTIVE_STATUSES)).update({
            RenderTranscriptionJob.status: 'FAILED',
            RenderTranscriptionJob.error_message: '服务重启中断了成片转写，请重试。此前成功的成片文字已保留。',
        }, synchronize_session=False)
        db.commit()


def run(job_id):
    # Share the CPU/model lock with original-timeline recognition.
    with transcription._model_lock, db_module.SessionLocal() as db:
        job = db.get(RenderTranscriptionJob, job_id)
        if not job or job.status != 'QUEUED':
            return
        try:
            version = db.get(AudioVersion, job.source_version_id)
            output = db.query(RenderOutput).filter_by(session_id=job.session_id, version_id=job.source_version_id).first()
            if not version or not output or version.session_id != job.session_id or version.version_type != 'master' or not version.filename:
                raise ValueError('成片音频不存在')
            path = Path(config.AUDIO_DIR) / job.session_id / version.filename
            if not path.is_file():
                raise ValueError('成片音频文件缺失')
            duration = version.duration_ms
            job.status = 'LOADING_MODEL'
            db.commit()
            model = transcription.get_model(job.model, str(config.ASR_MODEL_DIR), config.ASR_LOCAL_FILES_ONLY)
            job.status = 'TRANSCRIBING'
            db.commit()
            segments, info = model.transcribe(str(path), language=None if job.requested_language == 'auto' else job.requested_language,
                                             beam_size=5, word_timestamps=True, vad_filter=True, condition_on_previous_text=False)
            tokens = []
            for segment in segments:
                for word in transcription.timed_words(segment, duration):
                    if info.language == 'zh':
                        word['text'] = transcription.to_simplified(word['text'])
                    tokens.append(word)
                job.progress = max(job.progress, min(.99, max(0, segment.end * 1000 / max(1, duration))))
                db.commit()
            db.rollback()
            db.execute(text('BEGIN IMMEDIATE'))
            job = db.get(RenderTranscriptionJob, job_id)
            if not job or job.status != 'TRANSCRIBING':
                raise ValueError('成片转写已被中断，请重试')
            if not db.query(RenderOutput).filter_by(session_id=job.session_id, version_id=job.source_version_id).first():
                raise ValueError('成片已移除')
            job.tokens = sorted(tokens, key=lambda word: (word['start_ms'], word['end_ms']))
            job.detected_language = info.language
            job.status = 'COMPLETED'
            job.progress = 1.0
            db.commit()
        except Exception as exc:
            db.rollback()
            job = db.get(RenderTranscriptionJob, job_id)
            if job:
                job.status = 'FAILED'
                job.error_message = f'成片转写失败：{str(exc)[:600]}'
                db.commit()


def export(job, version, format='srt'):
    if not job or job.status != 'COMPLETED' or not job.tokens:
        raise ValueError('尚无可导出的成片文字，请等待转写完成或重试')
    if any(not 0 <= t['start_ms'] < t['end_ms'] <= version.duration_ms for t in job.tokens):
        raise ValueError('成片转写时间戳无效，请重试')
    words = [SimpleNamespace(**t) for t in job.tokens]
    if format == 'txt':
        return transcript_export.content(words) + '\n'
    return ''.join(f'{i}\n{transcript_export.timestamp(start)} --> {transcript_export.timestamp(end)}\n{content}\n\n'
                   for i, (start, end, content) in enumerate(transcript_export.subtitle_cues(words), 1))
