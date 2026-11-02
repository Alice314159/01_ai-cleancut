import time
import uuid

from sqlalchemy import BigInteger, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Chapter(Base):
    """
    章节标记。start_ms 记在原始（剪辑前）时间轴上，和转写、剪辑决策同一坐标系；
    导出成片时再按该成片的删除/重录快照换算到成片时间轴，所以改剪辑不必重标章节。
    """

    __tablename__ = "chapters"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: f"ch_{uuid.uuid4().hex[:10]}")
    session_id: Mapped[str] = mapped_column(ForeignKey("recording_sessions.id"), index=True)
    start_ms: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String)
    created_at_ns: Mapped[int] = mapped_column(BigInteger, default=time.time_ns)
