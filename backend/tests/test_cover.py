"""节目封面：上传校验与规范化、读取、删除、写入 MP3 / M4A、换封面后导出更新。"""
import json
import subprocess

import pytest

from tests.test_audio_export import cached_exports
from tests.test_rendering import client, recording  # noqa: F401  复用 fixture


def make_image(tmp_path, name, size="1600x900", codec_args=()):
    path = tmp_path / name
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"color=c=orange:s={size}", "-frames:v", "1",
                    *codec_args, str(path)], check=True)
    return path.read_bytes()


def probe(data, tmp_path, suffix):
    path = tmp_path / f"probe.{suffix}"
    path.write_bytes(data)
    return json.loads(subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(path)],
                                     capture_output=True, check=True).stdout)["streams"]


def test_upload_normalizes_to_bounded_jpeg(client, recording, tmp_path):
    session_id = recording[0]
    assert client.get(f"/api/sessions/{session_id}").json()["coverVersion"] is None
    png = make_image(tmp_path, "c.png", "2000x1000")
    response = client.put(f"/api/sessions/{session_id}/cover", files={"file": ("c.png", png, "image/png")})
    assert response.status_code == 200, response.text
    version = response.json()["coverVersion"]
    assert version
    image = client.get(f"/api/sessions/{session_id}/cover")
    assert image.headers["content-type"] == "image/jpeg"
    [stream] = probe(image.content, tmp_path, "jpg")
    assert (stream["width"], stream["height"]) == (1400, 700)

    # 换封面：版本号变化，旧文件被清理
    jpg = make_image(tmp_path, "d.jpg", "600x600")
    second = client.put(f"/api/sessions/{session_id}/cover", files={"file": ("d.jpg", jpg, "image/jpeg")}).json()
    assert second["coverVersion"] != version
    [stream] = probe(client.get(f"/api/sessions/{session_id}/cover").content, tmp_path, "jpg")
    assert (stream["width"], stream["height"]) == (600, 600)

    deleted = client.delete(f"/api/sessions/{session_id}/cover")
    assert deleted.json()["coverVersion"] is None
    assert client.get(f"/api/sessions/{session_id}/cover").status_code == 404


@pytest.mark.parametrize("payload,detail", [
    (b"", "为空"),
    (b"GIF89a....", "仅支持"),
    (b"\xff\xd8\xff" + b"0" * (5 * 1024 * 1024), "5MB"),
    (b"\x89PNG\r\n\x1a\n" + b"broken", "无法读取"),
])
def test_rejects_bad_uploads(client, recording, payload, detail):
    response = client.put(f"/api/sessions/{recording[0]}/cover", files={"file": ("x", payload, "image/png")})
    assert response.status_code == 422
    assert detail in response.json()["detail"]
    assert client.get(f"/api/sessions/{recording[0]}").json()["coverVersion"] is None


@pytest.mark.parametrize("fmt", ["mp3", "m4a"])
def test_cover_embedded_in_exports_and_cache_follows_cover(client, recording, tmp_path, fmt):
    session_id, source_id = recording[0], recording[1]
    url = f"/api/sessions/{session_id}/versions/{source_id}/export?format={fmt}"
    plain = client.get(url).content
    assert not [s for s in probe(plain, tmp_path, fmt) if s["codec_type"] == "video"]

    client.put(f"/api/sessions/{session_id}/cover", files={"file": ("c.png", make_image(tmp_path, "c.png", "800x800"), "image/png")})
    with_cover = client.get(url)
    assert with_cover.status_code == 200, with_cover.text
    pictures = [s for s in probe(with_cover.content, tmp_path, fmt) if s["codec_type"] == "video"]
    assert len(pictures) == 1 and pictures[0]["disposition"]["attached_pic"] == 1
    assert len(cached_exports(session_id, source_id, fmt)) == 1  # 旧的无封面缓存已清理

    client.delete(f"/api/sessions/{session_id}/cover")
    again = client.get(url).content
    assert not [s for s in probe(again, tmp_path, fmt) if s["codec_type"] == "video"]
