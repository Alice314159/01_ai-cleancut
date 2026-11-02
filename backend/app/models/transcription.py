import time
import uuid

from sqlalchemy import BigInteger, Float, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class TranscriptionJob(Base):
    __tablename__ = "transcription_jobs"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: f"asr_{uuid.uuid4().hex}")
    session_id: Mapped[str] = mapped_column(ForeignKey("recording_sessions.id"), index=True)
    source_version_id: Mapped[str] = mapped_column(ForeignKey("audio_versions.id"))
    created_at_ns: Mapped[int] = mapped_column(BigInteger, default=time.time_ns)
    model: Mapped[str] = mapped_column(String)
    requested_language: Mapped[str] = mapped_column(String, default="auto")
    detected_language: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, default="QUEUED")
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    error_message: Mapped[str | None] = mapped_column(String, nullable=True)
