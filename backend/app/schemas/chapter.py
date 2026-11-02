from pydantic import Field, field_validator

from app.schemas.common import CamelModel


def _clean_title(value: str | None) -> str | None:
    if value is None:
        return None
    value = " ".join(value.split())
    if not value:
        raise ValueError("章节标题不能为空")
    return value


class ChapterOut(CamelModel):
    id: str
    start_ms: int
    title: str


class CreateChapterIn(CamelModel):
    start_ms: int = Field(ge=0, strict=True)
    title: str = Field(max_length=100)

    _title = field_validator("title")(_clean_title)


class UpdateChapterIn(CamelModel):
    start_ms: int | None = Field(default=None, ge=0, strict=True)
    title: str | None = Field(default=None, max_length=100)

    _title = field_validator("title")(_clean_title)


class MappedChapterOut(CamelModel):
    """换算到某个音频版本时间轴后的章节。"""

    id: str
    title: str
    start_ms: int
    end_ms: int
    original_start_ms: int
    moved: bool  # 原起点落在被删除/重录的区间里，已挪到该区间在成片中的位置
