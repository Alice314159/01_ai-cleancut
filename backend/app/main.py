from contextlib import asynccontextmanager

import logging

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app import access

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

logging.getLogger("uvicorn.access").addFilter(access.RedactTokenFilter())


@app.middleware("http")
async def require_pairing_code(request: Request, call_next):
    """--lan / --app 模式下校验配对码；CORS 预检与健康检查放行。"""
    path = request.url.path
    if access.enabled() and path.startswith("/api") and path not in access.OPEN_PATHS and request.method != "OPTIONS":
        result = access.check(access.presented(request.headers, request.query_params))
        if result != "ok":
            detail = "配对码错误次数过多，请 1 分钟后再试" if result == "locked" else "需要配对码：请输入电脑终端显示的配对码"
            response = JSONResponse({"detail": detail, "code": "pairing_required"}, status_code=429 if result == "locked" else 401)
            # 中间件在 CORS 之前返回时也要带上跨域头，否则浏览器只会报“跨域失败”，看不到需要配对码
            origin = request.headers.get("origin")
            if origin and origin in CORS_ORIGINS:
                response.headers["access-control-allow-origin"] = origin
                response.headers["access-control-allow-credentials"] = "true"
                response.headers["vary"] = "Origin"
            return response
    return await call_next(request)


app.include_router(sessions.router)
app.include_router(enhancement.router)
app.include_router(review.router)
app.include_router(transcription.router)
app.include_router(recording_ws.router)
app.include_router(chapters.router)


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/api/auth/check")
def auth_check():
    """配对码有效（或未启用配对）时返回 200；用于 App 连接页验证地址与配对码。"""
    return {"ok": True, "pairing": access.enabled()}


@app.get("/api/capabilities")
def capabilities():
    """前端据此决定是否提供依赖本机转写的功能（如录制中实时提示）。"""
    from app.services import live_hints, transcription
    return {"transcription": transcription.dependency_available(), "liveHints": live_hints.available()}
