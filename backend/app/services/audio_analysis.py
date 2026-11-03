"""
真实的音频信号分析——不是随机数。用 ffmpeg 解码录制文件，用 numpy 算一组
基于实际波形的指标，并且真的对音频做谱减法降噪（不是神经网络，但是真实的
经典信号处理算法，能实际改变波形、能听出差别）。这里没有机器学习（说话人分类、
真正的混响时间估计、真正的响度积分算法 ITU-R BS.1770、真正的基频跟踪都需要
更重的实现），所以：

  - integrated_lufs / true_peak_dbtp 是基于 RMS/峰值的近似值，不是标准 LUFS 算法
  - hum_probability 是真实的 FFT 频谱能量占比（50/60Hz 附近）
  - reverb_score 是频谱平坦度的代理指标，不是真正的 RT60 混响时间
  - content_type（单人/多人/音乐）仍然是固定值——真正做需要说话人分离模型
  - "pitch_deviation"（对外字段名）实际算的是频谱质心偏移，不是逐帧基频跟踪
  - timbre_similarity 是频谱形状的余弦相似度，不是声纹模型算出的音色相似度

这些近似之处都在下面每个函数的注释里标注了，不是憋着不说。
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import numpy as np

ANALYSIS_SAMPLE_RATE = 16000


@lru_cache(maxsize=128)
def _hanning(n: int) -> np.ndarray:
    """汉宁窗只取决于长度，多次用同一个 n 重复计算是纯浪费；结果不可变，可以放心缓存复用。"""
    return np.hanning(n)


class DecodeError(Exception):
    pass


def _run_ffmpeg(command: list[str], *, capture_output: bool, timeout: int):
    try:
        return subprocess.run(command, capture_output=capture_output, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DecodeError(f"ffmpeg 解码不可用或超时：{exc}") from exc


def decode_pcm(path: Path, sample_rate: int = ANALYSIS_SAMPLE_RATE) -> np.ndarray:
    """用 ffmpeg 把任意音频/视频容器解成单声道 16kHz float32 PCM，范围 [-1, 1]。"""
    if not path.exists() or path.stat().st_size == 0:
        raise DecodeError("audio file missing or empty")

    proc = _run_ffmpeg(
        [
            "ffmpeg", "-v", "error", "-i", str(path),
            "-f", "s16le", "-ac", "1", "-ar", str(sample_rate), "pipe:1",
        ],
        capture_output=True,
        timeout=30,
    )
    if proc.returncode != 0 or len(proc.stdout) == 0:
        raise DecodeError(proc.stderr.decode("utf-8", errors="ignore")[:300] or "ffmpeg produced no output")

    pcm = np.frombuffer(proc.stdout, dtype=np.int16).astype(np.float32) / 32768.0
    return pcm


def decode_tail_pcm(path: Path, seconds: float = 3.0, sample_rate: int = ANALYSIS_SAMPLE_RATE) -> np.ndarray:
    """
    只解码文件末尾最近 N 秒，用于录制过程中周期性地分析"刚刚发生了什么"，
    不必每次都重新解码整个还在增长的文件。文件比 N 秒短时 ffmpeg 会自动从头开始，
    这是预期行为。
    """
    if not path.exists() or path.stat().st_size == 0:
        raise DecodeError("audio file missing or empty")

    proc = _run_ffmpeg(
        [
            "ffmpeg", "-v", "error", "-sseof", f"-{seconds}", "-i", str(path),
            "-f", "s16le", "-ac", "1", "-ar", str(sample_rate), "pipe:1",
        ],
        capture_output=True,
        timeout=10,
    )
    if proc.returncode != 0 or len(proc.stdout) == 0:
        raise DecodeError(proc.stderr.decode("utf-8", errors="ignore")[:300] or "ffmpeg produced no output")

    pcm = np.frombuffer(proc.stdout, dtype=np.int16).astype(np.float32) / 32768.0
    return pcm[-max(1, int(seconds * sample_rate)):]


def probe_format(path: Path) -> tuple[int, int]:
    """
    读取导入音频的真实采样率与声道数，仅用于原始版本的展示元数据——内部分析和降噪
    始终把音频重采样成 16kHz 单声道，不依赖这里探测到的值。探测失败不代表文件不可用，
    调用方应该回退到默认值，真正的可用性判断交给 decode_pcm。
    """
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a:0",
             "-show_entries", "stream=sample_rate,channels",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DecodeError(f"ffprobe 不可用或超时：{exc}") from exc
    if proc.returncode != 0:
        raise DecodeError(proc.stderr.decode("utf-8", errors="ignore")[:300] or "ffprobe 未能读取音频信息")
    lines = proc.stdout.decode("utf-8", errors="ignore").split()
    if len(lines) < 2:
        raise DecodeError("上传的文件未包含可识别的音频流")
    try:
        return int(lines[0]), int(lines[1])
    except ValueError as exc:
        raise DecodeError("ffprobe 输出格式异常") from exc


@dataclass
class RealtimeQualityEvent:
    event_type: str  # 'no_input' | 'low_volume' | 'clipping' | 'noise_spike'
    severity: str  # 'notice' | 'warning'
    message: str
    metrics: dict[str, float]


def detect_realtime_quality_events(segment: np.ndarray, sample_rate: int = ANALYSIS_SAMPLE_RATE) -> list[RealtimeQualityEvent]:
    """
    对最近一小段录音做真实的电平检测——不是随机抽一个事件类型。只有真的触发阈值才会
    产生事件，正常说话大部分时候不会有任何事件，这本身也更真实（不像固定概率轮盘）。

    只做这四种能单纯从电平/削波判断、不需要模型的事件；far_field/echo_risk/
    bluetooth_degraded/format_change/device_lost/no_speech 需要更多上下文
    （多麦克风、编解码器元数据、ASR），这里不冒充。
    """
    events: list[RealtimeQualityEvent] = []
    if len(segment) == 0:
        return events

    rms = float(np.sqrt(np.mean(segment.astype(np.float64) ** 2) + 1e-12))
    peak = float(np.max(np.abs(segment)))
    rms_dbfs = _dbfs(rms)
    clipping_ratio = float(np.mean(np.abs(segment) > 0.98))

    if rms_dbfs < -55:
        events.append(RealtimeQualityEvent(
            "no_input", "warning", "检测不到有效输入信号", {"rmsDbfs": round(rms_dbfs, 1)}
        ))
    elif rms_dbfs < -40:
        events.append(RealtimeQualityEvent(
            "low_volume", "notice", "音量偏低，建议靠近麦克风", {"rmsDbfs": round(rms_dbfs, 1)}
        ))

    if clipping_ratio > 0.005:
        events.append(RealtimeQualityEvent(
            "clipping", "warning", "检测到削波，建议降低输入增益",
            {"clippingRatio": round(clipping_ratio, 4)},
        ))

    # 瞬时尖峰：峰值远超这段音频自身的典型幅度（用中位数而不是均值，避免被峰值本身带偏）
    median_abs = float(np.median(np.abs(segment))) + 1e-6
    if peak > 0.5 and peak > median_abs * 12:
        events.append(RealtimeQualityEvent(
            "noise_spike", "notice", "检测到突发噪声",
            {"peakDbfs": round(_dbfs(peak), 1)},
        ))

    return events


def has_speech_activity(segment: np.ndarray, sample_rate: int = ANALYSIS_SAMPLE_RATE) -> bool:
    """
    粗粒度的能量型语音活动检测（VAD 的简化版，不是真正的语音/非语音分类模型）：
    只判断"这段有没有明显高于background的能量"，不判断内容是不是真的在说话。
    用来给还在模拟的语气词/重复/口吃信号加一个门槛——至少不在明显静音时触发。
    """
    if len(segment) < sample_rate // 10:
        return False
    rms_dbfs = _dbfs(float(np.sqrt(np.mean(segment.astype(np.float64) ** 2) + 1e-12)))
    return bool(rms_dbfs > -40)


def encode_wav(samples: np.ndarray, path: Path, sample_rate: int = ANALYSIS_SAMPLE_RATE) -> None:
    """把 float32 [-1,1] PCM 写成标准 WAV 文件，用标准库 wave，不引入额外依赖。"""
    import wave as wave_module

    clipped = np.clip(samples, -1.0, 1.0)
    int16 = (clipped * 32767.0).astype(np.int16)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave_module.open(str(path), "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(sample_rate)
        f.writeframes(int16.tobytes())


def _stft(samples: np.ndarray, frame_len: int, hop: int) -> tuple[np.ndarray, np.ndarray]:
    """手写 STFT（避免引入 scipy/librosa 依赖）。返回 (复数频谱矩阵 [frames, bins], 汉宁窗)。"""
    window = np.hanning(frame_len).astype(np.float32)
    if len(samples) < frame_len:
        n_frames = 1
    else:
        n_frames = 1 + -(-(len(samples) - frame_len) // hop)  # 向上取整，确保覆盖到信号末尾
    padded_len = (n_frames - 1) * hop + frame_len
    padded = np.zeros(padded_len, dtype=np.float32)
    padded[: len(samples)] = samples
    frames = np.lib.stride_tricks.as_strided(
        padded, shape=(n_frames, frame_len), strides=(padded.strides[0] * hop, padded.strides[0])
    )
    spectrum = np.fft.rfft(frames * window, axis=1)
    return spectrum, window


def _istft(spectrum: np.ndarray, window: np.ndarray, hop: int, out_len: int) -> np.ndarray:
    """对应的手写 ISTFT，重叠相加 + 窗函数能量归一化。"""
    frame_len = len(window)
    n_frames = spectrum.shape[0]
    padded_len = (n_frames - 1) * hop + frame_len
    out = np.zeros(padded_len, dtype=np.float64)
    norm = np.zeros(padded_len, dtype=np.float64)

    frames_time = np.fft.irfft(spectrum, n=frame_len, axis=1) * window
    for i in range(n_frames):
        start = i * hop
        out[start : start + frame_len] += frames_time[i]
        norm[start : start + frame_len] += window**2

    norm[norm < 1e-8] = 1e-8
    out = out / norm
    return out[:out_len].astype(np.float32)


def spectral_subtract_denoise(
    samples: np.ndarray,
    sample_rate: int = ANALYSIS_SAMPLE_RATE,
    over_subtraction: float = 1.8,
    spectral_floor: float = 0.05,
) -> np.ndarray:
    """
    经典谱减法降噪（Boll, 1979 的简化版），真实信号处理，不是神经网络降噪：
      1. 用能量最低的若干帧估计噪声的平均幅度谱
      2. 每帧幅度谱减去噪声幅度谱（乘以过减因子），相位保持不变
      3. 减完的幅度谱设一个下限（谱底），避免出现负值/音乐噪声
      4. 逆变换、重叠相加拼回时域信号

    效果比不上神经网络降噪器，但降噪量和保留度都是真实计算出来的，不是编的数字。
    """
    if len(samples) < sample_rate // 2:
        return samples.copy()

    frame_len = 1024
    hop = 256
    spectrum, window = _stft(samples, frame_len, hop)
    magnitude = np.abs(spectrum)
    phase = np.angle(spectrum)

    frame_energy = np.mean(magnitude**2, axis=1)
    noise_frame_count = max(1, int(len(frame_energy) * 0.2))
    noise_frame_idx = np.argsort(frame_energy)[:noise_frame_count]
    noise_profile = np.mean(magnitude[noise_frame_idx], axis=0)  # [bins]

    subtracted = magnitude - over_subtraction * noise_profile[np.newaxis, :]
    floor = spectral_floor * magnitude
    cleaned_magnitude = np.maximum(subtracted, floor)

    cleaned_spectrum = cleaned_magnitude * np.exp(1j * phase)
    return _istft(cleaned_spectrum, window, hop, out_len=len(samples))


@dataclass
class VoiceProtectionResult:
    noise_reduction_db: float
    timbre_similarity: float
    spectral_centroid_shift_pct: float  # 对外仍叫 pitch_deviation，见下方说明
    dynamic_range_retained: float
    artifact_score: float


def compute_voice_protection(original: np.ndarray, denoised: np.ndarray, sample_rate: int) -> VoiceProtectionResult:
    """
    比较降噪前后两段信号，算出真实的（非随机的）语音保护指标：

      - noise_reduction_db：安静帧（噪声段）能量下降了多少 dB
      - timbre_similarity：安静帧之外（有声段）的平均幅度谱做余弦相似度——
        真正的音色相似度需要声纹模型，这里是频谱形状相似度的近似
      - "pitch_deviation" 对外字段：这里实际算的是频谱质心（spectral centroid）偏移百分比，
        不是逐帧基频跟踪，命名是为了对齐前端已有字段，请勿理解成真正的音高检测
      - dynamic_range_retained：(峰值-噪声底) 的比值，降噪后 / 降噪前
      - artifact_score：安静帧频谱在降噪后不该被大幅改动，如果改动很大说明引入了伪影
    """
    frame_len = 1024
    hop = 512

    # _frame_rms does the same computation as the old per-frame Python loop here, but vectorized
    # (one strided array + one numpy reduction instead of one numpy call per frame) — on a
    # several-minute recording that's thousands of frames turned into a single vector op.
    def frame_rms(sig: np.ndarray) -> np.ndarray:
        return _frame_rms(sig, frame_len, hop)

    rms_before = frame_rms(original)
    threshold = np.percentile(rms_before, 20) * 3
    quiet_idx = np.where(rms_before <= np.percentile(rms_before, 20))[0]
    loud_idx = np.where(rms_before > threshold)[0]
    if len(quiet_idx) == 0:
        quiet_idx = np.array([0])
    if len(loud_idx) == 0:
        loud_idx = np.arange(len(rms_before))

    rms_after = frame_rms(denoised)
    noise_before_db = _dbfs(float(np.mean(rms_before[quiet_idx])))
    quiet_idx_after = quiet_idx[quiet_idx < len(rms_after)]
    if len(quiet_idx_after) == 0:
        quiet_idx_after = np.array([0])
    noise_after_db = _dbfs(float(np.mean(rms_after[quiet_idx_after])))
    noise_reduction_db = max(0.0, noise_before_db - noise_after_db)

    window = _hanning(frame_len)

    def spectrum_of(sig: np.ndarray, idx: np.ndarray) -> np.ndarray:
        specs = []
        for i in idx:
            start = i * hop
            frame = sig[start : start + frame_len]
            if len(frame) < frame_len:
                continue
            specs.append(np.abs(np.fft.rfft(frame * window)))
        if not specs:
            return np.zeros(frame_len // 2 + 1)
        return np.mean(specs, axis=0)

    spec_before_loud = spectrum_of(original, loud_idx)
    loud_idx_after = loud_idx[loud_idx < len(denoised) // hop] if len(denoised) // hop > 0 else np.array([], dtype=int)
    spec_after_loud = spectrum_of(denoised, loud_idx_after if len(loud_idx_after) > 0 else loud_idx)
    dot = float(np.dot(spec_before_loud, spec_after_loud))
    norm = float(np.linalg.norm(spec_before_loud) * np.linalg.norm(spec_after_loud)) + 1e-9
    timbre_similarity = max(0.0, min(1.0, dot / norm))

    freqs = np.fft.rfftfreq(frame_len, d=1 / sample_rate)

    def centroid(spec: np.ndarray) -> float:
        total = float(np.sum(spec)) + 1e-9
        return float(np.sum(freqs * spec) / total)

    c_before = centroid(spec_before_loud)
    c_after = centroid(spec_after_loud)
    centroid_shift_pct = abs(c_after - c_before) / max(c_before, 1e-6) * 100

    peak_before = float(np.max(np.abs(original))) + 1e-9
    peak_after = float(np.max(np.abs(denoised))) + 1e-9
    dr_before = _dbfs(peak_before) - noise_before_db
    dr_after = _dbfs(peak_after) - noise_after_db
    dynamic_range_retained = max(0.0, min(1.0, dr_after / max(dr_before, 1e-6)))

    spec_before_quiet = spectrum_of(original, quiet_idx)
    quiet_idx_after2 = quiet_idx[quiet_idx < len(denoised) // hop] if len(denoised) // hop > 0 else np.array([], dtype=int)
    spec_after_quiet = spectrum_of(denoised, quiet_idx_after2 if len(quiet_idx_after2) > 0 else quiet_idx)
    quiet_diff = float(np.mean(np.abs(spec_after_quiet - spec_before_quiet)))
    quiet_scale = float(np.mean(spec_before_quiet)) + 1e-6
    artifact_score = max(0.0, min(1.0, quiet_diff / quiet_scale / 10))  # /10 是经验缩放，让典型值落在 0~0.2

    return VoiceProtectionResult(
        noise_reduction_db=round(noise_reduction_db, 1),
        timbre_similarity=round(timbre_similarity, 3),
        spectral_centroid_shift_pct=round(centroid_shift_pct, 2),
        dynamic_range_retained=round(dynamic_range_retained, 2),
        artifact_score=round(artifact_score, 3),
    )


def segment_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """
    两段音频的频谱形状余弦相似度——不比较文字内容（做不到，没有 ASR），
    比较"听起来像不像同一个发音"：重复的短语/口吃的音节在声学上会显示出
    相近的共振峰结构，这是可以不靠语义理解就检测到的真实信号。
    """
    if len(a) == 0 or len(b) == 0:
        return 0.0
    n = max(len(a), len(b))
    pad_a = np.zeros(n, dtype=np.float32)
    pad_b = np.zeros(n, dtype=np.float32)
    pad_a[: len(a)] = a
    pad_b[: len(b)] = b
    # find_best_self_similarity calls this dozens of times per candidate with the same n
    # (target/candidate length never changes across one sliding search), so cache the window.
    window = _hanning(n)
    spec_a = np.abs(np.fft.rfft(pad_a * window))
    spec_b = np.abs(np.fft.rfft(pad_b * window))
    dot = float(np.dot(spec_a, spec_b))
    norm = float(np.linalg.norm(spec_a) * np.linalg.norm(spec_b)) + 1e-9
    return max(0.0, min(1.0, dot / norm))


def find_best_self_similarity(
    samples: np.ndarray,
    sample_rate: int,
    target_start_ms: int,
    target_end_ms: int,
    lookback_ms: int,
    step_ms: int = 80,
    min_gap_ms: int = 60,
) -> tuple[float, int] | None:
    """
    在目标片段之前的 lookback_ms 窗口内，滑动寻找声学上最相似的一段。
    返回 (最高相似度, 找到位置距目标开头多少毫秒之前)；目标片段越界/太短则返回 None。

    这不是语义查重（不知道说的是不是同一句话），只是"这段声音的频谱形状此前有没有
    出现过一次很像的"——用来给模拟生成的 repetition/stutter 信号做一次真实的声学复核，
    而不是原样相信随机生成的置信度。
    """
    target_start = int(target_start_ms / 1000 * sample_rate)
    target_end = int(target_end_ms / 1000 * sample_rate)
    if target_end <= target_start or target_end > len(samples) or target_start < 0:
        return None

    target = samples[target_start:target_end]
    seg_len = len(target)
    if seg_len < sample_rate // 50:  # 太短（<20ms）没法算出有意义的频谱
        return None

    seg_duration_ms = target_end_ms - target_start_ms
    # 候选窗口必须在目标片段结束前完全结束，否则会跟目标自己重叠，
    # 那样算出来的"相似度"只是在跟自己比较，没有意义
    search_start_ms = max(0, target_start_ms - lookback_ms)
    search_end_ms = target_start_ms - seg_duration_ms - min_gap_ms
    if search_end_ms < search_start_ms:
        return None

    best_similarity = 0.0
    best_offset_ms = 0
    cursor_ms = search_start_ms
    found_any = False
    while cursor_ms <= search_end_ms:
        cand_start = int(cursor_ms / 1000 * sample_rate)
        cand_end = cand_start + seg_len
        if cand_end <= len(samples) and cand_start >= 0:
            candidate = samples[cand_start:cand_end]
            sim = segment_similarity(target, candidate)
            found_any = True
            if sim > best_similarity:
                best_similarity = sim
                best_offset_ms = target_start_ms - cursor_ms
        cursor_ms += step_ms

    if not found_any:
        return None
    return best_similarity, best_offset_ms


@dataclass
class QualityMetrics:
    duration_ms: int
    integrated_lufs: float
    true_peak_dbtp: float
    noise_floor_dbfs: float
    estimated_snr_db: float
    clipping_ratio: float
    hum_probability: float
    reverb_score: float
    speech_coverage: float
    silence_coverage: float
    noise_types: list[str]
    quality_grade: str
    grade_reason: str


def _dbfs(x: float) -> float:
    return 20 * np.log10(max(x, 1e-9))


def _frame_rms(samples: np.ndarray, frame_len: int, hop: int) -> np.ndarray:
    if len(samples) < frame_len:
        return np.array([np.sqrt(np.mean(samples**2) + 1e-12)])
    n_frames = 1 + (len(samples) - frame_len) // hop
    frames = np.lib.stride_tricks.as_strided(
        samples,
        shape=(n_frames, frame_len),
        strides=(samples.strides[0] * hop, samples.strides[0]),
    )
    return np.sqrt(np.mean(frames.astype(np.float64) ** 2, axis=1) + 1e-12)


def _hum_probability(samples: np.ndarray, sample_rate: int) -> float:
    """50/60Hz 及其二次谐波附近的能量占总能量的比例——电流声的典型频谱特征。"""
    if len(samples) < sample_rate:  # 不到 1 秒，频率分辨率不够，不判断
        return 0.0
    spectrum = np.abs(np.fft.rfft(samples * np.hanning(len(samples))))
    freqs = np.fft.rfftfreq(len(samples), d=1 / sample_rate)
    total_energy = float(np.sum(spectrum**2)) + 1e-12

    hum_energy = 0.0
    for base in (50.0, 60.0):
        for harmonic in (1, 2):
            f = base * harmonic
            band = (freqs > f - 2) & (freqs < f + 2)
            hum_energy = max(hum_energy, float(np.sum(spectrum[band] ** 2)))
    return float(min(1.0, hum_energy / total_energy * 40))  # 40 是经验放大系数，让典型电流声落在 0.3~0.8


def _reverb_proxy(samples: np.ndarray, sample_rate: int) -> float:
    """
    频谱平坦度（spectral flatness）作代理：混响会让频谱在时间上更平滑、能量分布更均匀。
    不是真正的 RT60，只是一个方向大致对的数字。
    """
    frame_len = min(len(samples), sample_rate)
    if frame_len < 512:
        return 0.0
    spectrum = np.abs(np.fft.rfft(samples[:frame_len] * np.hanning(frame_len))) + 1e-9
    geo_mean = np.exp(np.mean(np.log(spectrum)))
    arith_mean = np.mean(spectrum)
    flatness = float(geo_mean / arith_mean)  # 已经天然落在 0..1（几何均值 <= 算术均值）
    return flatness


def analyze(path: Path, sample_rate: int = ANALYSIS_SAMPLE_RATE) -> QualityMetrics:
    samples = decode_pcm(path, sample_rate)
    return analyze_samples(samples, sample_rate)


def analyze_samples(samples: np.ndarray, sample_rate: int = ANALYSIS_SAMPLE_RATE) -> QualityMetrics:
    """
    analyze() 的纯计算部分，接受已经解码好的样本数组。拆出来是为了调用方（比如
    on_capture_stop）能先解码一次拿到真实采样数/时长用于别处（如 AudioVersion 的元数据），
    再把同一份样本传进来算质量指标，不用重复解码一遍。
    """
    duration_ms = int(len(samples) / sample_rate * 1000)

    peak = float(np.max(np.abs(samples))) if len(samples) else 0.0
    frame_rms = _frame_rms(samples, frame_len=sample_rate // 20, hop=sample_rate // 20)  # 50ms 帧

    overall_rms = float(np.sqrt(np.mean(samples.astype(np.float64) ** 2) + 1e-12))
    noise_floor_rms = float(np.percentile(frame_rms, 10))
    speech_threshold = max(noise_floor_rms * 3, 1e-4)
    speech_coverage = float(np.mean(frame_rms > speech_threshold))
    silence_coverage = 1.0 - speech_coverage

    speech_rms = float(np.mean(frame_rms[frame_rms > speech_threshold])) if speech_coverage > 0 else overall_rms
    snr_db = _dbfs(speech_rms) - _dbfs(max(noise_floor_rms, 1e-6))

    clipping_ratio = clip_ratio(samples)
    hum = _hum_probability(samples, sample_rate)
    reverb = _reverb_proxy(samples, sample_rate)

    noise_types: list[str] = []
    if hum > 0.3:
        noise_types.append("hum")
    if clipping_ratio > 0.001:
        noise_types.append("clipping")
    if reverb > 0.65:
        noise_types.append("reverb")

    if clipping_ratio > 0.01 or snr_db < 6:
        grade, reason = "POOR", "削波比例过高或信噪比过低"
    elif snr_db < 12 or hum > 0.5:
        grade, reason = "FAIR", "信噪比偏低或检测到明显电流声"
    elif snr_db < 20:
        grade, reason = "GOOD", "信噪比与响度基本达标"
    else:
        grade, reason = "EXCELLENT", "信噪比与响度表现优秀"

    return QualityMetrics(
        duration_ms=duration_ms,
        integrated_lufs=round(_dbfs(overall_rms) - 3, 1),  # -3 是粗略的 RMS→LUFS 偏移经验值，非标准算法
        true_peak_dbtp=round(_dbfs(peak), 1),
        noise_floor_dbfs=round(_dbfs(noise_floor_rms), 1),
        estimated_snr_db=round(float(snr_db), 1),
        clipping_ratio=round(clipping_ratio, 4),
        hum_probability=round(hum, 2),
        reverb_score=round(reverb, 2),
        speech_coverage=round(speech_coverage, 2),
        silence_coverage=round(silence_coverage, 2),
        noise_types=noise_types,
        quality_grade=grade,
        grade_reason=reason,
    )


def compute_envelope(path: Path, buckets: int = 200, sample_rate: int = ANALYSIS_SAMPLE_RATE) -> list[float]:
    """把整段录音降采样成 N 个柱子的振幅包络，供前端画波形用。"""
    samples = decode_pcm(path, sample_rate)
    if len(samples) == 0:
        return [0.0] * buckets

    abs_samples = np.abs(samples)
    bucket_size = max(1, len(abs_samples) // buckets)
    trimmed = abs_samples[: bucket_size * buckets]
    if len(trimmed) == 0:
        return [0.0] * buckets
    reshaped = trimmed.reshape(-1, bucket_size)
    peaks = reshaped.max(axis=1)

    overall_peak = float(np.max(peaks)) if len(peaks) else 1.0
    if overall_peak < 1e-6:
        return [0.06] * len(peaks) + [0.0] * (buckets - len(peaks))
    normalized = np.clip(peaks / overall_peak, 0.06, 1.0)
    out = normalized.tolist()
    if len(out) < buckets:
        out += [0.0] * (buckets - len(out))
    return [round(float(v), 3) for v in out[:buckets]]


# ─────────────── 去削波（破音修复） ───────────────

@dataclass
class DeclipResult:
    samples: np.ndarray
    regions: int          # 修复的削波段数
    clipped_samples: int  # 被重建的采样点数
    gain_db: float        # 为留出余量做的整体增益（≤ 0）


def clip_ratio(samples: np.ndarray) -> float:
    """
    削波采样占比。贴满幅（>0.99）的一定算；另外削波常发生在满幅以下（设备限幅、编码后的
    上限），所以样本在某个上限处明显堆积时，也把贴近该上限的采样计入。
    """
    if len(samples) == 0:
        return 0.0
    magnitude = np.abs(samples)
    full_scale = float(np.mean(magnitude > 0.99))
    if has_clipping_pileup(samples) and float(magnitude.max()) >= 0.3:
        return max(full_scale, float(np.mean(magnitude >= 0.995 * magnitude.max())))
    return full_scale


def has_clipping_pileup(x: np.ndarray) -> bool:
    """
    削波的特征是大量采样堆在同一个上限：贴近最大幅度（最高 0.5%）的采样数，远多于
    紧挨着的下一档（0.5%~1.5%，按宽度折算）。自然波峰只会短暂碰到最大值，不会堆积。
    """
    magnitude = np.abs(x)
    peak = float(magnitude.max()) if len(magnitude) else 0.0
    if peak <= 0:
        return False
    top = int(np.count_nonzero(magnitude >= 0.995 * peak))
    below = int(np.count_nonzero((magnitude >= 0.985 * peak) & (magnitude < 0.995 * peak))) / 2
    return top >= 20 and top > 3 * below


def declip(samples: np.ndarray, *, clip_level: float | None = None, context: int = 8,
           max_region: int = 400, headroom: float = 0.97) -> DeclipResult:
    """
    重建被削平的波峰：找出幅度贴着削波电平、连续同号的采样段，用段两侧未削波的
    采样拟合三次多项式，在段内插值出原本的峰形（不低于削波电平、不超过其两倍）。
    重建后峰值可能超过满幅，最后整体降一点增益留出余量。没有削波段时原样返回。

    clip_level 缺省为信号最大幅度的 99.5%：浏览器/编码器削波不一定正好在 1.0。
    """
    x = np.asarray(samples, dtype=np.float64)
    if len(x) == 0:
        return DeclipResult(samples.astype(np.float32), 0, 0, 0.0)
    peak = float(np.max(np.abs(x)))
    level = clip_level if clip_level is not None else peak * 0.995
    if peak < 0.3 or level <= 0:  # 太安静的录音不可能是削波
        return DeclipResult(samples.astype(np.float32), 0, 0, 0.0)
    if clip_level is None and not has_clipping_pileup(x):
        return DeclipResult(samples.astype(np.float32), 0, 0, 0.0)
    clipped = np.abs(x) >= level
    # 找连续段，且段内同号（正负削波分开处理）
    edges = np.diff(np.concatenate(([0], clipped.astype(np.int8), [0])))
    starts, ends = np.flatnonzero(edges == 1), np.flatnonzero(edges == -1)
    out = x.copy()
    regions = 0
    rebuilt = 0
    for start, end in zip(starts, ends):
        length = end - start
        # 单个采样点碰到峰值不算削波（正常波峰也会触到最大值）；过长的段是大面积失真，插值不可信
        if length < 2 or length > max_region:
            continue
        sign = np.sign(x[start])
        if np.any(np.sign(x[start:end]) != sign):
            continue
        left = np.arange(max(0, start - context), start)
        right = np.arange(end, min(len(x), end + context))
        left = left[~clipped[left]]
        right = right[~clipped[right]]
        if len(left) < 2 or len(right) < 2:
            continue
        positions = np.concatenate((left, right)).astype(np.float64)
        coefficients = np.polyfit(positions - start, x[positions.astype(int)], 3)
        fitted = np.polyval(coefficients, np.arange(length, dtype=np.float64))
        magnitude = np.clip(np.abs(fitted), level, 2.0 * level)
        out[start:end] = sign * magnitude
        regions += 1
        rebuilt += int(length)
    if regions == 0:
        return DeclipResult(samples.astype(np.float32), 0, 0, 0.0)
    new_peak = float(np.max(np.abs(out)))
    gain = min(1.0, headroom / new_peak) if new_peak > 0 else 1.0
    out *= gain
    return DeclipResult(out.astype(np.float32), regions, rebuilt, round(20 * np.log10(gain), 2) if gain < 1 else 0.0)
