"""Сбор и агрегация статистики Ozon в кэш БД."""
from datetime import date, datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Campaign, CampaignStat
from app.services.ozon_client import OzonAPIError, OzonClient, parse_decimal, _int


async def collect_statistics(
    db: AsyncSession, user_id: int, client: OzonClient, days: int = 30,
) -> int:
    """Собирает статистику за последние `days` дней по всем кампаниям пользователя.

    Две части:
    1. Синхронный дневной отчёт GET /statistics/daily/json — показы/клики/расход
       /заказы по дням (для графиков).
    2. Сводный отчёт GET /statistics/campaign/product/json — ДРР, добавления
       в корзину, средняя ставка за период (для карточек и списка).
    Возвращает количество сохранённых дневных записей.
    """
    result = await db.execute(select(Campaign).where(Campaign.user_id == user_id))
    campaigns = list(result.scalars().all())
    if not campaigns:
        return 0

    date_to = date.today()
    date_from = date_to - timedelta(days=days)

    campaign_ids = [c.campaign_id for c in campaigns]
    # Дневной отчёт — порциями, чтобы не упереться в лимит API
    BATCH = 20
    saved = 0
    for i in range(0, len(campaign_ids), BATCH):
        batch_ids = campaign_ids[i:i + BATCH]
        rows = await client.get_daily_statistics(batch_ids, date_from.isoformat(), date_to.isoformat())
        saved += await _save_stat_rows(db, campaigns, rows)

    # Сводные метрики (ДРР, корзина, средняя ставка) — одной порцией
    await collect_campaign_summary(db, campaigns, client, date_from, date_to)

    # Обновляем расход кампаний из последних данных статистики
    await _update_spent_from_stats(db, campaigns)
    return saved


async def collect_campaign_summary(
    db: AsyncSession, campaigns: list[Campaign], client: OzonClient,
    date_from: date, date_to: date,
) -> int:
    """Обновляет сводные метрики кампаний из отчёта campaign/product.

    Возвращает количество обновлённых кампаний.
    """
    try:
        rows = await client.get_campaign_summary_report(
            date_from.isoformat(), date_to.isoformat(),
            [c.campaign_id for c in campaigns],
        )
    except OzonAPIError:
        return 0  # отчёт доступен не для всех типов кампаний — не критично

    id_to_campaign = {c.campaign_id: c for c in campaigns}
    updated = 0
    for row in rows:
        campaign = id_to_campaign.get(str(row.get("id") or ""))
        if campaign is None:
            continue
        campaign.drr = parse_decimal(row.get("drr"))
        campaign.to_cart = _int(row.get("toCart"))
        campaign.avg_click_price = parse_decimal(row.get("clickPrice"))
        if row.get("weeklyBudget") not in (None, "", "0,00", "0"):
            campaign.weekly_budget = parse_decimal(row.get("weeklyBudget"))
        updated += 1
    await db.commit()
    return updated


async def _save_stat_rows(db: AsyncSession, campaigns: list[Campaign], rows: list[dict]) -> int:
    """Сохраняет строки отчёта в кэш campaign_stats."""
    from app.services.ozon_client import normalize_daily_stat_row

    id_to_campaign = {c.campaign_id: c for c in campaigns}
    saved = 0
    for row in rows:
        norm = normalize_daily_stat_row(row)
        campaign = id_to_campaign.get(norm["campaign_id"])
        if campaign is None or norm["date"] is None:
            continue
        existing = (await db.execute(
            select(CampaignStat).where(
                CampaignStat.campaign_id == campaign.id,
                CampaignStat.stat_date == norm["date"],
            )
        )).scalar_one_or_none()
        if existing is None:
            existing = CampaignStat(campaign_id=campaign.id, stat_date=norm["date"])
            db.add(existing)
        for field, value in norm.items():
            if field in ("campaign_id", "date"):
                continue
            setattr(existing, field, value)
        saved += 1
    await db.commit()
    return saved


async def _update_spent_from_stats(db: AsyncSession, campaigns: list[Campaign]) -> None:
    """Переносит последний известный расход из статистики в карточку кампании."""
    for campaign in campaigns:
        result = await db.execute(
            select(func.coalesce(func.sum(CampaignStat.spend), 0)).where(
                CampaignStat.campaign_id == campaign.id
            )
        )
        campaign.spent = float(result.scalar() or 0)
    await db.commit()


async def get_stats_for_period(
    db: AsyncSession, user_id: int, campaign_pk: int | None,
    date_from: date, date_to: date,
) -> list[CampaignStat]:
    """Статистика за период по одной кампании или по всем кампаниям пользователя."""
    stmt = select(CampaignStat).join(Campaign).where(
        Campaign.user_id == user_id,
        CampaignStat.stat_date >= date_from,
        CampaignStat.stat_date <= date_to,
    )
    if campaign_pk is not None:
        stmt = stmt.where(CampaignStat.campaign_id == campaign_pk)
    stmt = stmt.order_by(CampaignStat.stat_date)
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def get_aggregated_stats(
    db: AsyncSession, user_id: int, campaign_pk: int | None,
    date_from: date, date_to: date,
) -> dict:
    """Суммарные показатели за период (для дашборда и карточки кампании)."""
    rows = await get_stats_for_period(db, user_id, campaign_pk, date_from, date_to)
    agg = {
        "impressions": 0, "clicks": 0, "orders": 0,
        "revenue": 0.0, "spend": 0.0, "days": len(rows),
    }
    for r in rows:
        agg["impressions"] += r.impressions
        agg["clicks"] += r.clicks
        agg["orders"] += r.orders
        agg["revenue"] += r.revenue
        agg["spend"] += r.spend

    agg["ctr"] = round(agg["clicks"] / agg["impressions"] * 100, 2) if agg["impressions"] else 0.0
    agg["conversion"] = round(agg["orders"] / agg["clicks"] * 100, 2) if agg["clicks"] else 0.0
    agg["cpa"] = round(agg["spend"] / agg["orders"], 2) if agg["orders"] else 0.0
    agg["romi"] = round(agg["revenue"] / agg["spend"], 2) if agg["spend"] else 0.0
    return agg


async def delete_stats_before(db: AsyncSession, campaign_pk: int, cutoff: date) -> None:
    """Очищает устаревший кэш статистики (чтобы не раздувать БД)."""
    await db.execute(
        delete(CampaignStat).where(
            CampaignStat.campaign_id == campaign_pk,
            CampaignStat.stat_date < cutoff,
        )
    )
    await db.commit()
