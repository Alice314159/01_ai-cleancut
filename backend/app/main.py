from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import CORS_ORIGINS
from app.db import init_db
from app.routers import chapters, enhancement, recording_ws, review, sessions, transcription


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    from app.services.transcription import recover_interrupted_jobs
    recover_interrupted_jobs()
    from app.services import render_transcription
    render_transcription.recover_interrupted_jobs()
    from app.services.stub_pipeline import recover_interrupted_enhancements, recover_interrupted_finalizations
    recover_interrupted_enhancements()
    recover_interrupted_finalizations()
    from app.routers.recording_ws import recover_reconnecting_sessions
    recover_reconnecting_sessions()
    yield


app = FastAPI(title="AI CleanCut API", version="2.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    # 前端用 fetch 下载导出文件时需要读到服务端给的文件名。
    expose_headers=["Content-Disposition"],
)

app.include_router(sessions.router)
app.include_router(enhancement.router)
app.include_router(review.router)
app.include_router(transcription.router)
app.include_router(recording_ws.router)
app.include_router(chapters.router)


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/api/capabilities")
def capabilities():
    """前端据此决定是否提供依赖本机转写的功能（如录制中实时提示）。"""
    from app.services import live_hints, transcription
    return {"transcription": transcription.dependency_available(), "liveHints": live_hints.available()}
