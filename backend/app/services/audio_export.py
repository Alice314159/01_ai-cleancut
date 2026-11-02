"""
把任一音频版本（通常是剪辑成片）转码为常用的压缩发布格式。

音频版本一经生成就不再修改，所以转码结果可以按 (版本, 格式, 码率) 缓存在会话目录下，
重复下载不重复编码；删除会话时随目录一并清理。写入先落临时文件再原子改名，
并发请求同一份导出也不会读到半写的文件。
"""
from dataclasses import dataclass
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import uuid

from app.services import chapters as chapter_service, storage


@dataclass(frozen=True)
class ExportFormat:
    encoder: str
    extension: str
    media_type: str
    extra_args: tuple[str, ...] = ()


FORMATS: dict[str, ExportFormat] = {
    "mp3": ExportFormat("libmp3lame", "mp3", "audio/mpeg", ("-id3v2_version", "3")),
    # faststart 把索引放到文件开头，浏览器和播客平台可以边下边播。
    "m4a": ExportFormat("aac", "m4a", "audio/mp4", ("-movflags", "+faststart")),
}
BITRATES_KBPS = (96, 128, 192, 256, 320)
DEFAULT_BITRATE_KBPS = 192
EXPORT_DIR = "exports"
TIMEOUT_SECONDS = 600


class ExportError(Exception):
    pass


class EncoderUnavailable(ExportError):
    pass


@lru_cache(maxsize=None)
def encoder_available(encoder: str) -> bool:
    try:
        result = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return re.search(rf"^\s*A\S*\s+{re.escape(encoder)}\s", result.stdout.decode(errors="ignore"), re.M) is not None


def metadata_signature(title: str = "", chapters=()) -> str:
    """标题与章节决定文件内容，必须进缓存键：改名或改章节后不能再拿到旧文件。"""
    payload = json.dumps([title, [(c.start_ms, c.end_ms, c.title) for c in chapters]], ensure_ascii=False)
    return hashlib.sha256(payload.encode()).hexdigest()[:10]


def export_path(session_id: str, version_id: str, fmt: str, bitrate_kbps: int, signature: str = "") -> Path:
    suffix = f"_{signature}" if signature else ""
    return Path(storage.session_dir(session_id)) / EXPORT_DIR / f"{version_id}_{bitrate_kbps}k{suffix}.{FORMATS[fmt].extension}"


def ensure_export(session_id: str, version_id: str, source: Path, fmt: str, bitrate_kbps: int,
                  title: str = "", chapters=()) -> Path:
    """返回已编码好的文件路径；不存在时现场转码。chapters 为该版本时间轴上的章节（含 end_ms）。"""
    if fmt not in FORMATS:
        raise ExportError(f"不支持的导出格式：{fmt}")
    if bitrate_kbps not in BITRATES_KBPS:
        raise ExportError(f"不支持的码率：{bitrate_kbps} kbps")
    spec = FORMATS[fmt]
    title = (title or "")[:200]
    destination = export_path(session_id, version_id, fmt, bitrate_kbps, metadata_signature(title, chapters))
    if destination.is_file() and destination.stat().st_size > 0:
        return destination
    if not encoder_available(spec.encoder):
        raise EncoderUnavailable(f"本机 ffmpeg 缺少 {spec.encoder} 编码器，无法导出 {fmt.upper()}")
    if not source.is_file():
        raise ExportError("音频文件缺失")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = destination.with_name(f".{uuid.uuid4().hex}.{spec.extension}")
    # 标题和章节写进 FFmpeg 元数据文件，再整体映射；源文件自带的元数据一律不带出。
    meta = destination.with_name(f".{uuid.uuid4().hex}.ffmeta")
    meta.write_text(chapter_service.ffmetadata(list(chapters), title), encoding="utf-8")
    command = ["ffmpeg", "-v", "error", "-nostdin", "-y", "-i", str(source), "-f", "ffmetadata", "-i", str(meta),
               "-map", "0:a", "-map_metadata", "1", "-map_chapters", "1" if chapters else "-1",
               "-c:a", spec.encoder, "-b:a", f"{bitrate_kbps}k", *spec.extra_args, str(temp)]
    try:
        result = subprocess.run(command, capture_output=True, timeout=TIMEOUT_SECONDS)
        if result.returncode or not temp.is_file() or temp.stat().st_size == 0:
            raise ExportError("转码失败：" + result.stderr.decode(errors="ignore")[-300:].strip())
        os.replace(temp, destination)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ExportError("转码失败或超时") from exc
    finally:
        temp.unlink(missing_ok=True)
        meta.unlink(missing_ok=True)
    # 同一版本、格式、码率的旧导出（标题或章节已变）不再需要。
    for stale in destination.parent.glob(f"{version_id}_{bitrate_kbps}k*.{spec.extension}"):
        if stale != destination:
            stale.unlink(missing_ok=True)
    return destination


def download_name(title: str, version_type: str, extension: str) -> str:
    """下载文件名：会话标题 + 版本类型。去掉文件系统不允许的字符。"""
    label = {"master": "成片", "original": "原始", "denoised": "降噪", "retake": "重录"}.get(version_type, version_type)
    stem = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "_", (title or "").strip())[:80].strip(" .") or "audio"
    return f"{stem}-{label}.{extension}"
