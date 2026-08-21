"""FastAPI-приложение Ozon Ads Manager.

Запуск:  python run.py
Документация API: http://127.0.0.1:8000/docs
"""
from contextlib import asynccontextmanager
import logging
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware

from app.config import settings, BASE_DIR
from app.database import init_db
from app.routers import analyzer, api, pages

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)


class CSRFMiddleware(BaseHTTPMiddleware):
    """Защита от CSRF: проверяет Origin/Referer для мутирующих запросов.

    Браузер всегда шлёт Origin (fetch) или Referer (форма). Если запрос пришёл
    с чужого сайта — Origin/Referer будет чужим, и мы его отклоняем.
    Запросы без Origin/Referer (curl, внутренние) пропускаем.
    """

    def _is_allowed(self, request) -> bool:
        origin = request.headers.get("origin") or ""
        referer = request.headers.get("referer") or ""
        if not origin and not referer:
            return True  # нет Origin/Referer — curl/скрипт, не браузер
        for value in (origin, referer):
            if not value:
                continue
            try:
                host = urlparse(value).netloc
            except Exception:
                continue
            if not host:
                continue
            if host in settings.csrf_allowed_hosts or host.endswith(".dungeonverse.ru"):
                return True
        return False

    async def dispatch(self, request, call_next):
        if request.method in ("POST", "PUT", "DELETE", "PATCH"):
            if not self._is_allowed(request):
                return JSONResponse({"error": "CSRF check failed"}, status_code=403)
        return await call_next(request)


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
# CSRF-защита: отклоняет POST/PUT/DELETE с чужого Origin/Referer
app.add_middleware(CSRFMiddleware)

app.mount("/static", StaticFiles(directory=str(BASE_DIR / "app" / "static")), name="static")

# Загруженные фото (для поиска по картинке)
_uploads_dir = BASE_DIR / "data" / "uploads"
_uploads_dir.mkdir(parents=True, exist_ok=True)
app.mount("/static-uploads", StaticFiles(directory=str(_uploads_dir)), name="static-uploads")

app.include_router(pages.router)
app.include_router(api.router)
app.include_router(analyzer.router)
