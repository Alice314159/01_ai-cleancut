"""
下载说话人区分模型（只下载模型，不上传任何音频）。

    pip install -r requirements-diarization.txt
    python scripts/prepare_diarization.py

模型放到 CLEANCUT_DIARIZATION_DIR（默认 data/models/diarization）：
- segmentation.onnx：pyannote segmentation 3.0（MIT 许可），找出谁在什么时候说话；
- embedding.onnx：3D-Speaker ERes2Net 中文 16 kHz 声纹模型（Apache-2.0），区分不同的人。
下载后校验 SHA-256，不一致就不使用。无法访问 GitHub 时，可以在别的机器下载后把两个文件拷进该目录。
"""
from pathlib import Path
import hashlib
import io
import os
import sys
import tarfile
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import config  # noqa: E402

BASE = "https://github.com/k2-fsa/sherpa-onnx/releases/download"
SEGMENTATION_URL = f"{BASE}/speaker-segmentation-models/sherpa-onnx-pyannote-segmentation-3-0.tar.bz2"
SEGMENTATION_MEMBER = "sherpa-onnx-pyannote-segmentation-3-0/model.onnx"
SEGMENTATION_SHA256 = "220ad67ca923bef2fa91f2390c786097bf305bceb5e261d4af67b38e938e1079"
EMBEDDING_URL = f"{BASE}/speaker-recongition-models/3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx"
EMBEDDING_SHA256 = "f1ee61be04000b50392a63442e5dd9feaea7e81dddd494d3647cf1c3d88a1498"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fetch(url: str) -> bytes:
    print(f"下载 {url}", flush=True)
    with urllib.request.urlopen(url, timeout=120) as response:
        return response.read()


def install(path: Path, data: bytes, expected: str) -> None:
    actual = sha256(data)
    if actual != expected:
        raise SystemExit(f"{path.name} 校验失败（{actual}），文件可能被篡改或下载不完整，未保存")
    temp = path.with_suffix(".part")
    temp.write_bytes(data)
    os.replace(temp, path)
    print(f"已保存 {path}（{len(data) / 1e6:.1f} MB）", flush=True)


def already(path: Path, expected: str) -> bool:
    return path.is_file() and sha256(path.read_bytes()) == expected


def main() -> None:
    target = config.DIARIZATION_MODEL_DIR
    target.mkdir(parents=True, exist_ok=True)
    segmentation, embedding = target / "segmentation.onnx", target / "embedding.onnx"
    if not already(segmentation, SEGMENTATION_SHA256):
        with tarfile.open(fileobj=io.BytesIO(fetch(SEGMENTATION_URL)), mode="r:bz2") as archive:
            member = archive.extractfile(SEGMENTATION_MEMBER)   # 只取模型文件，不解压其它内容
            if member is None:
                raise SystemExit("压缩包里没有找到分段模型")
            install(segmentation, member.read(), SEGMENTATION_SHA256)
    if not already(embedding, EMBEDDING_SHA256):
        install(embedding, fetch(EMBEDDING_URL), EMBEDDING_SHA256)
    try:
        import sherpa_onnx  # noqa: F401
    except ImportError:
        print("模型已就绪，但还没安装 sherpa-onnx：pip install -r requirements-diarization.txt")
        return
    print(f"说话人区分已就绪：{target}")


if __name__ == "__main__":
    main()
