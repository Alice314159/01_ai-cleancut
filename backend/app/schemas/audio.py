from app.schemas.common import CamelModel


class AudioVersionOut(CamelModel):
    id: str
    version_type: str
    label: str
    duration_ms: int
    sample_count: int
    sample_rate: int
    latency_ms: int
    engine: str
    engine_version: str
    determinism: str
    processing_locality: str


class RoleAssignmentOut(CamelModel):
    role: str
    audio_version_id: str
    upgraded_from: str | None = None
