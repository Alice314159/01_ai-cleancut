"""
全局配置。先用简单的模块级常量，接环境变量时改成 pydantic-settings 也不影响调用方。
"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("CLEANCUT_DATA_DIR", str(BASE_DIR / "data"))).expanduser()
AUDIO_DIR = DATA_DIR / "audio"
DB_PATH = DATA_DIR / "cleancut.db"

DATABASE_URL = f"sqlite:///{DB_PATH}"

# CORS：本地开发时前端跑在 vite 默认端口
# 前端换端口或用局域网地址访问时，用逗号分隔的 CLEANCUT_CORS_ORIGINS 覆盖。
_DEFAULT_CORS_ORIGINS = "http://localhost:5173,http://127.0.0.1:5173"
CORS_ORIGINS = [
    origin.strip()
    for origin in os.environ.get("CLEANCUT_CORS_ORIGINS", _DEFAULT_CORS_ORIGINS).split(",")
    if origin.strip()
]

AUDIO_DIR.mkdir(parents=True, exist_ok=True)

# Local CPU transcription. Model files are downloaded once; audio is never uploaded.
ASR_MODEL = os.environ.get("CLEANCUT_ASR_MODEL", "base")
ASR_MODEL_DIR = Path(os.environ.get("CLEANCUT_ASR_MODEL_DIR", str(DATA_DIR / "models")))
ASR_LOCAL_FILES_ONLY = os.environ.get("CLEANCUT_ASR_OFFLINE", "0") == "1"

# 录制中实时提示（可选）：用较小的模型每隔一段时间转写最近一段，提示重复/口吃/语气词
LIVE_HINT_MODEL = os.environ.get("CLEANCUT_LIVE_HINT_MODEL", "tiny")

# 说话人区分（可选）：sherpa-onnx + 分段模型 + 声纹模型，全部在本机 CPU 上运行。
# 模型用 scripts/prepare_diarization.py 下载到这个目录；没装或没下载时退回音高判断。
DIARIZATION_MODEL_DIR = Path(os.environ.get("CLEANCUT_DIARIZATION_DIR", str(ASR_MODEL_DIR / "diarization"))).expanduser()
# 聚类阈值：越小越容易把同一个人拆成两人，越大越容易把两个人并成一人。
DIARIZATION_THRESHOLD = float(os.environ.get("CLEANCUT_DIARIZATION_THRESHOLD", "0.5"))
