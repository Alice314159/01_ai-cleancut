"""Export a completed transcript on its original, uncut audio timeline."""
import re

from app.models import TranscriptToken, TranscriptionJob


def content(tokens):
    # Whisper supplies leading spaces for Latin words; Chinese tokens need no separators.
    return ''.join(t.text for t in tokens).strip()


def timestamp(ms):
    seconds, millis = divmod(ms, 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f'{hours:02}:{minutes:02}:{seconds:02},{millis:03}'


def subtitle_cues(tokens):
    group = []
    end = 0
    for token in tokens:
        if group and token.start_ms >= end and (
            token.start_ms - end >= 800 or token.end_ms - group[0].start_ms > 5000
            or len(content(group)) + len(token.text) > 42
            or re.search(r'[。！？.!?]["”’\)]?$', content(group))
        ):
            yield group[0].start_ms, end, ' '.join(content(group).split())
            group = []
        group.append(token)
        end = max(t.end_ms for t in group)
    if group:
        yield group[0].start_ms, end, ' '.join(content(group).split())


def export(db, session, job_id, format):
    tokens = db.query(TranscriptToken).filter_by(session_id=session.id).order_by(TranscriptToken.seq).all()
    job = db.get(TranscriptionJob, job_id)
    if not tokens or not job or job.session_id != session.id or job.status != 'COMPLETED' or any(
        t.job_id != job_id or t.end_ms is None or not 0 <= t.start_ms < t.end_ms <= session.total_duration_ms
        for t in tokens
    ) or any(a.start_ms > b.start_ms for a, b in zip(tokens, tokens[1:])):
        raise ValueError('缺少有效的真实转写，或转写已更新，请刷新后重试')
    if format == 'txt':
        return content(tokens) + '\n'
    return ''.join(f'{i}\n{timestamp(start)} --> {timestamp(end)}\n{text}\n\n'
                   for i, (start, end, text) in enumerate(subtitle_cues(tokens), 1))


def snapshot(db, session_id):
    tokens = db.query(TranscriptToken).filter_by(session_id=session_id).order_by(TranscriptToken.seq).all()
    if not tokens:
        return None
    job = db.get(TranscriptionJob, tokens[0].job_id) if tokens[0].job_id else None
    if not job or job.session_id != session_id or job.status != 'COMPLETED' or any(
        t.job_id != job.id or t.end_ms is None or t.start_ms < 0 or t.end_ms <= t.start_ms for t in tokens
    ):
        return None
    return [dict(text=t.text, start_ms=t.start_ms, end_ms=t.end_ms, job_id=t.job_id) for t in tokens]


def export_render(output, version):
    from types import SimpleNamespace
    if any(d['action'] == 'REPLACE_AUDIO' and d['accepted'] for d in output.decisions):
        raise ValueError('成片包含重录片段，请等待成片转写完成或点击重试后导出字幕')
    if not output.transcript_snapshot:
        raise ValueError('此成片没有转写快照，请完成转写后重新生成成片')
    rate = version.sample_rate
    cuts = [(round(c['start_ms'] * rate / 1000), round(c['end_ms'] * rate / 1000)) for c in output.cut_ranges]
    def shifted(frame):
        return frame - sum(max(0, min(frame, end) - start) for start, end in cuts)
    words = []
    for token in output.transcript_snapshot:
        start, end = round(token['start_ms'] * rate / 1000), round(token['end_ms'] * rate / 1000)
        if any(a <= start and end <= b for a, b in cuts):
            continue
        if any(start < b and a < end for a, b in cuts):
            raise ValueError('删除边界穿过转写词，无法准确导出该词字幕；请按完整词调整区间后重新生成成片')
        start_ms = round(shifted(start) * 1000 / rate)
        end_ms = min(version.duration_ms, round(shifted(end) * 1000 / rate))
        if end_ms > start_ms:
            words.append(SimpleNamespace(text=token['text'], start_ms=start_ms, end_ms=end_ms))
    if not words:
        raise ValueError('成片中没有可导出的转写文字')
    return ''.join(f'{i}\n{timestamp(start)} --> {timestamp(end)}\n{text}\n\n'
                   for i, (start, end, text) in enumerate(subtitle_cues(words), 1))
