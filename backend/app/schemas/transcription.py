from typing import Literal

from app.schemas.common import CamelModel


class CreateTranscriptionIn(CamelModel):
    language: Literal["auto", "zh", "en"] = "auto"


class TranscriptionJobOut(CamelModel):
    id: str
    source_version_id: str
    model: str
    requested_language: str
    detected_language: str | None
    status: str
    progress: float
    error_message: str | None
