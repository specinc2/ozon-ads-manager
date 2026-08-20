"""Сервисный слой: работа с кампаниями Ozon и кэшем в БД."""
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ApiKey, Campaign
from app.security import decrypt_value
from app.services.ozon_client import OzonClient, OzonAPIError, OzonAuthError, normalize_campaign


async def get_active_ozon_client(db: AsyncSession, user_id: int) -> OzonClient:
    """Возвращает клиент для активных ключей пользователя или бросает OzonAuthError."""
    result = await db.execute(
        select(ApiKey).where(ApiKey.user_id == user_id, ApiKey.is_active.is_(True)).limit(1)
    )
    key = result.scalar_one_or_none()
    if key is None:
        raise OzonAuthError("API-ключи не подключены. Добавьте их в разделе «Настройки».")
    return OzonClient(
        client_id=decrypt_value(key.client_id_enc),
        client_secret=decrypt_value(key.client_secret_enc),
    )


async def sync_campaigns(db: AsyncSession, user_id: int, client: OzonClient) -> int:
    """Синхронизирует список кампаний из Ozon в кэш. Возвращает число обновлённых."""
    raw_list = await client.list_campaigns()
    updated = 0
    now = datetime.utcnow()
    seen_ids: set[str] = set()

    for raw in raw_list:
        norm = normalize_campaign(raw)
        cid = norm["campaign_id"]
        if not cid:
            continue
        seen_ids.add(cid)

        existing = (await db.execute(
            select(Campaign).where(
                Campaign.user_id == user_id,
                Campaign.campaign_id == cid,
            )
        )).scalar_one_or_none()

        if existing is None:
            existing = Campaign(user_id=user_id, campaign_id=cid)
            db.add(existing)
        for field, value in norm.items():
            if value is not None:
                setattr(existing, field, value)
        existing.last_synced_at = now
        updated += 1

    # Отмечаем удалённые (пропавшие из Ozon) кампании как ARCHIVED
    if seen_ids:
        result = await db.execute(
            select(Campaign).where(
                Campaign.user_id == user_id,
                Campaign.campaign_id.notin_(seen_ids),
                Campaign.status.in_(["RUNNING", "STOPPED", "INACTIVE", "PLANNED"]),
            )
        )
        for c in result.scalars().all():
            c.status = "ARCHIVED"

    await db.commit()
    return updated


async def get_campaigns(db: AsyncSession, user_id: int, *, status: str | None = None,
                        campaign_type: str | None = None) -> list[Campaign]:
    """Список кампаний пользователя с фильтрами."""
    stmt = select(Campaign).where(Campaign.user_id == user_id)
    if status:
        stmt = stmt.where(Campaign.status == status)
    if campaign_type:
        stmt = stmt.where(Campaign.campaign_type == campaign_type)
    stmt = stmt.order_by(Campaign.title)
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def get_campaign_or_none(db: AsyncSession, user_id: int, campaign_pk: int) -> Campaign | None:
    result = await db.execute(
        select(Campaign).where(Campaign.user_id == user_id, Campaign.id == campaign_pk)
    )
    return result.scalar_one_or_none()
