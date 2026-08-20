"""Конфигурация приложения из переменных окружения (.env)."""
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """Все настройки читаются из .env или переменных окружения."""

    model_config = SettingsConfigDict(
        env_file=str(BASE_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "Ozon Ads Manager"
    secret_key: str = "dev-secret-key-change-me"
    encryption_key: str = ""  # Fernet key; если пуст — будет сгенерирован в runtime
    debug: bool = True
    database_url: str = "sqlite+aiosqlite:///./data/ozon_ads.db"
    max_campaigns: int = 100
    scheduler_interval_minutes: int = 8
    host: str = "127.0.0.1"
    port: int = 8000

    # Ozon Performance API
    ozon_base_url: str = "https://api-performance.ozon.ru"


settings = Settings()
