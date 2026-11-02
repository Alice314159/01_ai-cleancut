"""章节：CRUD、换算到成片时间轴、写入 MP3/M4A、随会话删除。"""
import json
import subprocess
from types import SimpleNamespace as NS

import pytest

from app.models import Chapter
from app.services import chapters as service
from tests.test_audio_export import cached_exports
from tests.test_rendering import client, recording  # noqa: F401  复用 fixture


def ch(start, title, id=None):
    return NS(id=id or f"c{start}", start_ms=start, title=title)


# ---------- 纯换算 ----------

def test_map_time_through_cuts_and_replacements():
    edits = [(1000, 2000, 0), (5000, 6000, 3000)]  # 删 1 秒；1 秒重录成 3 秒
    assert service.map_time(500, edits) == (500, False)
    assert service.map_time(1500, edits) == (1000, True)    # 落在删除区间里 → 挪到区间位置
    assert service.map_time(2000, edits) == (1000, False)   # 正好在删除区间之后
    assert service.map_time(5500, edits) == (4000, True)    # 落在重录里 → 新录音开头
    assert service.map_time(7000, edits) == (8000, False)   # 重录后 +2 秒，此前 -1 秒


def test_mapped_chapters_dedupe_drop_overflow_and_get_ends():
    output = NS(cut_ranges=[{"start_ms": 1000, "end_ms": 3000}], decisions=[])
    mapped = service.map_to_render([ch(0, "开场"), ch(1200, "被剪掉"), ch(3000, "正题"), ch(9500, "尾声")], output, 7000)
    assert [(c.title, c.start_ms, c.end_ms) for c in mapped] == [("开场", 0, 1000), ("正题", 1000, 7000)]


def test_text_and_ffmetadata_formats():
    mapped = service.unmapped([ch(0, "开场"), ch(65_000, "a=b; #1")], 120_000)
    assert service.as_text(mapped) == "00:00 开场\n01:05 a=b; #1\n"
    meta = service.ffmetadata(mapped, "第一期")
    assert meta.startswith(";FFMETADATA1\ntitle=第一期\n")
    assert "START=65000\nEND=120000\ntitle=a\\=b\; \\#1" in meta
    long = service.unmapped([ch(0, "a"), ch(3_700_000, "b")], 4_000_000)
    assert service.as_text(long) == "0:00:00 a\n1:01:40 b\n"


# ---------- REST ----------

def test_crud_validation_and_ordering(client, recording):
    url = f"/api/sessions/{recording[0]}/chapters"
    b = client.post(url, json={"startMs": 2000, "title": "  第二部分  "})
    a = client.post(url, json={"startMs": 0, "title": "开场"})
    assert b.status_code == a.status_code == 201
    assert b.json()["title"] == "第二部分"
    assert [c["title"] for c in client.get(url).json()] == ["开场", "第二部分"]
    assert client.post(url, json={"startMs": 2000, "title": "重复"}).status_code == 409
    assert client.post(url, json={"startMs": 3000, "title": "越界"}).status_code == 422
    assert client.post(url, json={"startMs": 100, "title": "   "}).status_code == 422
    patched = client.patch(f"{url}/{b.json()['id']}", json={"title": "正题", "startMs": 1500})
    assert patched.status_code == 200 and patched.json()["startMs"] == 1500
    assert client.patch(f"{url}/{b.json()['id']}", json={"startMs": 0}).status_code == 409
    assert client.delete(f"{url}/{a.json()['id']}").status_code == 204
    assert [c["title"] for c in client.get(url).json()] == ["正题"]
    assert client.delete(f"{url}/missing").status_code == 404


def test_render_chapters_mapped_and_embedded_in_exports(client, recording, tmp_path):
    session_id = recording[0]
    base = f"/api/sessions/{session_id}"
    for start, title in ((0, "开场"), (700, "会被挪动"), (2000, "结尾")):
        client.post(base + "/chapters", json={"startMs": start, "title": title})
    client.post(base + "/decisions", json={"startMs": 500, "endMs": 1000})
    master = client.post(base + "/render").json()["version"]

    mapped = client.get(f"{base}/versions/{master['id']}/chapters").json()
    assert [(c["title"], c["startMs"], c["endMs"], c["moved"]) for c in mapped] == [
        ("开场", 0, 500, False), ("会被挪动", 500, 1500, True), ("结尾", 1500, 2500, False)]
    assert client.get(f"{base}/versions/{master['id']}/chapters?format=txt").text == "00:00 开场\n00:00 会被挪动\n00:01 结尾\n"

    for fmt in ("mp3", "m4a"):
        response = client.get(f"{base}/versions/{master['id']}/export?format={fmt}")
        assert response.status_code == 200, response.text
        out = tmp_path / f"x.{fmt}"
        out.write_bytes(response.content)
        info = json.loads(subprocess.run(["ffprobe", "-v", "error", "-show_chapters", "-of", "json", str(out)],
                                         capture_output=True, check=True).stdout)
        titles = [c["tags"]["title"] for c in info["chapters"]]
        assert titles == ["开场", "会被挪动", "结尾"]
        assert float(info["chapters"][1]["start_time"]) == pytest.approx(0.5, abs=0.01)

    # 改章节后导出重新生成，旧缓存被替换
    first = cached_exports(session_id, master["id"], "mp3")
    client.post(base + "/chapters", json={"startMs": 2500, "title": "彩蛋"})
    client.get(f"{base}/versions/{master['id']}/export?format=mp3")
    second = cached_exports(session_id, master["id"], "mp3")
    assert len(second) == 1 and second != first


def test_original_version_uses_unmapped_chapters_and_session_delete_cleans(client, recording, isolated_backend):
    session_id, source_id = recording[0], recording[1]
    client.post(f"/api/sessions/{session_id}/chapters", json={"startMs": 1200, "title": "中段"})
    mapped = client.get(f"/api/sessions/{session_id}/versions/{source_id}/chapters").json()
    assert [(c["startMs"], c["endMs"]) for c in mapped] == [(1200, 3000)]
    assert client.delete(f"/api/sessions/{session_id}").status_code == 204
    with isolated_backend() as db:
        assert db.query(Chapter).count() == 0
