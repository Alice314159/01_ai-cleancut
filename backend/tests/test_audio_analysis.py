"""
把这一整轮手工验证过的行为固化成可重复运行的测试：质量分析要能分清干净/嘈杂音频，
降噪要真的降低噪声，声学自相似度要能分清真重复和孤立片段，实时质量事件要在
干净音频上零误报、在真的有问题的片段上正确触发。
"""
import numpy as np
import pytest

from app.services import audio_analysis


class TestAnalyze:
    def test_clean_audio_gets_good_grade(self, clean_webm_file):
        metrics = audio_analysis.analyze(clean_webm_file)
        assert metrics.quality_grade in ("EXCELLENT", "GOOD")
        assert metrics.clipping_ratio < 0.001
        assert metrics.hum_probability < 0.3
        assert "hum" not in metrics.noise_types
        assert "clipping" not in metrics.noise_types

    def test_noisy_audio_gets_poor_grade(self, noisy_webm_file):
        metrics = audio_analysis.analyze(noisy_webm_file)
        assert metrics.quality_grade in ("POOR", "FAIR")
        assert metrics.clipping_ratio > 0.01
        assert metrics.hum_probability > 0.3
        assert "hum" in metrics.noise_types
        assert "clipping" in metrics.noise_types

    def test_clean_vs_noisy_snr_ordering(self, clean_webm_file, noisy_webm_file):
        """不只看绝对档位，直接断言干净的那段信噪比就是应该比嘈杂的高——这是核心不变量。"""
        clean = audio_analysis.analyze(clean_webm_file)
        noisy = audio_analysis.analyze(noisy_webm_file)
        assert clean.estimated_snr_db > noisy.estimated_snr_db

    def test_missing_file_raises_decode_error(self, tmp_path):
        with pytest.raises(audio_analysis.DecodeError):
            audio_analysis.analyze(tmp_path / "does_not_exist.webm")

    def test_empty_file_raises_decode_error(self, tmp_path):
        empty = tmp_path / "empty.webm"
        empty.write_bytes(b"")
        with pytest.raises(audio_analysis.DecodeError):
            audio_analysis.analyze(empty)

    def test_analyze_and_analyze_samples_agree(self, clean_webm_file, clean_audio_samples):
        """analyze() 现在是 decode_pcm + analyze_samples 的组合，两条路径结果要完全一致。"""
        via_path = audio_analysis.analyze(clean_webm_file)
        via_samples = audio_analysis.analyze_samples(clean_audio_samples)
        assert via_path.quality_grade == via_samples.quality_grade
        assert via_path.duration_ms == via_samples.duration_ms


class TestEnvelope:
    def test_envelope_length_matches_requested_buckets(self, clean_webm_file):
        env = audio_analysis.compute_envelope(clean_webm_file, buckets=50)
        assert len(env) == 50

    def test_envelope_values_in_valid_range(self, clean_webm_file):
        env = audio_analysis.compute_envelope(clean_webm_file, buckets=100)
        assert all(0.0 <= v <= 1.0 for v in env)

    def test_envelope_missing_file_raises(self, tmp_path):
        with pytest.raises(audio_analysis.DecodeError):
            audio_analysis.compute_envelope(tmp_path / "nope.webm")


class TestDenoise:
    def test_denoise_reduces_measured_noise_floor(self, noisy_audio_samples):
        denoised = audio_analysis.spectral_subtract_denoise(noisy_audio_samples, over_subtraction=1.8)
        vp = audio_analysis.compute_voice_protection(
            noisy_audio_samples, denoised, sample_rate=audio_analysis.ANALYSIS_SAMPLE_RATE
        )
        assert vp.noise_reduction_db > 5, "降噪前后应该有实质性的、可测量的噪声下降"

    def test_denoise_preserves_output_length(self, noisy_audio_samples):
        denoised = audio_analysis.spectral_subtract_denoise(noisy_audio_samples)
        assert len(denoised) == len(noisy_audio_samples)

    def test_denoise_on_very_short_signal_is_noop(self):
        """短于半秒的信号直接原样返回——见 spectral_subtract_denoise 里的提前退出。"""
        short = np.random.default_rng(0).normal(0, 0.1, 100).astype(np.float32)
        result = audio_analysis.spectral_subtract_denoise(short)
        np.testing.assert_array_equal(result, short)

    def test_voice_protection_metrics_in_valid_ranges(self, noisy_audio_samples):
        denoised = audio_analysis.spectral_subtract_denoise(noisy_audio_samples)
        vp = audio_analysis.compute_voice_protection(
            noisy_audio_samples, denoised, sample_rate=audio_analysis.ANALYSIS_SAMPLE_RATE
        )
        assert 0.0 <= vp.timbre_similarity <= 1.0
        assert 0.0 <= vp.dynamic_range_retained <= 1.0
        assert 0.0 <= vp.artifact_score <= 1.0
        assert vp.spectral_centroid_shift_pct >= 0.0


