"""Download/cache the configured local model without uploading any audio."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import config
from app.services.transcription import get_model

if __name__ == '__main__':
    print(f'Preparing local ASR model: {config.ASR_MODEL}', flush=True)
    get_model(config.ASR_MODEL, str(config.ASR_MODEL_DIR), config.ASR_LOCAL_FILES_ONLY)
    print(f'Model ready in {config.ASR_MODEL_DIR}', flush=True)
