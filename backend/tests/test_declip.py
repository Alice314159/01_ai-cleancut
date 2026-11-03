"""去削波：算法正确性、不误伤干净音频，以及接入音质增强流程。"""
import numpy as np
import pytest

from app.models import AudioQualityReport, AudioVersion, EnhancementJob
from app.services import audio_analysis
from app.services.stub_pipeline import run_enhancement
from tests.test_regressions import prepared_session

SR = 16000


def speech_like(seconds=3.0, seed=0):
    t = np.arange(int(SR * seconds)) / SR
    tone = (0.9 * np.sin(2 * np.pi * 180 * t) + 0.4 * np.sin(2 * np.pi * 360 * t + 0.5)) * (0.6 + 0.4 * np.sin(2 * np.pi * 1.3 * t))
    return tone + np.random.default_rng(seed).normal(0, 0.005, len(t))


def test_rebuilds_clipped_peaks_and_restores_headroom():
    truth = speech_like()
    clipped = np.clip(truth, -0.75, 0.75)
    result = audio_analysis.declip(clipped)
    assert result.regions > 100 and result.clipped_samples > result.regions
    assert result.gain_db < 0 and np.abs(result.samples).max() <= 0.971
    gain = 10 ** (result.gain_db / 20)
    region = np.abs(clipped) >= 0.75 * 0.995
    before = np.sqrt(np.mean((clipped[region] - truth[region]) ** 2))
    after = np.sqrt(np.mean((result.samples[region] / gain - truth[region]) ** 2))
    assert after < before * 0.5
    # 未削波的部分只做了整体增益，形状不变
    np.testing.assert_allclose(result.samples[~region] / gain, clipped[~region], rtol=1e-3, atol=1e-4)  # 报告的增益保留两位小数


@pytest.mark.parametrize("signal", [
    speech_like() / np.abs(speech_like()).max() * 0.95,
    0.5 * np.sin(2 * np.pi * 200 * np.arange(SR * 2) / SR),
    np.zeros(SR),
    0.1 * np.clip(speech_like(), -0.75, 0.75),  # 太安静：不可能是削波
])
def test_leaves_unclipped_audio_untouched(signal):
    result = audio_analysis.declip(signal)
    assert result.regions == 0 and result.gain_db == 0
    np.testing.assert_array_equal(result.samples, signal.astype(np.float32))


def test_skips_regions_too_long_to_interpolate():
    signal = speech_like(1.0)
    signal[4000:5000] = 0.99  # 一整段 1000 采样的平顶：插值不可信，不处理
    signal = np.clip(signal, -0.99, 0.99)
    result = audio_analysis.declip(signal, max_region=400)
    assert np.all(result.samples[4000:5000] == result.samples[4000])


def new_job(factory, session_id, mode="balanced", declip="auto"):
    with factory() as db:
        job = EnhancementJob(session_id=session_id, mode=mode, declip_mode=declip)
        db.add(job)
        db.commit()
        return job.id


async def test_auto_declip_runs_only_when_report_shows_clipping(isolated_backend, noisy_webm_file, clean_webm_file):
    clipped_session = await prepared_session(isolated_backend, noisy_webm_file)   # 合成信号含削波
    clean_session = await prepared_session(isolated_backend, clean_webm_file)
    with isolated_backend() as db:
        assert db.get(AudioQualityReport, clipped_session).clipping_ratio > 0.001
        assert db.get(AudioQualityReport, clean_session).clipping_ratio <= 0.001
    clipped_job = new_job(isolated_backend, clipped_session)
    clean_job = new_job(isolated_backend, clean_session)
    await run_enhancement(clipped_job)
    await run_enhancement(clean_job)
    with isolated_backend() as db:
        job = db.get(EnhancementJob, clipped_job)
        assert job.status == "COMPLETED"
        assert job.requested_chain == ["declip", "denoise"] and job.actual_chain == ["declip", "denoise"]
        assert job.declipped_regions > 0
        version = db.get(AudioVersion, job.result_version_id)
        assert version.label == "去削波 + 降噪版本" and version.engine == "declip_poly3+spectral_subtraction"
        clean = db.get(EnhancementJob, clean_job)
        assert clean.requested_chain == ["denoise"] and clean.declipped_regions == 0


async def test_declip_only_and_forced_on_clean_audio(isolated_backend, noisy_webm_file, clean_webm_file):
    clipped_session = await prepared_session(isolated_backend, noisy_webm_file)
    clean_session = await prepared_session(isolated_backend, clean_webm_file)
    only = new_job(isolated_backend, clipped_session, mode="off", declip="on")
    forced = new_job(isolated_backend, clean_session, mode="off", declip="on")
    await run_enhancement(only)
    await run_enhancement(forced)
    with isolated_backend() as db:
        job = db.get(EnhancementJob, only)
        assert job.actual_chain == ["declip"]
        assert db.get(AudioVersion, job.result_version_id).label == "去削波版本"
        # 强制开启但没有削波：请求了，实际没做，版本是未处理副本
        job = db.get(EnhancementJob, forced)
        assert job.requested_chain == ["declip"] and job.actual_chain == [] and job.declipped_regions == 0
        assert db.get(AudioVersion, job.result_version_id).label == "未降噪副本"


def test_api_accepts_declip_option(isolated_backend):
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as client:
        session_id = client.post("/api/sessions", json={}).json()["id"]
        assert client.post(f"/api/sessions/{session_id}/enhancement", json={"mode": "auto", "declip": "maybe"}).status_code == 422


def test_quality_report_detects_clipping_below_full_scale():
    clean = speech_like() / np.abs(speech_like()).max() * 0.9
    assert audio_analysis.clip_ratio(clean) == 0.0
    clipped = np.clip(speech_like() * 1.3, -0.9, 0.9)      # 在 0.9 处削平，没到满幅
    assert audio_analysis.clip_ratio(clipped) > 0.01
    assert audio_analysis.analyze_samples(clipped.astype(np.float32)).clipping_ratio > 0.001