class TestRealtimeQualityEvents:
    def test_clean_loud_segment_produces_no_events(self, clean_audio_samples):
        events = audio_analysis.detect_realtime_quality_events(clean_audio_samples)
        assert events == []

    def test_silence_produces_no_input_warning(self, silent_audio_samples):
        events = audio_analysis.detect_realtime_quality_events(silent_audio_samples)
        assert len(events) == 1
        assert events[0].event_type == "no_input"
        assert events[0].severity == "warning"

    def test_clipped_signal_produces_clipping_warning(self, noisy_audio_samples):
        events = audio_analysis.detect_realtime_quality_events(noisy_audio_samples)
        types = [e.event_type for e in events]
        assert "clipping" in types

    def test_spike_against_quiet_background_detected(self):
        rng = np.random.default_rng(2)
        segment = rng.normal(0, 0.01, audio_analysis.ANALYSIS_SAMPLE_RATE).astype(np.float32)
        segment[8000] = 0.8  # 突发的大响声
        events = audio_analysis.detect_realtime_quality_events(segment)
        types = [e.event_type for e in events]
        assert "noise_spike" in types

    def test_modest_peak_in_already_loud_signal_not_flagged_as_spike(self, clean_audio_samples):
        """峰值检测比较的是相对该段自身典型幅度的倍数，不是绝对阈值——
        已经很响的信号里一个不算夸张的峰值不应该被当成突发噪声。"""
        segment = clean_audio_samples.copy()
        segment[1000] = min(1.0, np.max(np.abs(segment)) * 1.5)
        events = audio_analysis.detect_realtime_quality_events(segment)
        types = [e.event_type for e in events]
        assert "noise_spike" not in types

    def test_empty_segment_produces_no_events(self):
        assert audio_analysis.detect_realtime_quality_events(np.array([], dtype=np.float32)) == []


class TestSpeechActivity:
    def test_silence_has_no_speech_activity(self, silent_audio_samples):
        assert audio_analysis.has_speech_activity(silent_audio_samples) is False

    def test_loud_tone_has_speech_activity(self, clean_audio_samples):
        assert audio_analysis.has_speech_activity(clean_audio_samples) is True

    def test_very_short_segment_has_no_speech_activity(self):
        tiny = np.ones(10, dtype=np.float32) * 0.5
        assert audio_analysis.has_speech_activity(tiny) is False


class TestSelfSimilarity:
    def test_confirms_genuine_repeat_with_correct_offset(self, repeat_timeline):
        samples, marks = repeat_timeline
        start_ms, end_ms = marks["phrase_x_2"]
        result = audio_analysis.find_best_self_similarity(
            samples, audio_analysis.ANALYSIS_SAMPLE_RATE, int(start_ms), int(end_ms), lookback_ms=4000
        )
        assert result is not None
        similarity, offset_ms = result
        assert similarity > 0.9, "phrase_x_2 是 phrase_x_1 的真实重复，相似度应该非常高"
        # 两次出现之间的真实间隔是 phrase_x_1 结束到 phrase_x_2 开始，即 marks 里 bg_b 的时长
        expected_offset = marks["phrase_x_2"][0] - marks["phrase_x_1"][0]
        assert abs(offset_ms - expected_offset) < 50, "找到的偏移量应该对应构造时设定的真实间隔"

    def test_rejects_isolated_segment(self, repeat_timeline):
        samples, marks = repeat_timeline
        start_ms, end_ms = marks["phrase_y"]
        result = audio_analysis.find_best_self_similarity(
            samples, audio_analysis.ANALYSIS_SAMPLE_RATE, int(start_ms), int(end_ms), lookback_ms=4000
        )
        assert result is not None
        similarity, _ = result
        assert similarity < 0.5, "phrase_y 附近没有真实重复，相似度应该低"

    def test_candidate_window_does_not_overlap_target(self):
        """回归测试：候选窗口曾经因为 min_gap 小于片段本身时长而跟目标片段自己重叠，
        导致"无重复"场景也被误判为高相似度。这里用一个片段长度远大于 min_gap 的场景验证。"""
        sr = audio_analysis.ANALYSIS_SAMPLE_RATE
        different = np.sin(2 * np.pi * 400 * np.linspace(0, 0.4, int(sr * 0.4))).astype(np.float32) * 0.3
        gap = np.zeros(int(sr * 0.8), dtype=np.float32)
        target = np.sin(2 * np.pi * 150 * np.linspace(0, 0.4, int(sr * 0.4))).astype(np.float32) * 0.3
        timeline = np.concatenate([different, gap, target])
        target_start_ms = (len(different) + len(gap)) / sr * 1000
        target_end_ms = target_start_ms + 400
        result = audio_analysis.find_best_self_similarity(
            timeline, sr, int(target_start_ms), int(target_end_ms), lookback_ms=3000, min_gap_ms=60
        )
        assert result is not None
        similarity, _ = result
        assert similarity < 0.5, "target 和 different 频率不同，不应该测出高相似度（曾经因重叠 bug 测出 >0.99）"

    def test_out_of_bounds_target_returns_none(self, clean_audio_samples):
        result = audio_analysis.find_best_self_similarity(
            clean_audio_samples, audio_analysis.ANALYSIS_SAMPLE_RATE, 999_999, 1_000_000, lookback_ms=1000
        )
        assert result is None

    def test_too_short_target_returns_none(self, clean_audio_samples):
        result = audio_analysis.find_best_self_similarity(
            clean_audio_samples, audio_analysis.ANALYSIS_SAMPLE_RATE, 1000, 1001, lookback_ms=1000
        )
        assert result is None
