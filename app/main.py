"""FastAPI-приложение Ozon Ads Manager.

Запуск:  python run.py
Документация API: http://127.0.0.1:8000/docs
"""
from contextlib import asynccontextmanager
import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.config import settings, BASE_DIR
from app.database import init_db
from app.routers import analyzer, api, pages, plugin

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    # Планировщик запускается только в основном процессе (не под --reload workers)
    try:
        from app import jobs
        jobs.start_scheduler()
    except Exception as e:  # не роняем приложение из-за планировщика
        print(f"[scheduler] не удалось запустить: {e}")
    yield
    try:
        from app import jobs
        jobs.stop_scheduler()
    except Exception:
        pass


app = FastAPI(
    title=settings.app_name,
    lifespan=lifespan,
)

# SessionMiddleware для flash-сообщений и хранения данных в подписанной cookie
app.add_middleware(SessionMiddleware, secret_key=settings.secret_key)

app.mount("/static", StaticFiles(directory=str(BASE_DIR / "app" / "static")), name="static")

# Загруженные фото (для поиска по картинке)
_uploads_dir = BASE_DIR / "data" / "uploads"
_uploads_dir.mkdir(parents=True, exist_ok=True)
app.mount("/static-uploads", StaticFiles(directory=str(_uploads_dir)), name="static-uploads")

app.include_router(pages.router)
app.include_router(api.router)
app.include_router(analyzer.router)
app.include_router(plugin.router, prefix="/api")
