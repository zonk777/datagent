import time
from collections import defaultdict
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .config import get_settings
from .db import initialize_database
from .routers import agent, audit, auth, datasets, knowledge, mcp, system
from .services.auth import SESSION_COOKIE, admin_from_session
from .services.vector_store import VectorStoreError, sync_knowledge

# ---- in-memory rate limiter ----
_rate_window = 60
_rate_limits: dict[str, int] = {
    "auth/login": 5,
    "agent/chat": 30,
    "datasets/upload/chunk": 600,
    "datasets/upload/chunks": 240,
    "datasets/upload/complete": 60,
    "datasets/upload": 30,
}
_rate_buckets: dict[str, list[float]] = defaultdict(list)


@asynccontextmanager
async def lifespan(_: FastAPI):
    initialize_database()
    try:
        await sync_knowledge()
    except VectorStoreError:
        # 向量服务不可用时不阻止 API 启动，检索自动降级为关键词模式。
        pass
    yield


settings = get_settings()
app = FastAPI(
    title="DataAgent API",
    version="0.1.0",
    description="企业数据智能体服务系统 API",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.frontend_origin],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


PUBLIC_PATHS = {
    "/",
    "/api/v1/health",
    "/api/v1/auth/login",
    "/api/v1/mcp",
}


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For")
    return (forwarded or "").split(",")[0].strip() or (request.client.host if request.client else "unknown")


@app.middleware("http")
async def rate_limit(request: Request, call_next):
    path = request.url.path
    bucket_key = None
    for pattern, limit in sorted(_rate_limits.items(), key=lambda item: len(item[0]), reverse=True):
        if f"/{pattern}" in path or path.endswith(f"/{pattern}"):
            bucket_key = pattern
            break
    if bucket_key:
        now = time.time()
        ip = _client_ip(request)
        full_key = f"{ip}:{bucket_key}"
        bucket = _rate_buckets[full_key]
        bucket[:] = [t for t in bucket if now - t < _rate_window]
        if len(bucket) >= _rate_limits[bucket_key]:
            return JSONResponse({"detail": "请求过于频繁，请稍后再试"}, status_code=429)
        bucket.append(now)
    return await call_next(request)


@app.middleware("http")
async def require_login(request, call_next):
    path = request.url.path
    if (
        request.method == "OPTIONS"
        or path in PUBLIC_PATHS
        or path.startswith("/docs")
        or path.startswith("/redoc")
        or path == "/openapi.json"
    ):
        return await call_next(request)
    if path.startswith("/api/v1"):
        admin = admin_from_session(request.cookies.get(SESSION_COOKIE))
        if not admin:
            return JSONResponse({"detail": "请先登录"}, status_code=401)
        request.state.admin = admin
    return await call_next(request)

app.include_router(system.router, prefix="/api/v1")
app.include_router(datasets.router, prefix="/api/v1")
app.include_router(knowledge.router, prefix="/api/v1")
app.include_router(agent.router, prefix="/api/v1")
app.include_router(auth.router, prefix="/api/v1")
app.include_router(audit.router, prefix="/api/v1")
app.include_router(mcp.router, prefix="/api/v1")


@app.get("/")
def root() -> dict:
    return {"name": settings.app_name, "docs": "/docs", "health": "/api/v1/health"}
