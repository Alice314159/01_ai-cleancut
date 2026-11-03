from collections.abc import Generator

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import DATABASE_URL

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},  # SQLite + 多线程/WS 访问需要这个
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


@event.listens_for(Engine, "connect")
def _configure_sqlite(dbapi_connection, _record) -> None:
    """
    增强、转写、终态化都在后台线程里写库，同时 HTTP / WS 请求也在写。SQLite 默认的
    rollback journal 下读写互斥，且锁冲突会立刻抛 "database is locked"。
    WAL 让读不阻塞写；busy_timeout 让短暂的写锁竞争排队等待而不是直接失败。
    挂在 Engine 类上，测试里另建的临时引擎也会生效。
    """
    import sqlite3

    if not isinstance(dbapi_connection, sqlite3.Connection):
        return
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA busy_timeout = 5000")
        cursor.execute("PRAGMA journal_mode = WAL")
        cursor.execute("PRAGMA synchronous = NORMAL")
    finally:
        cursor.close()


class Base(DeclarativeBase):
    pass


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    # 导入全部模型模块，确保它们在 metadata 上注册，再建表。
    from app import models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    # Existing SQLite installations predate job timestamps. Preserve their insertion order.
    columns = {column["name"] for column in inspect(engine).get_columns("enhancement_jobs")}
    if "created_at_ns" not in columns:
        with engine.begin() as connection:
            connection.execute(text("ALTER TABLE enhancement_jobs ADD COLUMN created_at_ns BIGINT NOT NULL DEFAULT 0"))
            connection.execute(text("UPDATE enhancement_jobs SET created_at_ns = rowid"))
    columns = {column["name"] for column in inspect(engine).get_columns("enhancement_jobs")}
    with engine.begin() as connection:
        if "declip_mode" not in columns:
            connection.execute(text("ALTER TABLE enhancement_jobs ADD COLUMN declip_mode VARCHAR NOT NULL DEFAULT 'off'"))
        if "declipped_regions" not in columns:
            connection.execute(text("ALTER TABLE enhancement_jobs ADD COLUMN declipped_regions INTEGER NOT NULL DEFAULT 0"))
        if "dereverb" not in columns:
            connection.execute(text("ALTER TABLE enhancement_jobs ADD COLUMN dereverb BOOLEAN NOT NULL DEFAULT 0"))
        if "dereverb_t60_s" not in columns:
            connection.execute(text("ALTER TABLE enhancement_jobs ADD COLUMN dereverb_t60_s FLOAT"))
        if "dereverb_reduction_db" not in columns:
            connection.execute(text("ALTER TABLE enhancement_jobs ADD COLUMN dereverb_reduction_db FLOAT"))

    columns = {column["name"] for column in inspect(engine).get_columns("recording_sessions")}
    if "created_at_ns" not in columns:
        # 旧会话没有记录创建时间：补 0（输出为 null），列表排序回落到 rowid。
        with engine.begin() as connection:
            connection.execute(text("ALTER TABLE recording_sessions ADD COLUMN created_at_ns BIGINT NOT NULL DEFAULT 0"))
    if "cover_filename" not in columns:
        with engine.begin() as connection:
            connection.execute(text("ALTER TABLE recording_sessions ADD COLUMN cover_filename VARCHAR"))
    if "received_chunks" not in columns:
        with engine.begin() as connection:
            connection.execute(text("ALTER TABLE recording_sessions ADD COLUMN received_chunks INTEGER NOT NULL DEFAULT 0"))
    if "client_recorded_ms" not in columns:
        with engine.begin() as connection:
            connection.execute(text("ALTER TABLE recording_sessions ADD COLUMN client_recorded_ms INTEGER"))

    columns = {column["name"] for column in inspect(engine).get_columns("transcript_tokens")}
    with engine.begin() as connection:
        if "end_ms" not in columns:
            connection.execute(text("ALTER TABLE transcript_tokens ADD COLUMN end_ms INTEGER"))
        if "job_id" not in columns:
            connection.execute(text("ALTER TABLE transcript_tokens ADD COLUMN job_id VARCHAR"))

    columns = {column["name"] for column in inspect(engine).get_columns("render_outputs")}
    with engine.begin() as connection:
        if "transcript_snapshot" not in columns:
            connection.execute(text("ALTER TABLE render_outputs ADD COLUMN transcript_snapshot JSON"))
        if "loudness" not in columns:
            connection.execute(text("ALTER TABLE render_outputs ADD COLUMN loudness JSON"))

    columns = {column["name"] for column in inspect(engine).get_columns("edit_decisions")}
    if "revision" not in columns:
        with engine.begin() as connection:
            connection.execute(text("ALTER TABLE edit_decisions ADD COLUMN revision INTEGER NOT NULL DEFAULT 0"))

    with engine.begin() as connection:
        if "replacement_version_id" not in columns:
            connection.execute(text("ALTER TABLE edit_decisions ADD COLUMN replacement_version_id VARCHAR"))
        if "replacement_duration_ms" not in columns:
            connection.execute(text("ALTER TABLE edit_decisions ADD COLUMN replacement_duration_ms INTEGER"))
