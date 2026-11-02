import uuid

from sqlalchemy import JSON, Boolean, Float, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class AudioQualityReport(Base):
    """对应前端 AudioQualityReport。一个 session 一份，增强流程会读它决定处理链。"""

    __tablename__ = "audio_quality_reports"

    session_id: Mapped[str] = mapped_column(ForeignKey("recording_sessions.id"), primary_key=True)

    quality_grade: Mapped[str] = mapped_column(String)  # EXCELLENT|GOOD|FAIR|POOR|UNUSABLE
    grade_reason: Mapped[str] = mapped_column(String)
    integrated_lufs: Mapped[float] = mapped_column(Float)
    true_peak_dbtp: Mapped[float] = mapped_column(Float)
    noise_floor_dbfs: Mapped[float] = mapped_column(Float)
    estimated_snr_db: Mapped[float] = mapped_column(Float)
    clipping_ratio: Mapped[float] = mapped_column(Float)
    hum_probability: Mapped[float] = mapped_column(Float)
    reverb_score: Mapped[float] = mapped_column(Float)
    speech_coverage: Mapped[float] = mapped_column(Float)
    silence_coverage: Mapped[float] = mapped_column(Float)
    noise_types: Mapped[list] = mapped_column(JSON, default=list)
    content_type: Mapped[str] = mapped_column(String, default="unknown")
    content_type_confidence: Mapped[float] = mapped_column(Float, default=0.0)
    content_type_overridden_by_user: Mapped[bool] = mapped_column(Boolean, default=False)


class QualityEvent(Base):
    """对应前端 QualityEvent——录制中实时质量守卫事件，两级：notice / warning（ADR-049，非模态）。"""

    __tablename__ = "quality_events"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: f"qe_{uuid.uuid4().hex[:6]}")
    session_id: Mapped[str] = mapped_column(ForeignKey("recording_sessions.id"), index=True)

    event_type: Mapped[str] = mapped_column(String)
    severity: Mapped[str] = mapped_column(String)  # notice | warning
    client_timestamp_ms: Mapped[int] = mapped_column(Integer)
    message: Mapped[str] = mapped_column(String)
    metrics: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class LiveSignal(Base):
    """对应前端 LiveSignal——录制中的实时提示（重复/口吃/语气词），不是最终判定（ADR-053）。"""

    __tablename__ = "live_signals"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: f"s_{uuid.uuid4().hex[:6]}")
    session_id: Mapped[str] = mapped_column(ForeignKey("recording_sessions.id"), index=True)

    signal_level: Mapped[str] = mapped_column(String)  # weak|medium|strong
    signal_type: Mapped[str] = mapped_column(String)  # filler|stutter|repetition
    start_ms: Mapped[int] = mapped_column(Integer)
    end_ms: Mapped[int] = mapped_column(Integer)
    score: Mapped[float] = mapped_column(Float)
    text: Mapped[str] = mapped_column(String)
    became_final_decision: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    invalidated: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=False)
    invalidation_reason: Mapped[str | None] = mapped_column(String, nullable=True)
