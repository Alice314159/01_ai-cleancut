from app.schemas.common import CamelModel


class TranscriptTokenOut(CamelModel):
    id: str
    text: str
    end_ms: int | None = None
    job_id: str | None = None
    start_ms: int
    state: str  # committed | provisional
    cut: bool | None = None
