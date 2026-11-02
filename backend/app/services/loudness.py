"""
成片响度标准化（EBU R128 / ITU-R BS.1770 积分响度）。

只做整体线性增益，不做动态压缩或限幅：样本数不变（时间轴、字幕都不受影响），
输出确定可复现。增益受真峰值上限约束——达到目标响度会让峰值超限时，
宁可响度略低于目标，也不引入削波，结果里会如实报告实际达到的响度。
测量用 ffmpeg 的 loudnorm 滤镜（只测量，不使用它的输出）。
"""
from dataclasses import dataclass
import json
import math
from pathlib import Path
import re
import subprocess
import wave

import numpy as np

# preset -> (目标积分响度 LUFS, 真峰值上限 dBTP)
TARGETS: dict[str, tuple[float, float]] = {
    "podcast": (-16.0, -1.5),  # 播客 / 流媒体常用
    "broadcast": (-23.0, -1.0),  # EBU R128 广播
}
# 安静录音最多提升这么多，避免把底噪放大到不可用。
MAX_GAIN_DB = 20.0
# 低于这个响度视为近乎静音，不做处理。
MIN_MEASURABLE_LUFS = -70.0


class LoudnessError(Exception):
    pass


@dataclass(frozen=True)
class LoudnessResult:
    target: str
    target_lufs: float
    input_lufs: float | None
    input_true_peak_db: float | None
    gain_db: float
    output_lufs: float | None
    # 未达到目标的原因：None=已达到；"peak"=受真峰值上限约束；"max_gain"=提升幅度已到上限
    limited_by: str | None


def measure(path: Path) -> tuple[float | None, float | None]:
    """返回 (积分响度 LUFS, 真峰值 dBTP)；静音返回 (None, None)。"""
    try:
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-nostdin", "-i", str(path), "-vn",
             "-af", "loudnorm=print_format=json", "-f", "null", "-"],
            capture_output=True, timeout=300,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise LoudnessError("响度测量失败或超时") from exc
    if result.returncode:
        raise LoudnessError("响度测量失败")
    stderr = result.stderr.decode("utf-8", errors="ignore")
    match = None
    for match in re.finditer(r"\{[^{}]*\"input_i\"[^{}]*\}", stderr):
        pass
    if match is None:
        raise LoudnessError("无法解析响度测量结果")
    data = json.loads(match.group(0))

    def number(key):
        try:
            value = float(data[key])
        except (KeyError, TypeError, ValueError):
            return None
        return value if math.isfinite(value) else None

    lufs, peak = number("input_i"), number("input_tp")
    if lufs is None or lufs < MIN_MEASURABLE_LUFS:
        return None, None
    return lufs, peak


def plan_gain(input_lufs: float | None, input_peak: float | None, target: str) -> tuple[float, str | None]:
    target_lufs, peak_ceiling = TARGETS[target]
    if input_lufs is None:
        return 0.0, None
    gain, limited = target_lufs - input_lufs, None
    if gain > MAX_GAIN_DB:
        gain, limited = MAX_GAIN_DB, "max_gain"
    if input_peak is not None and input_peak + gain > peak_ceiling:
        gain, limited = peak_ceiling - input_peak, "peak"
    return gain, limited


def apply_gain(source: Path, destination: Path, gain_db: float) -> None:
    """16-bit PCM WAV 按块乘以线性增益，保持声道、采样率和样本数不变。"""
    factor = 10 ** (gain_db / 20)
    with wave.open(str(source), "rb") as audio, wave.open(str(destination), "wb") as output:
        if audio.getsampwidth() != 2:
            raise LoudnessError("仅支持 16-bit PCM 成片")
        output.setnchannels(audio.getnchannels())
        output.setsampwidth(2)
        output.setframerate(audio.getframerate())
        while data := audio.readframes(65536):
            pcm = np.frombuffer(data, dtype="<i2").astype(np.float64) * factor
            output.writeframesraw(np.clip(np.rint(pcm), -32768, 32767).astype("<i2").tobytes())


def normalize(source: Path, destination: Path, target: str) -> LoudnessResult:
    if target not in TARGETS:
        raise LoudnessError(f"未知的响度目标：{target}")
    target_lufs = TARGETS[target][0]
    input_lufs, input_peak = measure(source)
    gain, limited = plan_gain(input_lufs, input_peak, target)
    apply_gain(source, destination, gain)
    return LoudnessResult(
        target=target, target_lufs=target_lufs, input_lufs=input_lufs, input_true_peak_db=input_peak,
        gain_db=round(gain, 2), output_lufs=None if input_lufs is None else round(input_lufs + gain, 2),
        limited_by=limited,
    )
