from sqlalchemy import ForeignKey, Integer, JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class RenderOutput(Base):
    """Immutable output with its original-timeline source and edit snapshot."""

    __tablename__ = "render_outputs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("recording_sessions.id"), index=True)
    version_id: Mapped[str] = mapped_column(ForeignKey("audio_versions.id"), unique=True)
    source_version_id: Mapped[str] = mapped_column(ForeignKey("audio_versions.id"))
    fingerprint: Mapped[str] = mapped_column(String, index=True)
    decisions: Mapped[list] = mapped_column(JSON)
    cut_ranges: Mapped[list] = mapped_column(JSON)
    removed_duration_ms: Mapped[int] = mapped_column(Integer)

    transcript_snapshot: Mapped[list | None] = mapped_column(JSON, nullable=True)
