import uuid

from sqlalchemy import JSON, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def gen_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:6]}"


class RecordingSession(Base):
    """
    对应前端 RecordingSession（types/api.ts）。
    captureProcessing / clockDrift 是固定形状的小型嵌套对象，直接存 JSON 列，
    没有必要为它们单独建表——它们从不需要独立查询。
    """

    __tablename__ = "recording_sessions"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: gen_id("rs"))
    project_id: Mapped[str] = mapped_column(String, default="proj_default")

    capture_mode: Mapped[str] = mapped_column(String, default="live")  # batch | live
    analysis_locality: Mapped[str] = mapped_column(String, default="local")  # 始终为 local，字段仅作展示历史遗留

    capture_status: Mapped[str] = mapped_column(String, default="IDLE")
    local_persistence_status: Mapped[str] = mapped_column(String, default="NOT_SUPPORTED")
    upload_status: Mapped[str] = mapped_column(String, default="NOT_STARTED")
    server_merge_status: Mapped[str] = mapped_column(String, default="NOT_STARTED")
    live_analysis_status: Mapped[str] = mapped_column(String, default="NOT_APPLICABLE")
    processing_status: Mapped[str] = mapped_column(String, default="NOT_STARTED")

    device_label: Mapped[str] = mapped_column(String, default="")
    actual_sample_rate: Mapped[int] = mapped_column(Integer, default=48000)
    channel_count: Mapped[int] = mapped_column(Integer, default=1)
    mime_type: Mapped[str] = mapped_column(String, default="audio/webm;codecs=opus")

    lookback_window_ms: Mapped[int] = mapped_column(Integer, default=15000)
    committed_duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    total_duration_ms: Mapped[int] = mapped_column(Integer, default=0)

    # CaptureProcessingState，形状固定见 types/api.ts
    capture_processing: Mapped[dict] = mapped_column(
        JSON,
        default=lambda: {
            "applied": False,
            "unknown": False,
            "echoCancellationRequested": True,
            "echoCancellationActual": "unknown",
            "noiseSuppressionRequested": True,
            "noiseSuppressionActual": "unknown",
            "autoGainControlRequested": True,
            "autoGainControlActual": "unknown",
        },
    )
    # ClockDriftReport，仅在 merge 阶段算出，录制中为 null
    clock_drift: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    audio_filename: Mapped[str | None] = mapped_column(String, nullable=True)

    # 展示用字段，SessionsPage 需要
    title: Mapped[str] = mapped_column(String, default="未命名录制")
