"""内容类型估计：音乐 / 噪声 / 静音用合成信号，人声用 ffmpeg 自带的 flite 语音合成（不可用时跳过）。"""
import subprocess

import numpy as np
import pytest

from app.models import AudioQualityReport
from app.services import audio_analysis
from tests.test_regressions import prepared_session

SR = 16000


def flite_available():
    try:
        out = subprocess.run(["ffmpeg", "-hide_banner", "-filters"], capture_output=True, timeout=10).stdout.decode()
    except (OSError, subprocess.TimeoutExpired):
        return False
    return " flite " in out


needs_flite = pytest.mark.skipif(not flite_available(), reason="本机 ffmpeg 未编译 libflite，无法合成人声样本")
TEXT = ("So the question I keep getting is how do you edit a podcast quickly. Honestly the trick is to cut "
        "the pauses, remove the repeats, and keep the energy up. Let me show you a real example.")


def speech(tmp_path, voice="kal"):
    path = tmp_path / f"speech_{voice}.wav"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"flite=text='{TEXT}':voice={voice}",
                    "-ar", str(SR), "-ac", "1", str(path)], check=True)
    return audio_analysis.decode_pcm(path)


def music(seconds=12):
    t = np.arange(SR * seconds) / SR
    out = np.zeros_like(t)
    chords = [[261.6, 329.6, 392.0], [220.0, 261.6, 329.6], [174.6, 220.0, 261.6], [196.0, 246.9, 293.7]]
    rng = np.random.default_rng(0)
    for k in range(seconds // 2):
        s, e = k * 2 * SR, (k + 1) * 2 * SR
        seg = t[s:e] - t[s]
        for f in chords[k % 4]:
            for h, a in ((1, 1), (2, .4), (3, .2)):
                out[s:e] += 0.08 * a * np.sin(2 * np.pi * f * h * seg) * np.exp(-seg * 0.6)
        out[s:e] += 0.12 * np.sin(2 * np.pi * chords[k % 4][0] / 2 * seg)
    for beat in np.arange(0, seconds, 0.5):
        s, n = int(beat * SR), int(0.08 * SR)
        out[s:s + n] += rng.normal(0, 0.25, n) * np.exp(-np.arange(n) / (0.015 * SR))
    return (out * 0.8).astype(np.float32)


def test_music_noise_and_silence():
    assert audio_analysis.classify_content(music()).content_type == "music_dominant"
    noise = np.random.default_rng(1).normal(0, 0.05, SR * 8).astype(np.float32)
    assert audio_analysis.classify_content(noise).content_type == "no_speech"
    quiet = np.random.default_rng(2).normal(0, 0.0005, SR * 8).astype(np.float32)
    result = audio_analysis.classify_content(quiet)
    assert result.content_type == "no_speech" and result.confidence >= 0.8
    assert audio_analysis.classify_content(np.zeros(100, dtype=np.float32)).content_type == "unknown"  # 太短


@needs_flite
@pytest.mark.parametrize("voice", ["kal", "slt"])   # slt 音调很平，曾被误判为“持续音”
def test_speech_voices(tmp_path, voice):
    result = audio_analysis.classify_content(speech(tmp_path, voice))
    assert result.content_type == "speech" and result.confidence >= 0.6


@needs_flite
def test_speech_with_music_and_with_noise(tmp_path):
    voice = speech(tmp_path)
    bed = music(int(len(voice) / SR) + 1)[:len(voice)]
    assert audio_analysis.classify_content(voice + bed * 0.35).content_type == "speech_with_music"
    noisy = voice + np.random.default_rng(3).normal(0, 0.02, len(voice)).astype(np.float32)
    assert audio_analysis.classify_content(noisy).content_type == "speech"   # 底噪不算背景音乐


@needs_flite
def test_long_recording_is_sampled_not_fully_processed(tmp_path):
    long = np.tile(speech(tmp_path), 200)          # 约 40 分钟
    import time
    start = time.monotonic()
    assert audio_analysis.classify_content(long).content_type == "speech"
    assert time.monotonic() - start < 2


@needs_flite
async def test_quality_report_records_content_type(isolated_backend, tmp_path):
    path = tmp_path / "speech.webm"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"flite=text='{TEXT}':voice=kal", str(path)], check=True)
    session_id = await prepared_session(isolated_backend, path)
    with isolated_backend() as db:
        report = db.get(AudioQualityReport, session_id)
        assert report.content_type == "speech" and report.content_type_confidence >= 0.6


def voice_lines(tmp_path, voice, count=2):
    parts = []
    for i in range(count):
        path = tmp_path / f"{voice}_{i}.wav"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                        f"flite=text='This is sentence number {i} from the {voice} voice. We are testing how well the tool "
                        f"can tell different people apart in a recording.':voice={voice}",
                        "-ar", str(SR), "-ac", "1", str(path)], check=True)
        parts.append(audio_analysis.decode_pcm(path))
    return parts


def dialogue(*speakers):
    return np.concatenate([part for turn in zip(*speakers) for part in turn])


@needs_flite
@pytest.mark.parametrize("a,b", [("kal", "slt"), ("slt", "rms"), ("kal", "awb")])
def test_dialogue_with_clearly_different_voices_is_multi_speaker(tmp_path, a, b):
    result = audio_analysis.classify_content(dialogue(voice_lines(tmp_path, a), voice_lines(tmp_path, b)))
    assert result.content_type == "multi_speaker_speech" and result.confidence >= 0.6


@needs_flite
@pytest.mark.parametrize("voice", ["kal", "slt", "awb", "rms"])
def test_single_voice_is_never_called_multi(tmp_path, voice):
    lines = voice_lines(tmp_path, voice, 4)
    assert audio_analysis.classify_content(np.concatenate(lines)).content_type == "speech"


@needs_flite
def test_similar_voices_stay_undetermined_rather_than_single(tmp_path):
    """已知局限：两个音高接近的同性别说话人分不出来，结果保持“人声（未区分人数）”，不冒称单人。"""
    result = audio_analysis.classify_content(dialogue(voice_lines(tmp_path, "awb"), voice_lines(tmp_path, "rms")))
    assert result.content_type == "speech"
