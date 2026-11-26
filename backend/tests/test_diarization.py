"""
说话人区分（sherpa-onnx）。

前半部分不需要模型：分段整理、按时间取说话人、类型修正、字幕/文本导出、接口和流水线的降级路径。
后半部分用真实模型和 flite 合成的不同声音，没装 sherpa-onnx 或没下载模型时跳过：
    pip install -r requirements-diarization.txt && python scripts/prepare_diarization.py
模型不在默认目录时，用 CLEANCUT_TEST_DIARIZATION_DIR 指过去。
"""
import asyncio
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace as NS

import numpy as np
import pytest

from app import config
from app.models import AudioQualityReport, TranscriptToken, TranscriptionJob
from app.services import diarization, stub_pipeline, transcript_export
from tests.test_content_type import SR, dialogue, needs_flite, voice_lines
from tests.test_regressions import prepared_session
from tests.test_rendering import client, recording  # noqa: F401  复用 fixture

REAL_MODEL_DIR = Path(os.environ.get("CLEANCUT_TEST_DIARIZATION_DIR", str(config.DIARIZATION_MODEL_DIR)))
needs_models = pytest.mark.skipif(
    not (diarization.package_available() and diarization.models_present(REAL_MODEL_DIR)),
    reason="未安装 sherpa-onnx 或未下载说话人区分模型",
)


def seg(start, end, speaker):
    return diarization.Segment(start, end, speaker)


# ─────────────── 不需要模型 ───────────────

def test_normalize_renumbers_by_first_appearance_and_merges_short_gaps():
    raw = [seg(5000, 6000, 3), seg(0, 2000, 7), seg(2100, 4000, 7), seg(6200, 7000, 3), seg(8000, 9000, 7)]
    assert diarization.normalize(raw) == [seg(0, 4000, 0), seg(5000, 7000, 1), seg(8000, 9000, 0)]


def test_speaker_at_uses_largest_overlap_and_nearby_gap():
    segments = [[0, 1000, 0], [1000, 3000, 1], [5400, 6000, 0]]
    assert diarization.speaker_at(segments, 800, 1600) == 1       # 重叠更多的一方
    assert diarization.speaker_at(segments, 3200, 3400) == 1      # 落在缝里，最近一段在 1 秒内
    assert diarization.speaker_at(segments, 4100, 4300) is None   # 离两边都超过 1 秒
    assert diarization.speaker_at(None, 0, 100) is None


def test_speaker_names():
    assert [diarization.speaker_name(i) for i in (0, 1, 25, 26)] == ["说话人 A", "说话人 B", "说话人 Z", "说话人 AA"]


def test_summarize_ignores_brief_blips():
    count = diarization.summarize([seg(0, 20000, 0), seg(20000, 40000, 1), seg(40000, 41000, 2)])
    assert count.speakers == 2 and count.analyzed_seconds == 41.0 and len(count.shares) == 3
    assert diarization.summarize([]).speakers == 0


@pytest.mark.parametrize("before,speakers,after", [
    ("speech", 2, "multi_speaker_speech"),
    ("speech", 1, "single_speaker_speech"),
    ("multi_speaker_speech", 1, "single_speaker_speech"),
    ("speech_with_music", 2, "speech_with_music"),   # 不是纯人声，不改
    ("speech", 0, "speech"),
])
def test_refine_content_type(before, speakers, after):
    report = NS(content_type=before, content_type_confidence=0.6, content_type_overridden_by_user=False, speaker_count=None)
    diarization.refine_content_type(report, diarization.SpeakerCount(speakers, (), 10.0))
    assert report.content_type == after and report.speaker_count == speakers


def test_refine_never_overrides_user_choice():
    report = NS(content_type="speech", content_type_confidence=1.0, content_type_overridden_by_user=True, speaker_count=None)
    diarization.refine_content_type(report, diarization.SpeakerCount(2, (), 10.0))
    assert report.content_type == "speech" and report.speaker_count == 2


def test_excerpts_keep_short_audio_and_sample_long_audio():
    short = np.zeros(SR * 60, dtype=np.float32)
    assert diarization.excerpts(short, SR) is short
    long = np.zeros(SR * 3600, dtype=np.float32)
    assert diarization.excerpts(long, SR).size < SR * 125


def words(*items):
    return [NS(text=t, start_ms=s, end_ms=e, speaker=p) for t, s, e, p in items]


