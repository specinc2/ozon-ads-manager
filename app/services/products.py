"""Товары кампании и изменение ставок (в т.ч. массовое)."""
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Campaign, Product
from app.services.ozon_client import OzonClient, OzonAPIError


async def sync_products(db: AsyncSession, campaign: Campaign, client: OzonClient) -> int:
    """Обновляет кэш товаров кампании из Ozon. Возвращает количество товаров."""
    raw_items = await client.get_products(campaign.campaign_id)
    now = datetime.utcnow()
    seen: set[str] = set()
    for item in raw_items:
        sku = str(item.get("sku") or item.get("id") or "")
        if not sku:
            continue
        seen.add(sku)
        existing = (await db.execute(
            select(Product).where(Product.campaign_id == campaign.id, Product.sku == sku)
        )).scalar_one_or_none()
        if existing is None:
            existing = Product(campaign_id=campaign.id, sku=sku)
            db.add(existing)
        existing.name = item.get("name") or item.get("title") or existing.name or f"Товар {sku}"
        existing.bid = float(item.get("bid") or item.get("price") or 0)
        existing.impressions = int(item.get("impressions") or 0)
        existing.clicks = int(item.get("clicks") or 0)
        existing.orders = int(item.get("orders") or 0)
        existing.spend = float(item.get("spend") or 0)
        existing.last_synced_at = now
    await db.commit()
    return len(seen)


async def get_products(db: AsyncSession, campaign_pk: int) -> list[Product]:
    result = await db.execute(
        select(Product).where(Product.campaign_id == campaign_pk).order_by(Product.bid.desc())
    )
    return list(result.scalars().all())


async def update_bids(
    db: AsyncSession, campaign: Campaign, client: OzonClient, items: list[dict],
) -> int:
    """Отправляет новые ставки в Ozon и обновляет кэш. Возвращает число товаров."""
    # items: [{"sku": ..., "bid": ...}]
    if not items:
        return 0
    await client.update_products_bids(campaign.campaign_id, items)
    now = datetime.utcnow()
    for item in items:
        sku = str(item["sku"])
        existing = (await db.execute(
            select(Product).where(Product.campaign_id == campaign.id, Product.sku == sku)
        )).scalar_one_or_none()
        if existing is None:
            existing = Product(campaign_id=campaign.id, sku=sku)
            db.add(existing)
        existing.bid = float(item["bid"])
        existing.last_synced_at = now
    await db.commit()
    return len(items)
