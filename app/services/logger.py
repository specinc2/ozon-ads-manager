"""Журналирование действий и запросов API."""
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ActionLog, ApiLog


async def log_action(
    db: AsyncSession,
    *,
    action: str,
    user_id: int | None = None,
    entity_type: str = "",
    entity_name: str = "",
    details: dict | None = None,
    source: str = "manual",
) -> None:
    """Записывает действие в журнал."""
    db.add(ActionLog(
        user_id=user_id,
        action=action,
        entity_type=entity_type,
        entity_name=entity_name,
        details=details or {},
        source=source,
        ts=datetime.utcnow(),
    ))
    await db.commit()


async def log_api_call(
    db: AsyncSession,
    *,
    endpoint: str,
    method: str,
    status: int | None = None,
    error: str = "",
    user_id: int | None = None,
) -> None:
    """Записывает запрос к Ozon API (без секретов)."""
    db.add(ApiLog(
        user_id=user_id,
        endpoint=endpoint,
        method=method,
        status=status,
        error=error[:2000],
        ts=datetime.utcnow(),
    ))
    await db.commit()