def test_subtitles_break_and_label_on_speaker_change():
    tokens = words(("你好", 0, 400, 0), ("，", 400, 450, 0), ("欢迎", 500, 900, 0),
                   ("谢谢", 1000, 1400, 1), ("邀请", 1400, 1800, 1))
    assert list(transcript_export.subtitle_cues(tokens)) == [
        (0, 900, "说话人 A：你好，欢迎"), (1000, 1800, "说话人 B：谢谢邀请")]
    assert transcript_export.turns(tokens) == "说话人 A：你好，欢迎\n\n说话人 B：谢谢邀请\n"


def test_subtitles_without_speakers_unchanged():
    tokens = [NS(text=t, start_ms=s, end_ms=e) for t, s, e in (("Hello", 0, 400), (" world.", 400, 900))]
    assert list(transcript_export.subtitle_cues(tokens)) == [(0, 900, "Hello world.")]


def test_status_reports_missing_pieces(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DIARIZATION_MODEL_DIR", tmp_path)
    monkeypatch.setattr(diarization, "package_available", lambda: True)
    assert diarization.status() == "missing_models" and not diarization.available()
    monkeypatch.setattr(diarization, "package_available", lambda: False)
    assert diarization.status() == "missing_package"
    with pytest.raises(RuntimeError):
        diarization.diarize(np.zeros(SR * 2, dtype=np.float32))


def add_report(factory, session_id, **fields):
    values = dict(session_id=session_id, quality_grade="GOOD", grade_reason="", integrated_lufs=-16, true_peak_dbtp=-1,
                  noise_floor_dbfs=-60, estimated_snr_db=30, clipping_ratio=0, hum_probability=0, reverb_score=0,
                  speech_coverage=0.8, silence_coverage=0.2, noise_types=[], content_type="multi_speaker_speech",
                  content_type_confidence=0.85)
    values.update(fields)
    with factory() as db:
        db.add(AudioQualityReport(**values))
        db.commit()


def add_transcript(factory, session_id, items):
    with factory() as db:
        job = TranscriptionJob(session_id=session_id, source_version_id="av_x", model="base", status="COMPLETED", progress=1)
        db.add(job)
        db.flush()
        for seq, (text, start, end) in enumerate(items):
            db.add(TranscriptToken(session_id=session_id, job_id=job.id, seq=seq, text=text, start_ms=start, end_ms=end, state="committed"))
        db.commit()
        return job.id


TRANSCRIPT = [("你好", 0, 400), ("欢迎", 500, 900), ("谢谢", 1600, 2000), ("邀请", 2000, 2400)]


def test_transcript_api_and_exports_carry_speakers(client, recording, isolated_backend):
    session_id = recording[0]
    add_report(isolated_backend, session_id, speaker_analysis_status="done", speaker_count=2,
               speaker_segments=[[0, 1200, 0], [1500, 2600, 1]])
    job_id = add_transcript(isolated_backend, session_id, TRANSCRIPT)
    tokens = client.get(f"/api/sessions/{session_id}/transcript").json()
    assert [t["speaker"] for t in tokens] == [0, 0, 1, 1]
    quality = client.get(f"/api/sessions/{session_id}/quality").json()
    assert quality["speakerAnalysisStatus"] == "done" and quality["speakerCount"] == 2
    srt = client.get(f"/api/sessions/{session_id}/transcript/export", params={"transcriptionJobId": job_id, "format": "srt"}).text
    assert "说话人 A：你好欢迎" in srt and "说话人 B：谢谢邀请" in srt
    txt = client.get(f"/api/sessions/{session_id}/transcript/export", params={"transcriptionJobId": job_id, "format": "txt"}).text
    assert txt == "说话人 A：你好欢迎\n\n说话人 B：谢谢邀请\n"
    # 成片的转写快照也带上说话人，成片字幕同样能标出
    with isolated_backend() as db:
        snapshot = transcript_export.snapshot(db, session_id)
    assert [t["speaker"] for t in snapshot] == [0, 0, 1, 1]


@pytest.mark.parametrize("fields", [
    dict(speaker_analysis_status="done", speaker_count=1, speaker_segments=[[0, 3000, 0]]),   # 单人不标
    dict(speaker_analysis_status="running", speaker_count=None, speaker_segments=None),
    dict(),                                                                                   # 没有模型
])
def test_transcript_has_no_speakers_unless_model_found_several(client, recording, isolated_backend, fields):
    session_id = recording[0]
    add_report(isolated_backend, session_id, **fields)
    job_id = add_transcript(isolated_backend, session_id, TRANSCRIPT)
    assert all(t["speaker"] is None for t in client.get(f"/api/sessions/{session_id}/transcript").json())
    txt = client.get(f"/api/sessions/{session_id}/transcript/export", params={"transcriptionJobId": job_id, "format": "txt"}).text
    assert txt == "你好欢迎谢谢邀请\n"


async def test_pipeline_without_models_marks_unavailable(isolated_backend, clean_webm_file):
    session_id = await prepared_session(isolated_backend, clean_webm_file)
    with isolated_backend() as db:
        report = db.get(AudioQualityReport, session_id)
        assert report.speaker_analysis_status == "unavailable" and report.speaker_count is None


def test_failed_analysis_is_recorded(isolated_backend, recording, monkeypatch):
    session_id = recording[0]
    add_report(isolated_backend, session_id, speaker_analysis_status="pending")
    monkeypatch.setattr(diarization, "diarize", lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(diarization, "excerpts", lambda samples, sr: samples)
    diarization.analyze_session(session_id)    # 没有 raw.webm，解码失败
    with isolated_backend() as db:
        assert db.get(AudioQualityReport, session_id).speaker_analysis_status == "failed"


def test_recovery_requeues_or_marks_unavailable(isolated_backend, recording, monkeypatch):
    session_id = recording[0]
    add_report(isolated_backend, session_id, speaker_analysis_status="running")
    monkeypatch.setattr(diarization, "available", lambda: False)
    assert diarization.recover_interrupted() == []
    with isolated_backend() as db:
        report = db.get(AudioQualityReport, session_id)
        assert report.speaker_analysis_status == "unavailable"
        report.speaker_analysis_status = "running"
        db.commit()
    monkeypatch.setattr(diarization, "available", lambda: True)
    assert diarization.recover_interrupted() == [session_id]


# ─────────────── 真实模型 ───────────────

@pytest.fixture()
def real_models(monkeypatch):
    monkeypatch.setattr(config, "DIARIZATION_MODEL_DIR", REAL_MODEL_DIR)


def count(samples):
    return diarization.summarize(diarization.normalize(diarization.diarize(samples))).speakers


def three_lines(voice):
    return voice_lines(None, voice, 3)


@needs_models
@needs_flite
@pytest.mark.parametrize("voices,expected", [
    (("awb",), 1), (("slt",), 1),
    (("kal", "slt"), 2),            # 男女
    (("awb", "rms"), 2),            # 同性别、音高接近：音高判断分不出来的那一对
    (("kal", "awb", "rms"), 3),
])
def test_real_model_counts_speakers(real_models, voices, expected):
    gap = np.zeros(SR // 2, dtype=np.float32)
    parts = [part for turn in zip(*(three_lines(v) for v in voices)) for line in turn for part in (line, gap)]
    assert count(np.concatenate(parts)) == expected


@needs_models
@needs_flite
async def test_real_pipeline_refines_similar_voices_and_labels_turns(isolated_backend, real_models, tmp_path):
    audio = dialogue(three_lines("awb"), three_lines("rms"))
    raw = tmp_path / "dialogue.raw"
    (np.clip(audio, -1, 1) * 32767).astype("<i2").tofile(raw)
    path = tmp_path / "dialogue.webm"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "s16le", "-ar", str(SR), "-ac", "1", "-i", str(raw), str(path)], check=True)
    session_id = await prepared_session(isolated_backend, path)
    with isolated_backend() as db:
        report = db.get(AudioQualityReport, session_id)
        assert report.content_type == "speech"            # 信号判断分不出这两个人
        assert report.speaker_analysis_status == "pending"
    await asyncio.gather(*stub_pipeline._speaker_tasks)
    with isolated_backend() as db:
        report = db.get(AudioQualityReport, session_id)
        assert report.speaker_analysis_status == "done"
        assert report.content_type == "multi_speaker_speech" and report.speaker_count == 2
        segments = report.speaker_segments
    # 句子按 awb、rms 交替：每句中点所属的说话人也应交替
    bounds, start = [], 0
    for line in [l for pair in zip(three_lines("awb"), three_lines("rms")) for l in pair]:
        bounds.append((start, start + len(line)))
        start += len(line)
    labels = [diarization.speaker_at(segments, s * 1000 // SR + 500, e * 1000 // SR - 500) for s, e in bounds]
    assert labels == [0, 1, 0, 1, 0, 1]
