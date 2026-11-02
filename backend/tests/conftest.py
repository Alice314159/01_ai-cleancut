"""
共享 fixture。核心思路：每个测试都要有自己独立的 SQLite 数据库和音频存储目录，
不能碰共享的 data/ 目录，否则测试之间、测试和手动调试之间会互相污染。

app.db.SessionLocal / app.db.engine / app.config.AUDIO_DIR 都在对应模块里用
属性访问的方式被其它模块引用（app.services.stub_pipeline 用 db_module.SessionLocal()，
app.services.storage 用 config.AUDIO_DIR），所以在这里 monkeypatch 模块属性
就能让全部调用方都感知到，不用逐个 mock。
"""
import subprocess
from pathlib import Path

import numpy as np
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import config as config_module
from app import db as db_module
from app.db import Base
from app.services import transcription as transcription_module

ANALYSIS_SAMPLE_RATE = 16000


@pytest.fixture()
def isolated_backend(tmp_path, monkeypatch):
    """
    把 app.db.engine / app.db.SessionLocal 换成指向临时 sqlite 文件的新实例，
    把 app.config.AUDIO_DIR 换成临时目录，建表，测试结束后临时目录自动清理
    （pytest 的 tmp_path 机制），不需要手动 teardown。
    """
    db_path = tmp_path / "test.db"
    test_engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    test_session_local = sessionmaker(bind=test_engine, autoflush=False, autocommit=False)

    monkeypatch.setattr(db_module, "engine", test_engine)
    monkeypatch.setattr(db_module, "SessionLocal", test_session_local)

    audio_dir = tmp_path / "audio"
    audio_dir.mkdir()
    monkeypatch.setattr(config_module, "AUDIO_DIR", audio_dir)

    # on_capture_stop 终态化成功后会自动排队一次真实转写（见 stub_pipeline.py）。
    # 这里的机器已经装了 faster-whisper（本次开发环境的一部分），如果不关掉，几乎
    # 每个跑到终态化的测试都会在后台真的加载模型做推理——又慢，又会在测试的临时
    # 数据库已经 drop 之后继续写入，产生跨测试的竞态。默认关闭，需要真实验证自动
    # 转写触发的测试自己再打开。
    monkeypatch.setattr(transcription_module, "dependency_available", lambda: False)

    from app import models  # noqa: F401  确保所有模型都注册到 Base.metadata 上

    Base.metadata.create_all(bind=test_engine)

    yield test_session_local

    Base.metadata.drop_all(bind=test_engine)


def _encode_webm(samples: np.ndarray, path: Path, sample_rate: int = ANALYSIS_SAMPLE_RATE) -> None:
    int16 = (np.clip(samples, -1, 1) * 32767).astype(np.int16)
    raw_path = path.with_suffix(".raw")
    int16.tofile(raw_path)
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "s16le", "-ar", str(sample_rate), "-ac", "1",
         "-i", str(raw_path), str(path)],
        check=True,
    )
    raw_path.unlink()


def _make_tone(f0: float, dur_s: float, sample_rate: int = ANALYSIS_SAMPLE_RATE, amp: float = 0.3) -> np.ndarray:
    t = np.linspace(0, dur_s, int(sample_rate * dur_s), endpoint=False)
    sig = sum(amp / h * np.sin(2 * np.pi * f0 * h * t) for h in (1, 2, 3))
    return sig.astype(np.float32)


@pytest.fixture()
def clean_audio_samples() -> np.ndarray:
    """一段干净的、带幅度起伏的合成语音替代品：谐波音调 + 轻微噪声。"""
    t = np.linspace(0, 3, ANALYSIS_SAMPLE_RATE * 3, endpoint=False)
    tone = 0.3 * np.sin(2 * np.pi * 180 * t) * (0.5 + 0.5 * np.sin(2 * np.pi * 0.5 * t))
    noise = np.random.default_rng(0).normal(0, 0.005, len(t))
    return np.clip(tone + noise, -1, 1).astype(np.float32)


@pytest.fixture()
def noisy_audio_samples() -> np.ndarray:
    """带 60Hz 电流声 + 削波 + 白噪声的合成信号，用于验证质量分析能正确识别出"差"。"""
    t = np.linspace(0, 3, ANALYSIS_SAMPLE_RATE * 3, endpoint=False)
    tone = 0.2 * np.sin(2 * np.pi * 300 * t)
    hum = 0.15 * np.sin(2 * np.pi * 60 * t)
    noise = np.random.default_rng(1).normal(0, 0.08, len(t))
    sig = np.clip((tone + hum + noise) * 3, -1, 1)
    return sig.astype(np.float32)


@pytest.fixture()
def silent_audio_samples() -> np.ndarray:
    return np.zeros(ANALYSIS_SAMPLE_RATE * 2, dtype=np.float32)


@pytest.fixture()
def clean_webm_file(tmp_path, clean_audio_samples) -> Path:
    path = tmp_path / "clean.webm"
    _encode_webm(clean_audio_samples, path)
    return path


@pytest.fixture()
def noisy_webm_file(tmp_path, noisy_audio_samples) -> Path:
    path = tmp_path / "noisy.webm"
    _encode_webm(noisy_audio_samples, path)
    return path


@pytest.fixture()
def repeat_timeline():
    """
    构造一段已知重复位置的音频，返回 (样本数组, 时间标记字典)。
    时间标记用来在测试里断言"找到的重复偏移量应该正好等于构造时设定的间隔"，
    而不是只笼统地断言"相似度较高"。
    """
    bg_a = _make_tone(220, 2.0)
    phrase_x_1 = _make_tone(180, 0.3)
    bg_b = _make_tone(250, 0.7)
    phrase_x_2 = _make_tone(180, 0.3)  # 与 phrase_x_1 相同音高——真实重复
    bg_c = _make_tone(260, 1.7)
    phrase_y = _make_tone(310, 0.3)  # 孤立出现，附近没有相似片段

    segments = [bg_a, phrase_x_1, bg_b, phrase_x_2, bg_c, phrase_y]
    full = np.concatenate(segments).astype(np.float32)

    marks = {}
    t = 0
    for name, seg in zip(
        ["bg_a", "phrase_x_1", "bg_b", "phrase_x_2", "bg_c", "phrase_y"], segments
    ):
        marks[name] = (t / ANALYSIS_SAMPLE_RATE * 1000, (t + len(seg)) / ANALYSIS_SAMPLE_RATE * 1000)
        t += len(seg)

    return full, marks
