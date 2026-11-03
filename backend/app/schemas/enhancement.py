from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.common import CamelModel, WarningOut


class VoiceProtectionMetricOut(CamelModel):
    key: str
    label: str
    value: float
    unit: str
    target: str
    # ORM 列叫 passed（避开 Python/SQL 关键字），对外仍暴露成 pass，和前端类型对齐
    passed: bool = Field(serialization_alias="pass")
    hard: bool | None = None


class EnhancementJobOut(CamelModel):
    id: str
    mode: str  # off|light|balanced|strong|custom
    status: str
    progress: float
    result_version_id: str | None = None
    error_message: str | None = None
    enhancement_success: bool | None
    noise_reduction_db: float
    voice_protection: list[VoiceProtectionMetricOut] = []
    requested_chain: list[str]
    actual_chain: list[str]
    declip_mode: str = "off"
    declipped_regions: int = 0
    warnings: list[WarningOut] = []


class CreateEnhancementIn(BaseModel):
    mode: Literal["auto", "conservative", "aggressive", "off", "light", "balanced", "strong", "custom"] = "auto"
    # 去削波：auto 检测到削波才修复；on 总是尝试（无削波时不改动）；off 不做
    declip: Literal["auto", "on", "off"] = "auto"
