from sqlalchemy import ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Warning(Base):
    """
    对应前端 Warning。session/enhancement/task 三种 scope 共用一张表（ADR-032：
    警告不改变请求状态码，只在这里和任务结果的 warnings[] 里出现）。
    """

    __tablename__ = "warnings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("recording_sessions.id"), index=True)
    job_id: Mapped[str | None] = mapped_column(ForeignKey("enhancement_jobs.id"), nullable=True, index=True)

    scope: Mapped[str] = mapped_column(String)  # session|enhancement|task
    code: Mapped[str] = mapped_column(String)
    message: Mapped[str] = mapped_column(String)
    stage: Mapped[str | None] = mapped_column(String, nullable=True)
