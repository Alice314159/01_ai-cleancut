"""
Pydantic schema 与 src/types/api.ts 逐字段对齐，字段名用 camelCase（前端习惯），
通过 alias_generator 自动把 ORM 的 snake_case 转过来。
"""
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


class CamelModel(BaseModel):
    """所有对外 schema 的基类：自动 snake_case -> camelCase，且能直接从 ORM 对象读取。"""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        from_attributes=True,
    )


class WarningOut(CamelModel):
    code: str
    message: str
    stage: str | None = None
