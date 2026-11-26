from app.schemas.common import CamelModel


class AudioQualityReportOut(CamelModel):
    quality_grade: str
    grade_reason: str
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
    content_type: str
    content_type_confidence: float
    content_type_overridden_by_user: bool
    speaker_analysis_status: str = "unavailable"
    speaker_count: int | None = None
    speaker_names: dict[str, str] | None = None
    # 以下两项在接口里现算：本机现在是否装好了模型；有没有全程分段（能不能手动更正）
    speaker_model_available: bool = False
    speakers: list[int] = []


class QualityEventOut(CamelModel):
    id: str
    event_type: str
    severity: str  # notice | warning
    client_timestamp_ms: int
    message: str
    metrics: dict[str, float | str] | None = None


class LiveSignalOut(CamelModel):
    id: str
    signal_level: str  # weak|medium|strong
    signal_type: str  # filler|stutter|repetition
    start_ms: int
    end_ms: int
    score: float
    text: str
    became_final_decision: bool | None = None
    invalidated: bool | None = None
    invalidation_reason: str | None = None


class SpeakerNamesIn(CamelModel):
    names: dict[str, str]


class AssignSpeakerIn(CamelModel):
    start_ms: int
    end_ms: int
    speaker: int
