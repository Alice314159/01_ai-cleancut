from typing import Literal

from pydantic import Field, model_validator

from app.schemas.audio import AudioVersionOut
from app.schemas.common import CamelModel
from app.schemas.transcription import TranscriptionJobOut


class CreateManualDecisionIn(CamelModel):
    start_ms: int = Field(ge=0, strict=True)
    end_ms: int = Field(gt=0, strict=True)

    @model_validator(mode="after")
    def ordered_range(self):
        if self.end_ms <= self.start_ms:
            raise ValueError("结束时间必须大于开始时间")
        return self


class CutRangeOut(CamelModel):
    start_ms: int
    end_ms: int


class LoudnessOut(CamelModel):
    target: str
    target_lufs: float
    input_lufs: float | None
    input_true_peak_db: float | None
    gain_db: float
    output_lufs: float | None
    limited_by: str | None


class RenderOutputOut(CamelModel):
    version: AudioVersionOut
    source_version_id: str
    removed_duration_ms: int
    cut_ranges: list[CutRangeOut]
    stale: bool
    has_replacements: bool = False
    transcription_job: TranscriptionJobOut | None = None
    transcript_ready: bool = False
    loudness: LoudnessOut | None = None


class RenderOptionsIn(CamelModel):
    smooth_cuts: bool = Field(default=False, strict=True)
    # None 表示不做响度标准化；podcast=-16 LUFS，broadcast=-23 LUFS。
    loudness_target: Literal["podcast", "broadcast"] | None = None


class SuggestPausesIn(CamelModel):
    min_pause_ms: int = Field(default=1500, ge=600, le=10000, strict=True)
    keep_ms: int = Field(default=500, ge=100, le=2000, strict=True)

    @model_validator(mode="after")
    def keep_shorter_than_pause(self):
        if self.keep_ms >= self.min_pause_ms:
            raise ValueError("保留时长必须小于停顿阈值")
        return self
