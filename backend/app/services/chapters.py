"""
章节：标在原始时间轴上，导出时按某个成片的剪辑快照换算到成片时间轴。

换算规则（与渲染一致）：
- 已接受的删除区间整段移除；起点落在删除区间里的章节挪到该区间在成片中的位置；
- 已接受的重录按新录音的实际时长替换原区间；起点落在其中的章节挪到新录音开头；
- 换算后起点相同的章节只保留最后一个（中间内容被剪光了，后一个章节才是剩下的内容）；
- 超出成片时长的章节丢弃。
"""
from dataclasses import dataclass

from app.models import Chapter, RenderOutput


@dataclass(frozen=True)
class MappedChapter:
    id: str
    title: str
    start_ms: int
    end_ms: int
    original_start_ms: int
    moved: bool


def list_for(db, session_id: str) -> list[Chapter]:
    return (db.query(Chapter).filter_by(session_id=session_id)
            .order_by(Chapter.start_ms, Chapter.created_at_ns).all())


def edits_of(output: RenderOutput) -> list[tuple[int, int, int]]:
    """成片快照里的编辑：(原起点, 原终点, 在成片中占用的时长)。"""
    edits = [(c["start_ms"], c["end_ms"], 0) for c in output.cut_ranges or []]
    for d in output.decisions or []:
        if d.get("action") == "REPLACE_AUDIO" and d.get("accepted") is True and not d.get("hard_protected"):
            edits.append((d["start_ms"], d["end_ms"], int(d.get("replacement_duration_ms") or 0)))
    return sorted(edits)


def map_time(t: int, edits: list[tuple[int, int, int]]) -> tuple[int, bool]:
    """原始时间 → 成片时间；第二个值表示是否因落在编辑区间里而被挪动。"""
    shift = 0
    for start, end, new_length in edits:
        if t < start:
            break
        if t < end:
            return start + shift, t != start
        shift += new_length - (end - start)
    return t + shift, False


def _with_ends(items: list[tuple[str, str, int, int, bool]], duration_ms: int) -> list[MappedChapter]:
    by_start: dict[int, tuple[str, str, int, int, bool]] = {}
    for item in items:
        if 0 <= item[2] < duration_ms:
            by_start[item[2]] = item  # 后者覆盖前者：被剪光的前一个章节不再保留
    ordered = [by_start[k] for k in sorted(by_start)]
    return [MappedChapter(id=i, title=title, start_ms=start,
                          end_ms=ordered[n + 1][2] if n + 1 < len(ordered) else duration_ms,
                          original_start_ms=original, moved=moved)
            for n, (i, title, start, original, moved) in enumerate(ordered)]


def map_to_render(chapters: list[Chapter], output: RenderOutput, duration_ms: int) -> list[MappedChapter]:
    edits = edits_of(output)
    items = []
    for c in chapters:
        start, moved = map_time(c.start_ms, edits)
        items.append((c.id, c.title, start, c.start_ms, moved))
    return _with_ends(items, duration_ms)


def unmapped(chapters: list[Chapter], duration_ms: int) -> list[MappedChapter]:
    """原始 / 降噪版本与原始录音同一时间轴，章节原样使用。"""
    return _with_ends([(c.id, c.title, c.start_ms, c.start_ms, False) for c in chapters], duration_ms)


def _escape(value: str) -> str:
    out = value
    for ch in ("\\", "=", ";", "#", "\n"):
        out = out.replace(ch, "\\" + ch)
    return out


def ffmetadata(chapters: list[MappedChapter], title: str = "") -> str:
    """FFmpeg 元数据文件：全局标题 + 章节（毫秒时间基），供 -map_chapters 写入 MP3/M4A。"""
    lines = [";FFMETADATA1"]
    if title:
        lines.append(f"title={_escape(title)}")
    for c in chapters:
        lines += ["", "[CHAPTER]", "TIMEBASE=1/1000", f"START={c.start_ms}", f"END={c.end_ms}", f"title={_escape(c.title)}"]
    return "\n".join(lines) + "\n"


def timestamp(ms: int, force_hours: bool = False) -> str:
    seconds = ms // 1000
    h, m, s = seconds // 3600, seconds // 60 % 60, seconds % 60
    return f"{h}:{m:02}:{s:02}" if h or force_hours else f"{m:02}:{s:02}"


def as_text(chapters: list[MappedChapter]) -> str:
    """节目简介常用格式："00:00 开场"。超过一小时统一带小时位。"""
    long_form = bool(chapters) and chapters[-1].start_ms >= 3600_000
    return "".join(f"{timestamp(c.start_ms, long_form)} {c.title}\n" for c in chapters)
