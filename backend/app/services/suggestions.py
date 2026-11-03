"""Literal transcript rules, never automatic deletion or semantic judgments."""
import unicodedata

from sqlalchemy import text

from app.models import EditDecision, RecordingSession, TranscriptToken, TranscriptionJob
from app.services.transcription import ACTIVE_STATUSES, source_for_session


# 常见叠词（不是口吃）：词内首字重复时排除这些开头
_REDUPLICATED = set(
    '慢慢 常常 天天 刚刚 渐渐 悄悄 好好 轻轻 默默 偷偷 紧紧 静静 久久 处处 人人 个个 年年 时时 事事 统统 往往 '
    '明明 白白 早早 多多 仅仅 稍稍 微微 隐隐 纷纷 匆匆 哈哈 呵呵 嘻嘻 谢谢 爸爸 妈妈 哥哥 姐姐 弟弟 妹妹 宝宝 '
    '奶奶 爷爷 星星 看看 想想 试试 说说 走走 听听 等等 聊聊 楼楼 道道 家家 点点 滴滴 平平 安安 高高 大大 小小 '
    '快快 乖乖 满满 淡淡 远远 深深 浅浅 冷冷 热热 干干 净净 清清 楚楚 实实 在在 断断 续续'.split()
)
_STUTTER_MAX_GAP_MS = 600


def _is_cjk(text):
    return bool(text) and all('\u4e00' <= ch <= '\u9fff' for ch in text)


def _stutter(tokens, normalized, i):
    """
    口吃（只看文字，保守）：
    1) 词首重复：当前词是下一个词的真前缀且很短（中文 ≤2 字；英文 ≤2 个字母或以“-”结尾的半截词），
       两词间隔不超过 0.6 秒——如“我 我们”“就 就是”“th- the”。删除前面的半截。
    2) 词内首字重复：一个词里开头的字连说了几遍，如“这这个”“我我们”；排除常见叠词（谢谢、慢慢…）。
       词内没有逐字时间戳，删除边界按字数估算。
    """
    token, cur = tokens[i], normalized[i]
    if not cur or token.end_ms is None:
        return None
    if i + 1 < len(tokens):
        nxt, following = normalized[i + 1], tokens[i + 1]
        raw = token.text.strip()
        half_word = raw.endswith(('-', '—', '–'))
        short = len(cur) <= 2
        if (nxt and len(cur) < len(nxt) and nxt.startswith(cur) and following.end_ms is not None
                and (short if _is_cjk(cur) else (half_word or short))
                and 0 <= following.start_ms - token.end_ms <= _STUTTER_MAX_GAP_MS
                and 0 <= token.start_ms < token.end_ms <= following.start_ms):
            return dict(start_ms=token.start_ms, end_ms=token.end_ms, action='DELETE_STUTTER', text=token.text,
                        reason=f'词首重复：“{raw}”之后紧接着说了完整的“{following.text.strip()}”，疑似口吃。请试听确认。')
    if _is_cjk(cur) and len(cur) >= 3 and cur[0] == cur[1]:
        repeats = 1
        while repeats < len(cur) and cur[repeats] == cur[0]:
            repeats += 1
        rest = cur[repeats - 1:]
        if len(rest) >= 2 and cur[:2] not in _REDUPLICATED and rest[:2] not in _REDUPLICATED:
            span = token.end_ms - token.start_ms
            cut_end = token.start_ms + round(span * (repeats - 1) / len(cur))
            if cut_end - token.start_ms >= 40:
                return dict(start_ms=token.start_ms, end_ms=cut_end, action='DELETE_STUTTER', text=cur[:repeats - 1],
                            reason=f'词内首字重复：“{cur}”开头的“{cur[0]}”多说了 {repeats - 1} 次，疑似口吃。'
                                   '词内没有逐字时间，删除边界按字数估算，请试听确认。')
    return None


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
        stutter = _stutter(tokens, normalized, i) if 0 <= tokens[i].start_ms and (tokens[i].end_ms or 0) <= duration_ms else None
        if stutter:
            yield stutter
            i += 1
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
        # 转写完成时自动发布的只有“重复类”提示（相邻重复与口吃）；语气词需要手动查找
        if repetitions_only and candidate['action'] not in ('DELETE_REPETITION', 'DELETE_STUTTER'):
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
