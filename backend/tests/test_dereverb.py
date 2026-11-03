"""减轻混响：混响时间估计、拖尾压低、人声主体损失可控，以及接入增强流程。"""
import numpy as np
import pytest

from app.models import AudioVersion, EnhancementJob
from app.services import audio_analysis
from app.services.stub_pipeline import run_enhancement
from tests.test_regressions import prepared_session

SR = 16000


def syllables(seconds=10, seed=0):
    """类似说话的干声：150–300ms 的谐波“音节”，音高起伏，中间有 80–400ms 停顿。"""
    rng = np.random.default_rng(seed)
    out = np.zeros(SR * seconds)
    t = 0.2
    while t < seconds - 0.5:
        length = rng.uniform(0.15, 0.3)
        n = int(length * SR)
        time = np.arange(n) / SR
        f0 = rng.uniform(110, 220) * (1 + 0.1 * np.sin(2 * np.pi * 3 * time))
        phase = 2 * np.pi * np.cumsum(f0) / SR
        tone = sum(np.sin(h * phase) / h for h in range(1, 8))
        start = int(t * SR)
        out[start:start + n] += 0.25 * tone * np.sin(np.pi * np.arange(n) / n) ** 0.5
        t += length + rng.uniform(0.08, 0.4)
    return out


def reverberate(dry, t60, seed=1):
    rng = np.random.default_rng(seed)
    n = int(t60 * 1.2 * SR)
    time = np.arange(n) / SR
    rir = rng.normal(0, 1, n) * np.exp(-6.9 * time / t60)
    rir[: int(0.002 * SR)] = 0
    rir = rir / np.sqrt((rir ** 2).sum()) * 0.9
    rir[0] = 1.0
    wet = np.convolve(dry, rir)[: len(dry)]
    return (wet / np.abs(wet).max() * 0.8).astype(np.float32)


def levels(signal, reference):
    """(干声安静处的拖尾电平, 干声响亮处的电平)，单位 dB。"""
    hop = 160
    n = min(len(signal), len(reference)) // hop
    db = lambda s: 10 * np.log10((np.asarray(s[: n * hop], dtype=np.float64).reshape(n, hop) ** 2).mean(1) + 1e-12)
    ref = db(reference)
    quiet, loud = ref < np.percentile(ref, 95) - 35, ref > np.percentile(ref, 95) - 15
    sig = db(signal)
    return float(sig[quiet].mean()), float(sig[loud].mean())


@pytest.mark.parametrize("t60", [0.5, 0.8])
def test_estimates_reverb_time_roughly(t60):
    estimate = audio_analysis.estimate_t60(reverberate(syllables(), t60))
    assert estimate is not None and 0.5 * t60 <= estimate <= 1.5 * t60


def test_estimate_needs_enough_audio():
    assert audio_analysis.estimate_t60(np.zeros(200, dtype=np.float32)) is None


def test_suppresses_tails_and_keeps_speech_body():
    dry = syllables()
    wet = reverberate(dry, 0.8)
    result = audio_analysis.dereverb(wet)
    tail_before, body_before = levels(wet, dry)
    tail_after, body_after = levels(result.samples, dry)
    assert tail_before - tail_after >= 3       # 拖尾至少压低 3 dB
    assert body_before - body_after <= 3       # 人声主体损失不超过 3 dB
    assert result.t60_s is not None and result.reduction_db > 0
    assert len(result.samples) == len(wet)


def test_dry_audio_is_barely_changed():
    dry = syllables().astype(np.float32)
    result = audio_analysis.dereverb(dry)
    _, body_before = levels(dry, dry)
    _, body_after = levels(result.samples, dry)
    assert body_before - body_after <= 1.5


async def test_pipeline_records_dereverb(isolated_backend, clean_webm_file):
    session_id = await prepared_session(isolated_backend, clean_webm_file)
    with isolated_backend() as db:
        job = EnhancementJob(session_id=session_id, mode="balanced", declip_mode="off", dereverb=True)
        db.add(job)
        db.commit()
        job_id = job.id
    await run_enhancement(job_id)
    with isolated_backend() as db:
        job = db.get(EnhancementJob, job_id)
        assert job.status == "COMPLETED"
        assert job.requested_chain == job.actual_chain == ["dereverb", "denoise"]
        assert job.dereverb_reduction_db is not None
        version = db.get(AudioVersion, job.result_version_id)
        assert version.label == "减轻混响 + 降噪版本"
        assert version.engine == "late_reverb_suppression+spectral_subtraction"


def test_api_accepts_dereverb_flag(isolated_backend):
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as client:
        session_id = client.post("/api/sessions", json={}).json()["id"]
        response = client.post(f"/api/sessions/{session_id}/enhancement", json={"dereverb": "maybe"})
        assert response.status_code == 422


def test_quality_report_flags_reverb_by_estimated_t60():
    dry = syllables()
    assert "reverb" not in audio_analysis.analyze_samples(dry.astype(np.float32)).noise_types
    assert "reverb" in audio_analysis.analyze_samples(reverberate(dry, 1.0)).noise_types
