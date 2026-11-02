"""MP3 / M4A 导出：真实 ffmpeg 转码、缓存、文件名与错误路径。"""
import json
import subprocess
from urllib.parse import unquote

import pytest

from app.services import audio_export
from tests.test_rendering import client, recording  # noqa: F401  复用 fixture


def cached_exports(session_id, version_id, fmt):
    folder = audio_export.export_path(session_id, version_id, fmt, 192).parent
    return sorted(folder.glob(f"{version_id}_*.{fmt}"))


def probe(path):
    result = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
                            capture_output=True, check=True)
    return json.loads(result.stdout)


@pytest.mark.parametrize("fmt,codec,media_type", [("mp3", "mp3", "audio/mpeg"), ("m4a", "aac", "audio/mp4")])
def test_export_master_transcodes_with_metadata(client, recording, isolated_backend, tmp_path, fmt, codec, media_type):
    session_id = recording[0]
    client.patch(f"/api/sessions/{session_id}", json={"title": "第一期/访谈"})
    client.post(f"/api/sessions/{session_id}/decisions", json={"startMs": 500, "endMs": 1000})
    master = client.post(f"/api/sessions/{session_id}/render").json()["version"]
    response = client.get(f"/api/sessions/{session_id}/versions/{master['id']}/export?format={fmt}&bitrate=128")
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == media_type
    disposition = unquote(response.headers["content-disposition"])
    assert disposition.startswith("attachment") and f"第一期_访谈-成片.{fmt}" in disposition

    out = tmp_path / f"out.{fmt}"
    out.write_bytes(response.content)
    info = probe(out)
    stream = info["streams"][0]
    assert stream["codec_name"] == codec
    assert stream["channels"] == 2
    assert float(info["format"]["duration"]) == pytest.approx(2.5, abs=0.1)
    assert info["format"]["tags"].get("title") == "第一期/访谈"

    # 第二次请求直接返回缓存，不重新编码
    [cached] = cached_exports(session_id, master["id"], fmt)
    mtime = cached.stat().st_mtime_ns
    assert client.get(f"/api/sessions/{session_id}/versions/{master['id']}/export?format={fmt}&bitrate=128").content == response.content
    assert cached.stat().st_mtime_ns == mtime
    assert not [p for p in cached.parent.iterdir() if p.name.startswith(".")]


def test_export_validates_input(client, recording):
    session_id, source_id = recording[0], recording[1]
    base = f"/api/sessions/{session_id}/versions/{source_id}/export"
    assert client.get(base + "?format=flac").status_code == 422
    assert client.get(base + "?bitrate=100").status_code == 422
    assert client.get(f"/api/sessions/{session_id}/versions/av_missing/export").status_code == 404
    assert client.get(f"/api/sessions/rs_missing/versions/{source_id}/export").status_code == 404
    # 原始版本也可以导出，默认 MP3 192k
    response = client.get(base)
    assert response.status_code == 200
    assert "-原始.mp3" in unquote(response.headers["content-disposition"])


def test_missing_encoder_reports_503(client, recording, monkeypatch):
    monkeypatch.setattr(audio_export, "encoder_available", lambda encoder: False)
    session_id, source_id = recording[0], recording[1]
    response = client.get(f"/api/sessions/{session_id}/versions/{source_id}/export?format=m4a")
    assert response.status_code == 503
    assert "aac" in response.json()["detail"]


def test_export_files_removed_with_session(client, recording):
    session_id, source_id = recording[0], recording[1]
    assert client.get(f"/api/sessions/{session_id}/versions/{source_id}/export").status_code == 200
    [path] = cached_exports(session_id, source_id, "mp3")
    assert client.delete(f"/api/sessions/{session_id}").status_code == 204
    assert not path.exists()


def test_download_name_sanitizes():
    assert audio_export.download_name('a:b*c?', "master", "mp3") == "a_b_c_-成片.mp3"
    assert audio_export.download_name("  ", "denoised", "m4a") == "audio-降噪.m4a"
