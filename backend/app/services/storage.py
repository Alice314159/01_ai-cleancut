"""
本地磁盘存储。所有音频文件放在 data/audio/{session_id}/ 下，
按版本类型命名，方便调试时直接用文件管理器看文件。
"""
import aiofiles

from app import config


def session_dir(session_id: str):
    d = config.AUDIO_DIR / session_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def raw_chunk_path(session_id: str) -> str:
    """WS 录制期间持续追加写入的原始分片文件。"""
    return str(session_dir(session_id) / "raw.webm")


def version_path(session_id: str, version_type: str) -> str:
    return str(session_dir(session_id) / f"{version_type}.webm")


async def append_chunk(session_id: str, chunk: bytes) -> None:
    path = raw_chunk_path(session_id)
    async with aiofiles.open(path, "ab") as f:
        await f.write(chunk)
