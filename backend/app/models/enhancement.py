import uuid
import time

from sqlalchemy import BigInteger, JSON, Boolean, Float, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class EnhancementJob(Base):
    """对应前端 EnhancementJob。每次点击“增强”插入一行，session 可以有多条历史任务。"""

    __tablename__ = "enhancement_jobs"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: f"job_{uuid.uuid4().hex[:6]}")
    session_id: Mapped[str] = mapped_column(ForeignKey("recording_sessions.id"), index=True)

    created_at_ns: Mapped[int] = mapped_column(BigInteger, default=time.time_ns)

    mode: Mapped[str] = mapped_column(String)  # off|light|balanced|strong|custom
    status: Mapped[str] = mapped_column(String, default="QUEUED")  # QUEUED|PROCESSING|QUALITY_CHECKING|COMPLETED|FAILED
    progress: Mapped[float] = mapped_column(Float, default=0.0)

    enhancement_success: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    noise_reduction_db: Mapped[float] = mapped_column(Float, default=0.0)

    # 前端类型是 string[]（处理步骤名列表）；当前只会出现 [] 或 ["denoise"]
    requested_chain: Mapped[list] = mapped_column(JSON, default=list)
    actual_chain: Mapped[list] = mapped_column(JSON, default=list)
    # 去削波：auto=检测到削波才做，on=总是尝试，off=不做；declipped_regions 为实际修复的段数
    declip_mode: Mapped[str] = mapped_column(String, default="auto")
    declipped_regions: Mapped[int] = mapped_column(Integer, default=0)
    # 减轻混响（可选，默认关）：估计的混响时间与平均压低量
    dereverb: Mapped[bool] = mapped_column(Boolean, default=False)
    dereverb_t60_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    dereverb_reduction_db: Mapped[float | None] = mapped_column(Float, nullable=True)

    result_version_id: Mapped[str | None] = mapped_column(String, nullable=True)
    error_message: Mapped[str | None] = mapped_column(String, nullable=True)


class VoiceProtectionMetric(Base):
    """
    对应前端 VoiceProtectionMetrics 里的每一项指标（音色保真度、动态范围保留...）。
    "pass" 是 Python/SQL 关键字，列名用 passed，暴露给前端时在 schema 层映射回 pass。
    """

    __tablename__ = "voice_protection_metrics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("enhancement_jobs.id"), index=True)

    key: Mapped[str] = mapped_column(String)
    label: Mapped[str] = mapped_column(String)
    value: Mapped[float] = mapped_column(Float)
    unit: Mapped[str] = mapped_column(String)
    target: Mapped[str] = mapped_column(String)
    passed: Mapped[bool] = mapped_column(Boolean)
    hard: Mapped[bool] = mapped_column(Boolean, default=False)
