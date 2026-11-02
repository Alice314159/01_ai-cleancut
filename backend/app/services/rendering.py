"""Render accepted cuts from an uncut source, preserving its rate and channels."""
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import uuid
import wave

import numpy as np

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models import AudioVersion, EditDecision, EnhancementJob, RecordingSession, RenderOutput, RoleAssignment
import dataclasses
import os

from app.services import loudness, storage, transcript_export

# COMPRESS_SILENCE 的区间本身就是"停顿中间要删掉的部分"（两侧余量已在生成时留好）。
DELETE_ACTIONS = {"DELETE_REPETITION", "DELETE_STUTTER", "DELETE_FILLER", "DELETE_SELECTION", "COMPRESS_SILENCE"}
ACTIVE_JOBS = ["QUEUED", "PROCESSING", "QUALITY_CHECKING"]


class RenderConflict(Exception):
    pass


class RenderInputError(Exception):
    pass


def decision_snapshot(db: Session, session_id: str) -> list[dict]:
    return [dict(id=d.id, start_ms=d.start_ms, end_ms=d.end_ms, action=d.action,
                 accepted=d.accepted, hard_protected=d.hard_protected,
                 **({"replacement_version_id": d.replacement_version_id,
                     "replacement_duration_ms": d.replacement_duration_ms} if d.action == "REPLACE_AUDIO" else {}))
            for d in db.query(EditDecision).filter_by(session_id=session_id).order_by(EditDecision.id).all()]


