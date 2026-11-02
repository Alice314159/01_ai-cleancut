"""节目封面：校验图片、统一转成 JPEG（长边不超过 1400 像素，去掉原图元数据），存在会话目录下。"""
from pathlib import Path
import subprocess
import time

from app.services import storage

MAX_COVER_BYTES = 5 * 1024 * 1024
MAX_SIDE = 1400
# 只认常见的位图格式；按文件头判断，不信扩展名和 Content-Type。
_SIGNATURES = (b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n", b"RIFF")


class CoverError(Exception):
    pass


def looks_like_image(head: bytes) -> bool:
    if head.startswith(b"RIFF"):
        return head[8:12] == b"WEBP"
    return any(head.startswith(sig) for sig in _SIGNATURES)


def save(session_id: str, data: bytes) -> str:
    if not data:
        raise CoverError("上传的图片为空")
    if len(data) > MAX_COVER_BYTES:
        raise CoverError("封面图片不能超过 5MB")
    if not looks_like_image(data[:16]):
        raise CoverError("封面仅支持 JPEG、PNG 或 WebP 图片")
    folder = Path(storage.session_dir(session_id))
    filename = f"cover_{time.time_ns()}.jpg"
    source = folder / f".{filename}.upload"
    target = folder / filename
    source.write_bytes(data)
    scale = (f"scale='if(gt(iw,ih),min({MAX_SIDE},iw),-2)':'if(gt(iw,ih),-2,min({MAX_SIDE},ih))'")
    try:
        result = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-y", "-i", str(source), "-map_metadata", "-1",
                                 "-frames:v", "1", "-vf", scale, "-q:v", "3", str(target)], capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CoverError("图片处理失败或超时") from exc
    finally:
        source.unlink(missing_ok=True)
    if result.returncode or not target.is_file() or target.stat().st_size == 0:
        target.unlink(missing_ok=True)
        raise CoverError("无法读取这张图片，请换一张 JPEG 或 PNG")
    return filename


def remove_files(session_id: str, keep: str | None = None) -> None:
    for path in Path(storage.session_dir(session_id)).glob("cover_*.jpg"):
        if path.name != keep:
            path.unlink(missing_ok=True)
