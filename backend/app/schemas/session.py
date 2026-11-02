from datetime import datetime, timezone

from pydantic import Field, computed_field, field_serializer

from app.schemas.common import CamelModel, WarningOut


class AnalysisLocalityOut(CamelModel):
    analysis_locality: str

    @field_serializer('analysis_locality')
    def always_local(self, value):
        # 从未真正实现过云端分析路径；旧库里可能还留着建表时的 "cloud" 默认值，
        # 这里和 local_persistence_status 一样在输出边界强制纠正，不做迁移脚本。
        return 'local'


class CaptureProcessingStateOut(CamelModel):
    applied: bool
    unknown: bool
    echo_cancellation_requested: bool
    echo_cancellation_actual: str  # 'true'|'false'|'unknown'
    noise_suppression_requested: bool
    noise_suppression_actual: str
    auto_gain_control_requested: bool
    auto_gain_control_actual: str


class ClockDriftReportOut(CamelModel):
    decoded_sample_count: int
    expected_sample_count: int
    clock_drift_ppm: float
    encoder_delay_samples: int
    padding_samples: int
    drift_remap_applied: bool


class PersistenceStatusOut(CamelModel):
    local_persistence_status: str

    @field_serializer('local_persistence_status')
    def browser_persistence(self, value):
        # Legacy PERSISTED described server storage, never a durable browser copy.
        return 'NOT_SUPPORTED'


class RecordingSessionOut(PersistenceStatusOut, AnalysisLocalityOut):
    id: str
    title: str
    project_id: str
    capture_mode: str
    capture_status: str
    local_persistence_status: str
    upload_status: str
    server_merge_status: str
    live_analysis_status: str
    processing_status: str
    device_label: str
    actual_sample_rate: int
    channel_count: int
    mime_type: str
    lookback_window_ms: int
    committed_duration_ms: int
    total_duration_ms: int
    capture_processing: CaptureProcessingStateOut
    clock_drift: ClockDriftReportOut | None = None
    warnings: list[WarningOut] = []
    cover_filename: str | None = Field(default=None, exclude=True)

    @computed_field(alias="coverVersion")
    @property
    def cover_version(self) -> str | None:
        """有封面时为一个随封面变化的版本号（前端拼到封面 URL 上防缓存），没有封面为 null。"""
        return self.cover_filename.rsplit(".", 1)[0].removeprefix("cover_") if self.cover_filename else None


class RecordingSessionListItemOut(PersistenceStatusOut):
    """SessionsPage 用的精简视图，不带 warnings/captureProcessing 这些大字段。"""

    id: str
    title: str
    project_id: str
    capture_mode: str
    capture_status: str
    upload_status: str
    server_merge_status: str
    live_analysis_status: str
    processing_status: str
    total_duration_ms: int
    created_at_ns: int = Field(default=0, exclude=True)
    cover_filename: str | None = Field(default=None, exclude=True)

    @computed_field(alias="coverVersion")
    @property
    def cover_version(self) -> str | None:
        return self.cover_filename.rsplit(".", 1)[0].removeprefix("cover_") if self.cover_filename else None

    @computed_field(alias="createdAt")
    @property
    def created_at(self) -> str | None:
        """ISO 8601（UTC）。迁移前的旧会话没有记录创建时间，返回 null。"""
        if not self.created_at_ns:
            return None
        return datetime.fromtimestamp(self.created_at_ns / 1e9, tz=timezone.utc).isoformat(timespec="seconds")


class CreateSessionIn(CamelModel):
    project_id: str = "proj_default"
    capture_mode: str = "live"  # batch | live
    analysis_locality: str = "local"
    device_label: str = ""
    title: str = "未命名录制"


class UpdateSessionIn(CamelModel):
    """重命名会话标题或者改到另一个项目；两个字段都可选，缺省不改。"""

    title: str | None = None
    project_id: str | None = None
