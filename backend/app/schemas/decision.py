from pydantic import Field

from app.schemas.common import CamelModel


class EditDecisionOut(CamelModel):
    replacement_version_id: str | None = None
    replacement_duration_ms: int | None = None
    revision: int
    id: str
    start_ms: int
    end_ms: int
    action: str  # DELETE_REPETITION|DELETE_STUTTER|DELETE_FILLER|COMPRESS_SILENCE|KEEP_PROTECTED
    reason: str
    text: str
    confidence: float
    source: str  # algorithm|user
    hard_protected: bool
    review_required: bool
    accepted: bool | None


class UpdateDecisionIn(CamelModel):
    accepted: bool | None
    expected_revision: int | None = Field(default=None, ge=0, strict=True)


class BatchDecisionsIn(CamelModel):
    expected_revisions: dict[str, int] = Field(default_factory=dict)
