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


class RenderOutputOut(CamelModel):
    version: AudioVersionOut
    source_version_id: str
    removed_duration_ms: int
    cut_ranges: list[CutRangeOut]
    stale: bool
    has_replacements: bool = False
    transcription_job: TranscriptionJobOut | None = None
    transcript_ready: bool = False


class RenderOptionsIn(CamelModel):
    smooth_cuts: bool = Field(default=False, strict=True)
