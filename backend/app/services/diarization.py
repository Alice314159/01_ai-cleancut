"""
说话人区分（可选）：用 sherpa-onnx 在本机 CPU 上判断“谁在什么时候说话”。

- 分段模型 pyannote segmentation 3.0：找出每一小段里有几个人在说话；
- 声纹模型 3D-Speaker ERes2Net（中文 16 kHz）：给每段算声纹向量，再聚类成不同的人。

它比音高判断可靠得多：同性别、音高接近的两个人也能分开。但它是可选依赖——
没装 sherpa-onnx 或没下载模型时 available() 为 False，调用方退回原有的音高判断。
模型文件用 scripts/prepare_diarization.py 下载，音频始终不离开本机。
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import importlib.util
import logging
from pathlib import Path
import threading

import numpy as np

from app import config

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16000
SEGMENTATION_FILE = "segmentation.onnx"
EMBEDDING_FILE = "embedding.onnx"
# 一个“人”至少要占说话时长的这个比例且累计这么多秒，才算一位说话人——
# 偶尔一两个词的聚类抖动、咳嗽、笑声不应该变成“第三个人”。
MIN_SPEAKER_SHARE = 0.1
MIN_SPEAKER_SECONDS = 3.0

# 同一时间只跑一个推理：模型本身已经多线程，并发只会抢内存和 CPU。
_lock = threading.Lock()


@dataclass(frozen=True)
class Segment:
    start_ms: int
    end_ms: int
    speaker: int


@dataclass(frozen=True)
class SpeakerCount:
    speakers: int               # 达到门槛的说话人数
    shares: tuple[float, ...]   # 各说话人占说话时长的比例，从多到少
    analyzed_seconds: float


def package_available() -> bool:
    return importlib.util.find_spec("sherpa_onnx") is not None


def model_dir() -> Path:
    return config.DIARIZATION_MODEL_DIR


def models_present(directory: Path | None = None) -> bool:
    directory = directory or model_dir()
    return all((directory / name).is_file() and (directory / name).stat().st_size > 0
               for name in (SEGMENTATION_FILE, EMBEDDING_FILE))


def status() -> str:
    """ready / missing_package / missing_models，用于界面和日志说明为什么没用上模型。"""
    if not package_available():
        return "missing_package"
    if not models_present():
        return "missing_models"
    return "ready"


def available() -> bool:
    return status() == "ready"


@lru_cache(maxsize=2)
def _engine(directory: str, threshold: float, threads: int):
    import sherpa_onnx

    cfg = sherpa_onnx.OfflineSpeakerDiarizationConfig(
        segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
            pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(model=f"{directory}/{SEGMENTATION_FILE}"),
            num_threads=threads,
        ),
        embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=f"{directory}/{EMBEDDING_FILE}", num_threads=threads),
        # 人数未知（-1），按声纹距离阈值聚类
        clustering=sherpa_onnx.FastClusteringConfig(num_clusters=-1, threshold=threshold),
        min_duration_on=0.3,
        min_duration_off=0.5,
    )
    if not cfg.validate():
        raise RuntimeError("说话人区分模型配置无效，请重新运行 scripts/prepare_diarization.py")
    return sherpa_onnx.OfflineSpeakerDiarization(cfg)


def _threads() -> int:
    import os
    return max(1, min(4, (os.cpu_count() or 2)))


def diarize(samples: np.ndarray, sample_rate: int = SAMPLE_RATE) -> list[Segment]:
    """整段音频的说话人分段（时间相对 samples 开头）。模型不可用时抛 RuntimeError。"""
    if sample_rate != SAMPLE_RATE:
        raise ValueError(f"说话人区分需要 {SAMPLE_RATE} Hz 单声道音频")
    if not available():
        raise RuntimeError(f"说话人区分不可用：{status()}")
    audio = np.ascontiguousarray(samples, dtype=np.float32)
    if audio.size < SAMPLE_RATE:      # 不足 1 秒，没有可分的内容
        return []
    with _lock:
        engine = _engine(str(model_dir()), config.DIARIZATION_THRESHOLD, _threads())
        result = engine.process(audio).sort_by_start_time()
    return [Segment(round(s.start * 1000), round(s.end * 1000), int(s.speaker)) for s in result]


def excerpts(samples: np.ndarray, sample_rate: int, seconds: float = 40.0, count: int = 3) -> np.ndarray:
    """长录音只取开头、中间、结尾几段拼起来分析（同一次聚类，跨段的同一个人仍归为一人），控制耗时。"""
    length = int(seconds * sample_rate)
    if samples.size <= length * count:
        return samples
    starts = np.linspace(0, samples.size - length, count).astype(int)
    gap = np.zeros(sample_rate // 2, dtype=np.float32)
    parts = []
    for start in starts:
        parts += [samples[start:start + length], gap]
    return np.concatenate(parts[:-1])


def summarize(segments: list[Segment]) -> SpeakerCount:
    totals: dict[int, float] = {}
    for s in segments:
        totals[s.speaker] = totals.get(s.speaker, 0.0) + (s.end_ms - s.start_ms) / 1000
    speech = sum(totals.values())
    if speech <= 0:
        return SpeakerCount(0, (), 0.0)
    ranked = sorted(totals.values(), reverse=True)
    significant = [t for t in ranked if t / speech >= MIN_SPEAKER_SHARE and t >= MIN_SPEAKER_SECONDS]
    return SpeakerCount(len(significant), tuple(round(t / speech, 3) for t in ranked), round(speech, 1))


def normalize(segments: list[Segment]) -> list[Segment]:
    """按第一次出现的先后重新编号（第一个开口的是 A），合并同一人之间不足 0.3 秒的空隙。"""
    order: dict[int, int] = {}
    merged: list[Segment] = []
    for s in sorted(segments, key=lambda s: (s.start_ms, s.end_ms)):
        speaker = order.setdefault(s.speaker, len(order))
        if merged and merged[-1].speaker == speaker and s.start_ms - merged[-1].end_ms < 300:
            merged[-1] = Segment(merged[-1].start_ms, max(merged[-1].end_ms, s.end_ms), speaker)
        else:
            merged.append(Segment(s.start_ms, s.end_ms, speaker))
    return merged


def speaker_at(segments, start_ms: int, end_ms: int) -> int | None:
    """与 [start_ms, end_ms) 重叠最多的说话人；segments 为 [[start, end, speaker], ...]。"""
    best, best_overlap = None, 0
    for seg_start, seg_end, speaker in segments or ():
        if seg_start >= end_ms:
            break
        overlap = min(end_ms, seg_end) - max(start_ms, seg_start)
        if overlap > best_overlap:
            best, best_overlap = speaker, overlap
    if best is None and segments and end_ms > start_ms:
        # 词落在两段之间的缝里（分段模型会略掉很短的词）：取最近的一段，相距 1 秒以内才算
        mid = (start_ms + end_ms) / 2
        nearest = min(segments, key=lambda s: min(abs(mid - s[0]), abs(mid - s[1])))
        if min(abs(mid - nearest[0]), abs(mid - nearest[1])) <= 1000:
            best = nearest[2]
    return best


def session_segments(db, session_id: str):
    """转写要标的分段：只有模型判断为多人时才标，单人录音全篇“说话人 A”没有意义。"""
    from app.models import AudioQualityReport

    report = db.get(AudioQualityReport, session_id)
    if report is None or report.speaker_analysis_status != "done" or (report.speaker_count or 0) < 2:
        return None
    return report.speaker_segments or None


def label_tokens(segments, tokens) -> list[int | None]:
    """逐词取说话人（tokens 有 start_ms / end_ms，按时间排序）。"""
    if not segments:
        return [None] * len(tokens)
    return [speaker_at(segments, t.start_ms, t.end_ms) if t.end_ms is not None else None for t in tokens]


def speaker_name(speaker: int) -> str:
    letters = ""
    n = speaker
    while True:
        letters = chr(ord("A") + n % 26) + letters
        n = n // 26 - 1
        if n < 0:
            return f"说话人 {letters}"


# 超过这个时长只抽样估计人数，不做全程分段（约 0.13× 实时，3 小时要二十多分钟）
MAX_FULL_SECONDS = 3 * 3600
SPEECH_TYPES = ("speech", "multi_speaker_speech", "single_speaker_speech")


def refine_content_type(report, count: SpeakerCount) -> None:
    """模型结果覆盖音高判断，但不改用户手动指定的类型，也不改“人声 + 背景音乐”等非纯人声类型。"""
    report.speaker_count = count.speakers
    if report.content_type_overridden_by_user or report.content_type not in SPEECH_TYPES:
        return
    if count.speakers >= 2:
        report.content_type, report.content_type_confidence = "multi_speaker_speech", 0.85
    elif count.speakers == 1:
        report.content_type, report.content_type_confidence = "single_speaker_speech", 0.8


def analyze_session(session_id: str) -> None:
    """后台任务：对原始录音做说话人区分，写回质量报告。失败只记状态，不影响其它流程。"""
    from app import db as db_module
    from app.models import AudioQualityReport
    from app.services import audio_analysis, storage

    with db_module.SessionLocal() as db:
        report = db.get(AudioQualityReport, session_id)
        if report is None or report.speaker_analysis_status not in ("pending", "failed"):
            return
        report.speaker_analysis_status = "running"
        db.commit()
    try:
        samples = audio_analysis.decode_pcm(Path(storage.raw_chunk_path(session_id)))
        full = samples.size <= MAX_FULL_SECONDS * SAMPLE_RATE
        segments = normalize(diarize(samples if full else excerpts(samples, SAMPLE_RATE), SAMPLE_RATE))
        count = summarize(segments)
    except Exception:  # noqa: BLE001
        logger.exception("说话人区分失败：%s", session_id)
        with db_module.SessionLocal() as db:
            report = db.get(AudioQualityReport, session_id)
            if report is not None:
                report.speaker_analysis_status = "failed"
                db.commit()
        return
    with db_module.SessionLocal() as db:
        report = db.get(AudioQualityReport, session_id)
        if report is None:      # 分析期间会话被删除
            return
        refine_content_type(report, count)
        report.speaker_segments = [[s.start_ms, s.end_ms, s.speaker] for s in segments] if full else None
        report.speaker_analysis_status = "done"
        db.commit()


def recover_interrupted() -> list[str]:
    """服务重启时，把中断的分析重新排队（返回需要重跑的会话）。"""
    from app import db as db_module
    from app.models import AudioQualityReport

    with db_module.SessionLocal() as db:
        rows = db.query(AudioQualityReport).filter(AudioQualityReport.speaker_analysis_status.in_(("pending", "running"))).all()
        ids = []
        for row in rows:
            if available():
                row.speaker_analysis_status = "pending"
                ids.append(row.session_id)
            else:
                row.speaker_analysis_status = "unavailable"
        db.commit()
    return ids
