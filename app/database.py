"""Подключение к базе данных (SQLAlchemy 2.0, async).

Поддерживается SQLite (по умолчанию) и PostgreSQL через DATABASE_URL.
"""
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import settings


class Base(DeclarativeBase):
    """Базовый класс всех ORM-моделей."""


def _ensure_sqlite_dir() -> None:
    """SQLite не создаёт родительские директории — создаём вручную."""
    if settings.database_url.startswith("sqlite"):
        import os
        from urllib.parse import urlparse

        path = settings.database_url.split("///", 1)[-1]
        if path and path != ":memory:":
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)


_ensure_sqlite_dir()

engine = create_async_engine(settings.database_url, echo=False, future=True)
async_session = async_sessionmaker(engine, expire_on_commit=False)


async def init_db() -> None:
    """Создаёт таблицы, если их ещё нет, и добавляет недостающие колонки."""
    from app import models  # noqa: F401  (регистрация моделей в Base.metadata)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Простая миграция: добавляет новые колонки в существующие таблицы (SQLite)
        if settings.database_url.startswith("sqlite"):
            await _migrate_sqlite(conn)


async def _migrate_sqlite(conn) -> None:
    """Добавляет колонки, появившиеся в моделях после создания таблиц."""
    from sqlalchemy import text

    # Колонки, которые могут отсутствовать в старой схеме: {таблица: {колонка: тип}}
    additions = {
        "campaigns": {
            "weekly_budget": "FLOAT",
            "drr": "FLOAT DEFAULT 0",
            "to_cart": "INTEGER DEFAULT 0",
            "avg_click_price": "FLOAT DEFAULT 0",
        },
        "api_keys": {
            "seller_client_id_enc": "TEXT DEFAULT ''",
            "seller_api_key_enc": "TEXT DEFAULT ''",
        },
        "proxy_settings": {
            "ozon_cookies": "TEXT DEFAULT ''",
            "bd_api_key": "TEXT DEFAULT ''",
            "bd_dataset_id": "TEXT DEFAULT ''",
        },
        "analyzer_history": {
            "photo_urls_json": "TEXT DEFAULT '[]'",
        },
    }
    for table, columns in additions.items():
        result = await conn.execute(text(f"PRAGMA table_info({table})"))
        existing = {row[1] for row in result.all()}
        for name, dtype in columns.items():
            if name not in existing:
                await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {dtype}"))


async def get_db():
    """FastAPI-зависимость: одна сессия на запрос."""
    async with async_session() as session:
        yield session
