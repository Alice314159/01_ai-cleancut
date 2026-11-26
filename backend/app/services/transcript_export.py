"""Export a completed transcript on its original, uncut audio timeline."""
import re
from types import SimpleNamespace

from app.models import TranscriptToken, TranscriptionJob
from app.services import diarization


def content(tokens):
    # Whisper supplies leading spaces for Latin words; Chinese tokens need no separators.
    return ''.join(t.text for t in tokens).strip()


def timestamp(ms):
    seconds, millis = divmod(ms, 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f'{hours:02}:{minutes:02}:{seconds:02},{millis:03}'


def subtitle_cues(tokens, names=None):
    """切字幕条；换人处必定另起一条，换人后的第一条前面加“说话人 X：”。"""
    previous = None
    for start, end, text, speaker in _cues(tokens):
        if speaker is not None and speaker != previous:
            text = f"{diarization.speaker_name(speaker, names)}：{text}"
        previous = speaker if speaker is not None else previous
        yield start, end, text


def _cues(tokens):
    group = []
    end = 0
    for token in tokens:
        speaker = getattr(token, "speaker", None)
        changed = bool(group) and speaker is not None and speaker != getattr(group[-1], "speaker", None)
        if group and (changed or token.start_ms >= end and (
            token.start_ms - end >= 800 or token.end_ms - group[0].start_ms > 5000
            or len(content(group)) + len(token.text) > 42
            or re.search(r'[。！？.!?]["”’\)]?$', content(group))
        )):
            yield group[0].start_ms, end, ' '.join(content(group).split()), getattr(group[0], "speaker", None)
            group = []
        group.append(token)
        end = max(t.end_ms for t in group)
    if group:
        yield group[0].start_ms, end, ' '.join(content(group).split()), getattr(group[0], "speaker", None)


def turns(tokens, names=None):
    """TXT 导出：有说话人时按轮次分段，每段前标说话人。"""
    paragraphs, current, speaker = [], [], None
    for token in tokens:
        if current and token.speaker is not None and token.speaker != speaker:
            paragraphs.append((speaker, current))
            current = []
        if token.speaker is not None:
            speaker = token.speaker
        current.append(token)
    if current:
        paragraphs.append((speaker, current))
    return '\n\n'.join(f"{diarization.speaker_name(s, names)}：{content(group)}" if s is not None else content(group)
                       for s, group in paragraphs) + '\n'


def with_speakers(db, session_id, tokens):
    speakers = diarization.label_tokens(diarization.session_segments(db, session_id), tokens)
    return [SimpleNamespace(text=t.text, start_ms=t.start_ms, end_ms=t.end_ms, speaker=s) for t, s in zip(tokens, speakers)]


def export(db, session, job_id, format):
    tokens = db.query(TranscriptToken).filter_by(session_id=session.id).order_by(TranscriptToken.seq).all()
    job = db.get(TranscriptionJob, job_id)
    if not tokens or not job or job.session_id != session.id or job.status != 'COMPLETED' or any(
        t.job_id != job_id or t.end_ms is None or not 0 <= t.start_ms < t.end_ms <= session.total_duration_ms
        for t in tokens
    ) or any(a.start_ms > b.start_ms for a, b in zip(tokens, tokens[1:])):
        raise ValueError('缺少有效的真实转写，或转写已更新，请刷新后重试')
    tokens = with_speakers(db, session.id, tokens)
    names = diarization.session_names(db, session.id)
    if format == 'txt':
        if all(t.speaker is None for t in tokens):
            return content(tokens) + '\n'
        return turns(tokens, names)
    return ''.join(f'{i}\n{timestamp(start)} --> {timestamp(end)}\n{text}\n\n'
                   for i, (start, end, text) in enumerate(subtitle_cues(tokens, names), 1))


def snapshot(db, session_id):
    tokens = db.query(TranscriptToken).filter_by(session_id=session_id).order_by(TranscriptToken.seq).all()
    if not tokens:
        return None
    job = db.get(TranscriptionJob, tokens[0].job_id) if tokens[0].job_id else None
    if not job or job.session_id != session_id or job.status != 'COMPLETED' or any(
        t.job_id != job.id or t.end_ms is None or t.start_ms < 0 or t.end_ms <= t.start_ms for t in tokens
    ):
        return None
    speakers = diarization.label_tokens(diarization.session_segments(db, session_id), tokens)
    return [dict(text=t.text, start_ms=t.start_ms, end_ms=t.end_ms, job_id=t.job_id, speaker=s) for t, s in zip(tokens, speakers)]


def export_render(output, version, names=None):
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
            words.append(SimpleNamespace(text=token['text'], start_ms=start_ms, end_ms=end_ms, speaker=token.get('speaker')))
    if not words:
        raise ValueError('成片中没有可导出的转写文字')
    return ''.join(f'{i}\n{timestamp(start)} --> {timestamp(end)}\n{text}\n\n'
                   for i, (start, end, text) in enumerate(subtitle_cues(words, names), 1))
