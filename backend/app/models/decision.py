import uuid

from sqlalchemy import Boolean, Float, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class EditDecision(Base):
    """
    对应前端 EditDecision——复核页的核心数据。accepted 为 null 表示用户尚未表态，
    true/false 是用户已确认接受/拒绝。hard_protected 的行前端会禁用勾选框。
    """

    __tablename__ = "edit_decisions"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: f"d_{uuid.uuid4().hex[:6]}")
    session_id: Mapped[str] = mapped_column(ForeignKey("recording_sessions.id"), index=True)

    start_ms: Mapped[int] = mapped_column(Integer)
    end_ms: Mapped[int] = mapped_column(Integer)
    # DELETE_REPETITION|DELETE_STUTTER|DELETE_FILLER|COMPRESS_SILENCE|KEEP_PROTECTED
    action: Mapped[str] = mapped_column(String)
    reason: Mapped[str] = mapped_column(String, default="")  # 自由文本，人类可读的判定依据
    text: Mapped[str] = mapped_column(String, default="")
    confidence: Mapped[float] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String)  # algorithm|user
    hard_protected: Mapped[bool] = mapped_column(Boolean, default=False)
    review_required: Mapped[bool] = mapped_column(Boolean, default=True)
    accepted: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=None)

    revision: Mapped[int] = mapped_column(Integer, default=0)

    replacement_version_id: Mapped[str | None] = mapped_column(ForeignKey("audio_versions.id"), nullable=True)
    replacement_duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