def fingerprint(source_id: str, decisions: list[dict]) -> str:
    payload = json.dumps([source_id, decisions], sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def render_source(db: Session, session_id: str) -> AudioVersion:
    role = db.query(RoleAssignment).filter_by(session_id=session_id, role="RENDER_SOURCE").first()
    source = db.get(AudioVersion, role.audio_version_id) if role else None
    if source is None or source.session_id != session_id or source.version_type == "master" or not source.filename:
        raise RenderConflict("完整时间轴的音频源尚未就绪")
    return source


def is_stale(db: Session, output: RenderOutput) -> bool:
    try:
        source = render_source(db, output.session_id)
    except RenderConflict:
        return True
    return fingerprint(source.id, decision_snapshot(db, output.session_id)) != output.fingerprint


def validate_session(db: Session, session_id: str) -> RecordingSession:
    session = db.get(RecordingSession, session_id)
    if session is None or session.capture_status != "STOPPED" or session.live_analysis_status != "FINALIZED":
        raise RenderConflict("请先完成录制分析")
    if db.query(EnhancementJob).filter(EnhancementJob.session_id == session_id,
                                      EnhancementJob.status.in_(ACTIVE_JOBS)).first():
        raise RenderConflict("增强处理中，请完成后再渲染")
    return session


def accepted_cuts(decisions: list[dict], duration_ms: int) -> list[tuple[int, int]]:
    protected = [(d["start_ms"], d["end_ms"]) for d in decisions
                 if d["hard_protected"] or d["action"] == "KEEP_PROTECTED"]
    cuts = []
    for decision in decisions:
        if decision["hard_protected"] or decision["action"] == "KEEP_PROTECTED":
            continue
        if decision["accepted"] is None:
            raise RenderConflict("还有待定决策，请先接受或拒绝后再渲染")
        if not decision["accepted"]:
            continue
        if decision["action"] == "REPLACE_AUDIO":
            continue
        if decision["action"] not in DELETE_ACTIONS:
            raise RenderInputError("存在尚不支持的决策类型，请拒绝后再渲染")
        start, end = decision["start_ms"], decision["end_ms"]
        if not 0 <= start < end <= duration_ms:
            raise RenderInputError("删除区间超出原始时间轴，请修正或拒绝该决策")
        if any(start < p_end and end > p_start for p_start, p_end in protected):
            raise RenderConflict("删除区间与保护区间重叠，请拒绝冲突决策")
        cuts.append((start, end))
    merged: list[tuple[int, int]] = []
    for start, end in sorted(cuts):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    if merged == [(0, duration_ms)]:
        raise RenderInputError("不能删除全部音频，请至少保留一个片段")
    return merged


@dataclass
class RenderedAudio:
    sample_rate: int
    sample_count: int
    removed_duration_ms: int


def write_render(source: Path, destination: Path, cuts: list[tuple[int, int]], smooth_cuts: bool = False) -> RenderedAudio:
    # Decode to temporary PCM on disk, avoiding a full recording held in memory.
    with tempfile.TemporaryDirectory(prefix=".render-", dir=destination.parent) as temp:
        decoded = Path(temp) / "decoded.wav"
        try:
            result = subprocess.run(
                ["ffmpeg", "-v", "error", "-nostdin", "-i", str(source), "-vn",
                 "-c:a", "pcm_s16le", str(decoded)], capture_output=True, timeout=120,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RenderInputError("音频解码失败或超时，未生成导出版本") from exc
        if result.returncode:
            raise RenderInputError("音频无法解码，未生成导出版本")
        try:
            with wave.open(str(decoded), "rb") as audio:
                rate, frames = audio.getframerate(), audio.getnframes()
                removed = [(round(start * rate / 1000), round(end * rate / 1000)) for start, end in cuts]
                if any(not 0 <= start < end <= frames for start, end in removed):
                    raise RenderInputError("删除区间超出源文件的实际采样范围")
                retained: list[tuple[int, int]] = []
                cursor = 0
                for start, end in removed:
                    if start > cursor:
                        retained.append((cursor, start))
                    cursor = end
                if cursor < frames:
                    retained.append((cursor, frames))
                count = sum(end - start for start, end in retained)
                if count == 0:
                    raise RenderInputError("不能删除全部音频，请至少保留一个片段")
                with wave.open(str(destination), "wb") as output:
                    output.setnchannels(audio.getnchannels())
                    output.setsampwidth(audio.getsampwidth())
                    output.setframerate(rate)
                    for start, end in retained:
                        audio.setpos(start)
                        remaining = end - start
                        while remaining:
                            block = min(remaining, 65536)
                            data = audio.readframes(block)
                            if len(data) != block * audio.getnchannels() * audio.getsampwidth():
                                raise RenderInputError("源音频读取不完整，未生成导出版本")
                            if smooth_cuts:
                                # Fade only cut edges, with identical gains across all channels.
                                # Gains use segment offsets so block boundaries cannot restart a fade.
                                fade = min(round(rate * 0.005), (end - start) // 2)
                                if fade >= 2:
                                    positions = np.arange(end - start - remaining, end - start - remaining + block)
                                    gain = np.ones(block)
                                    if start > 0:
                                        gain *= np.clip(positions / (fade - 1), 0, 1)
                                    if end < frames:
                                        gain *= np.clip((end - start - 1 - positions) / (fade - 1), 0, 1)
                                    pcm = np.frombuffer(data, dtype='<i2').reshape(-1, audio.getnchannels())
                                    data = np.rint(pcm * gain[:, None]).astype('<i2').tobytes()
                            output.writeframesraw(data)
                            remaining -= block
                return RenderedAudio(rate, count, round((frames - count) * 1000 / rate))
        except (wave.Error, EOFError) as exc:
            raise RenderInputError("源音频格式不可用") from exc


def render_session(db: Session, session_id: str, smooth_cuts: bool = False,
                   loudness_target: str | None = None) -> RenderOutput:
    session = validate_session(db, session_id)
    source = render_source(db, session_id)
    decisions = decision_snapshot(db, session_id)
    cuts = accepted_cuts(decisions, session.total_duration_ms)
    replacements = accepted_replacements(db, session_id, decisions, cuts, session.total_duration_ms)
    signature = fingerprint(source.id, decisions)
    source_id = source.id
    source_path = Path(storage.session_dir(session_id)) / source.filename
    if not source_path.is_file():
        raise RenderConflict("音频源文件缺失")
    # Release the read transaction during decoding; revalidate under a write lock before publishing.
    db.rollback()
    version_id = f"av_{uuid.uuid4().hex}"
    output_path = Path(storage.session_dir(session_id)) / f"master_{version_id}.wav"
    published = False
    try:
        if replacements:
            rendered = write_replacement_render(source_path, output_path, cuts, replacements, smooth_cuts)
        else:
            rendered = write_render(source_path, output_path, cuts, smooth_cuts=True) if smooth_cuts else write_render(source_path, output_path, cuts)
        loudness_info = None
        if loudness_target:
            # 线性增益不改变样本数，rendered 的时长统计依然有效。
            normalized = output_path.with_name(output_path.stem + ".loudnorm.wav")
            try:
                loudness_info = dataclasses.asdict(loudness.normalize(output_path, normalized, loudness_target))
                os.replace(normalized, output_path)
            except loudness.LoudnessError as exc:
                raise RenderInputError(f"响度标准化失败：{exc}") from exc
            finally:
                normalized.unlink(missing_ok=True)
        db.execute(text("BEGIN IMMEDIATE"))
        validate_session(db, session_id)
        current_source = render_source(db, session_id)
        if fingerprint(current_source.id, decision_snapshot(db, session_id)) != signature:
            raise RenderConflict("渲染期间音频源或决策已变化，请按最新选择重新渲染")
        version = AudioVersion(
            id=version_id, session_id=session_id, version_type="master", label="剪辑成片",
            duration_ms=round(rendered.sample_count * 1000 / rendered.sample_rate),
            sample_count=rendered.sample_count, sample_rate=rendered.sample_rate,
            latency_ms=0, engine=("pcm_replace_fade" if smooth_cuts else "pcm_replace") if replacements else ("pcm_cut_fade" if smooth_cuts else "pcm_cut"), engine_version="1.0", determinism="deterministic",
            processing_locality="local", filename=output_path.name,
        )
        db.add(version)
        db.flush()
        output = RenderOutput(
            session_id=session_id, version_id=version.id, source_version_id=source_id,
            fingerprint=signature, decisions=decisions,
            transcript_snapshot=transcript_export.snapshot(db, session_id),
            cut_ranges=[{"start_ms": start, "end_ms": end} for start, end in cuts],
            removed_duration_ms=rendered.removed_duration_ms,
            loudness=loudness_info,
        )
        db.add(output)
        role = db.query(RoleAssignment).filter_by(session_id=session_id, role="EXPORT_SOURCE").first()
        if role:
            role.upgraded_from = role.audio_version_id
            role.audio_version_id = version.id
        else:
            db.add(RoleAssignment(session_id=session_id, role="EXPORT_SOURCE", audio_version_id=version.id))
        # RENDER_SOURCE remains uncut, so subsequent renders never cut an already shortened file.
        db.commit()
        published = True
        db.refresh(output)
        return output
    except Exception:
        db.rollback()
        raise
    finally:
        if not published:
            output_path.unlink(missing_ok=True)


def accepted_replacements(db, session_id, decisions, cuts, duration_ms):
    replacements = []
    occupied = list(cuts) + [(d['start_ms'], d['end_ms']) for d in decisions
                             if d['hard_protected'] or d['action'] == 'KEEP_PROTECTED']
    for d in sorted(decisions, key=lambda d: d['start_ms']):
        if d['action'] != 'REPLACE_AUDIO' or d['accepted'] is not True or d['hard_protected']:
            continue
        start, end = d['start_ms'], d['end_ms']
        if not 0 <= start < end <= duration_ms:
            raise RenderInputError('重录区间超出原始时间轴')
        if any(start < b and a < end for a, b in occupied):
            raise RenderConflict('重录与删除、保护或其他重录区间重叠，请拒绝冲突决策后再生成')
        version = db.get(AudioVersion, d.get('replacement_version_id')) if d.get('replacement_version_id') else None
        if not version or version.session_id != session_id or version.version_type != 'retake' or not version.filename:
            raise RenderConflict('重录音频版本缺失，请重新录制该片段')
        path = Path(storage.session_dir(session_id)) / version.filename
        if not path.is_file():
            raise RenderConflict('重录音频文件缺失，请重新录制该片段')
        replacements.append((start, end, path))
        occupied.append((start, end))
    return replacements


def write_replacement_render(source, destination, cuts, replacements, smooth_cuts=False):
    """Splice immutable takes into the original timeline without changing speaking speed."""
    with tempfile.TemporaryDirectory(prefix='.retake-render-', dir=destination.parent) as temp:
        decoded = Path(temp) / 'source.wav'
        write_render(source, decoded, [])
        with wave.open(str(decoded), 'rb') as original:
            rate, channels, frames = original.getframerate(), original.getnchannels(), original.getnframes()
            edits = sorted([(a, b, None) for a, b in cuts] + replacements, key=lambda item: item[0])
            pieces = []
            cursor = 0
            for index, (start_ms, end_ms, replacement) in enumerate(edits):
                start, end = round(start_ms * rate / 1000), round(end_ms * rate / 1000)
                if not cursor <= start < end <= frames:
                    raise RenderInputError('剪辑区间重叠或超出源文件实际采样范围')
                if start > cursor:
                    pieces.append((decoded, cursor, start))
                if replacement:
                    take = Path(temp) / f'take-{index}.wav'
                    try:
                        result = subprocess.run(['ffmpeg', '-v', 'error', '-nostdin', '-i', str(replacement),
                                                 '-vn', '-ar', str(rate), '-ac', str(channels),
                                                 '-c:a', 'pcm_s16le', str(take)], capture_output=True, timeout=120)
                    except (OSError, subprocess.TimeoutExpired) as exc:
                        raise RenderInputError('重录音频解码失败或超时') from exc
                    if result.returncode:
                        raise RenderInputError('重录音频无法解码')
                    with wave.open(str(take), 'rb') as audio:
                        if audio.getnframes() == 0:
                            raise RenderInputError('重录音频为空')
                        pieces.append((take, 0, audio.getnframes()))
                cursor = end
            if cursor < frames:
                pieces.append((decoded, cursor, frames))
            count = sum(end - start for _, start, end in pieces)
            if not count:
                raise RenderInputError('成片不能为空')
            with wave.open(str(destination), 'wb') as output:
                output.setnchannels(channels)
                output.setsampwidth(2)
                output.setframerate(rate)
                for index, (path, start, end) in enumerate(pieces):
                    with wave.open(str(path), 'rb') as audio:
                        audio.setpos(start)
                        offset, length = 0, end - start
                        fade = min(round(rate * 0.005), length // 2)
                        while offset < length:
                            block = min(65536, length - offset)
                            data = audio.readframes(block)
                            if len(data) != block * channels * 2:
                                raise RenderInputError('音频读取不完整，未生成成片')
                            if smooth_cuts and fade >= 2:
                                positions = np.arange(offset, offset + block)
                                gain = np.ones(block)
                                if index > 0:
                                    gain *= np.clip(positions / (fade - 1), 0, 1)
                                if index < len(pieces) - 1:
                                    gain *= np.clip((length - 1 - positions) / (fade - 1), 0, 1)
                                pcm = np.frombuffer(data, dtype='<i2').reshape(-1, channels)
                                data = np.rint(pcm * gain[:, None]).astype('<i2').tobytes()
                            output.writeframesraw(data)
                            offset += block
            return RenderedAudio(rate, count, round((frames - count) * 1000 / rate))
