"""
把任一音频版本（通常是剪辑成片）转码为常用的压缩发布格式。

音频版本一经生成就不再修改，所以转码结果可以按 (版本, 格式, 码率) 缓存在会话目录下，
重复下载不重复编码；删除会话时随目录一并清理。写入先落临时文件再原子改名，
并发请求同一份导出也不会读到半写的文件。
"""
from dataclasses import dataclass
from functools import lru_cache
import os
from pathlib import Path
import re
import subprocess
import uuid

from app.services import storage


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


def export_path(session_id: str, version_id: str, fmt: str, bitrate_kbps: int) -> Path:
    return Path(storage.session_dir(session_id)) / EXPORT_DIR / f"{version_id}_{bitrate_kbps}k.{FORMATS[fmt].extension}"


def ensure_export(session_id: str, version_id: str, source: Path, fmt: str, bitrate_kbps: int,
                  title: str = "") -> Path:
    """返回已编码好的文件路径；不存在时现场转码。"""
    if fmt not in FORMATS:
        raise ExportError(f"不支持的导出格式：{fmt}")
    if bitrate_kbps not in BITRATES_KBPS:
        raise ExportError(f"不支持的码率：{bitrate_kbps} kbps")
    spec = FORMATS[fmt]
    destination = export_path(session_id, version_id, fmt, bitrate_kbps)
    if destination.is_file() and destination.stat().st_size > 0:
        return destination
    if not encoder_available(spec.encoder):
        raise EncoderUnavailable(f"本机 ffmpeg 缺少 {spec.encoder} 编码器，无法导出 {fmt.upper()}")
    if not source.is_file():
        raise ExportError("音频文件缺失")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = destination.with_name(f".{uuid.uuid4().hex}.{spec.extension}")
    command = ["ffmpeg", "-v", "error", "-nostdin", "-y", "-i", str(source), "-vn", "-map_metadata", "-1",
               "-c:a", spec.encoder, "-b:a", f"{bitrate_kbps}k", *spec.extra_args]
    if title:
        command += ["-metadata", f"title={title[:200]}"]
    command.append(str(temp))
    try:
        result = subprocess.run(command, capture_output=True, timeout=TIMEOUT_SECONDS)
        if result.returncode or not temp.is_file() or temp.stat().st_size == 0:
            raise ExportError("转码失败：" + result.stderr.decode(errors="ignore")[-300:].strip())
        os.replace(temp, destination)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ExportError("转码失败或超时") from exc
    finally:
        temp.unlink(missing_ok=True)
    return destination


def download_name(title: str, version_type: str, extension: str) -> str:
    """下载文件名：会话标题 + 版本类型。去掉文件系统不允许的字符。"""
    label = {"master": "成片", "original": "原始", "denoised": "降噪", "retake": "重录"}.get(version_type, version_type)
    stem = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "_", (title or "").strip())[:80].strip(" .") or "audio"
    return f"{stem}-{label}.{extension}"
