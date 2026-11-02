"""
全局配置。先用简单的模块级常量，接环境变量时改成 pydantic-settings 也不影响调用方。
"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
AUDIO_DIR = DATA_DIR / "audio"
DB_PATH = DATA_DIR / "cleancut.db"

DATABASE_URL = f"sqlite:///{DB_PATH}"

# CORS：本地开发时前端跑在 vite 默认端口
CORS_ORIGINS = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
]

AUDIO_DIR.mkdir(parents=True, exist_ok=True)

# Local CPU transcription. Model files are downloaded once; audio is never uploaded.
ASR_MODEL = os.environ.get("CLEANCUT_ASR_MODEL", "base")
ASR_MODEL_DIR = Path(os.environ.get("CLEANCUT_ASR_MODEL_DIR", str(DATA_DIR / "models")))
ASR_LOCAL_FILES_ONLY = os.environ.get("CLEANCUT_ASR_OFFLINE", "0") == "1"
