import uuid

from sqlalchemy import ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class AudioVersion(Base):
    """对应前端 AudioVersion。每次产出一个新版本（原始/流式标准化/降噪/母版）都插入一行，旧版本不删。"""

    __tablename__ = "audio_versions"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: f"av_{uuid.uuid4().hex[:6]}")
    session_id: Mapped[str] = mapped_column(ForeignKey("recording_sessions.id"), index=True)

    version_type: Mapped[str] = mapped_column(String)  # original|normalized|stream_normalized|denoised|master
    label: Mapped[str] = mapped_column(String)
    duration_ms: Mapped[int] = mapped_column(Integer)
    sample_count: Mapped[int] = mapped_column(Integer)
    sample_rate: Mapped[int] = mapped_column(Integer)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    engine: Mapped[str] = mapped_column(String, default="—")
    engine_version: Mapped[str] = mapped_column(String, default="—")
    determinism: Mapped[str] = mapped_column(String, default="deterministic")
    processing_locality: Mapped[str] = mapped_column(String, default="local")

    filename: Mapped[str | None] = mapped_column(String, nullable=True)


class RoleAssignment(Base):
    """
    对应前端 RoleAssignment（ADR-024/052 的核心：每个角色显式指向一个版本 id，不靠推荐标记猜）。
    一个 session 内每个 role 只应有一条当前生效记录，用 (session_id, role) 联合查询取最新。
    """

    __tablename__ = "role_assignments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("recording_sessions.id"), index=True)

    role: Mapped[str] = mapped_column(String)  # TIMEBASE|ASR_SOURCE|REVIEW_SOURCE|RENDER_SOURCE|EXPORT_SOURCE|REFERENCE_SOURCE
    audio_version_id: Mapped[str] = mapped_column(ForeignKey("audio_versions.id"))
    upgraded_from: Mapped[str | None] = mapped_column(String, nullable=True)
