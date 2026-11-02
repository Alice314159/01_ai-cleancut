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


class DecisionStateIn(CamelModel):
    """撤销/重做要恢复到的一条决策的完整状态（与 EditDecisionOut 同形）。"""

    id: str = Field(min_length=1, max_length=64)
    start_ms: int = Field(ge=0)
    end_ms: int = Field(gt=0)
    action: str
    reason: str = ""
    text: str = ""
    confidence: float = 1.0
    source: str
    review_required: bool = True
    accepted: bool | None
    replacement_version_id: str | None = None
    replacement_duration_ms: int | None = None


class RestoreDecisionsIn(CamelModel):
    """把一组决策恢复到给定状态：target 中的写入（不存在则按原 id 重建），remove 中的删除。
    expected_revisions 必须覆盖当前存在的每一个涉及的决策，任何一条对不上就整体拒绝。"""

    target: list[DecisionStateIn] = Field(default_factory=list, max_length=2000)
    remove: list[str] = Field(default_factory=list, max_length=2000)
    expected_revisions: dict[str, int] = Field(default_factory=dict)
