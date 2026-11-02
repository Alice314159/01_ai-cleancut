import uuid

from sqlalchemy import Boolean, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class TranscriptToken(Base):
    """对应前端 TranscriptToken——按词/短语切分，state 与 EditDecision 联动但独立存储，方便按时间轴渲染。"""

    __tablename__ = "transcript_tokens"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: f"t_{uuid.uuid4().hex[:6]}")
    session_id: Mapped[str] = mapped_column(ForeignKey("recording_sessions.id"), index=True)

    text: Mapped[str] = mapped_column(String)
    end_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    job_id: Mapped[str | None] = mapped_column(String, nullable=True)

    start_ms: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String, default="provisional")  # committed|provisional
    cut: Mapped[bool] = mapped_column(Boolean, default=False)

    # 保持插入顺序用，SQLite 默认按 rowid 排序，但显式列更稳
    seq: Mapped[int] = mapped_column(Integer, default=0)
