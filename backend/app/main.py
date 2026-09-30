"""应用入口：装配路由与本地前端静态资源。"""
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from . import db
from .api import router

app = FastAPI(title="课堂复习 Agent", docs_url="/api/docs", redoc_url=None)

# 仅用于开发期 Vite(5173) 跨端口访问；生产形态为同源静态托管
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)


@app.on_event("startup")
def _startup() -> None:
    db.init_db()


_DIST = Path(__file__).resolve().parent.parent.parent / "frontend" / "dist"
if _DIST.exists():
    app.mount("/", StaticFiles(directory=_DIST, html=True), name="frontend")
else:
    @app.get("/")
    def _root():
        return {"app": "课堂复习 Agent", "hint": "前端未构建：见 frontend/，或使用 npm run build 后重启"}
