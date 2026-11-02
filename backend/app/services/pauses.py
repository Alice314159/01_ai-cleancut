"""
按真实音频能量查找过长停顿，生成"压缩停顿"候选。

只看声学能量，不做语义判断：阈值相对本段录音自身的底噪和语音电平自适应，
底噪与语音差距太小（比如全程音乐或大噪声）时不给建议，避免误删说话内容。
每段停顿只删掉中间部分，两侧各保留 keep_ms/2，让句间仍有自然的呼吸感。
"""
from dataclasses import dataclass

import numpy as np

FRAME_MS = 10
# 底噪与语音至少要差这么多，能量门限才可信。
MIN_DYNAMIC_RANGE_DB = 12.0


@dataclass(frozen=True)
class Pause:
    start_ms: int  # 静音段起点（原始时间轴）
    end_ms: int  # 静音段终点
    cut_start_ms: int  # 建议删除区间
    cut_end_ms: int

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms


def _frame_db(samples: np.ndarray, sample_rate: int) -> np.ndarray:
    frame = max(1, sample_rate * FRAME_MS // 1000)
    count = len(samples) // frame
    if count == 0:
        return np.zeros(0)
    frames = samples[: count * frame].astype(np.float64).reshape(count, frame)
    rms = np.sqrt(np.mean(frames ** 2, axis=1))
    return 20 * np.log10(np.maximum(rms, 1e-6))


def silence_threshold_db(levels: np.ndarray) -> float | None:
    """返回静音门限；动态范围不足以区分语音与底噪时返回 None。"""
    if len(levels) == 0:
        return None
    floor = float(np.percentile(levels, 10))
    speech = float(np.percentile(levels, 95))
    if speech - floor < MIN_DYNAMIC_RANGE_DB:
        return None
    return min(floor + max(6.0, 0.25 * (speech - floor)), speech - 15.0)


def find_long_pauses(samples: np.ndarray, sample_rate: int, *, min_pause_ms: int = 1500,
                     keep_ms: int = 500) -> list[Pause]:
    if keep_ms >= min_pause_ms:
        raise ValueError("保留时长必须小于停顿阈值")
    levels = _frame_db(samples, sample_rate)
    threshold = silence_threshold_db(levels)
    if threshold is None:
        return []
    silent = levels < threshold
    total_ms = len(levels) * FRAME_MS
    pauses = []
    # 找连续静音帧的起止。两端各补一个 False，方便用 diff 求边界。
    edges = np.diff(np.concatenate(([False], silent, [False])).astype(np.int8))
    starts, ends = np.flatnonzero(edges == 1), np.flatnonzero(edges == -1)
    for start_frame, end_frame in zip(starts, ends):
        start_ms, end_ms = int(start_frame * FRAME_MS), int(end_frame * FRAME_MS)
        if end_ms - start_ms < min_pause_ms:
            continue
        half = keep_ms // 2
        # 开头/结尾的静音只在靠语音一侧留余量。
        cut_start = start_ms if start_ms == 0 else start_ms + half
        cut_end = end_ms if end_ms >= total_ms else end_ms - half
        if cut_start == 0 and cut_end >= total_ms:
            continue  # 整段都是静音，不能全删
        if cut_end - cut_start >= FRAME_MS:
            pauses.append(Pause(start_ms, end_ms, cut_start, cut_end))
    return pauses
