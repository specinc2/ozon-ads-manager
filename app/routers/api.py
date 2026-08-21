"""JSON API для AJAX-действий: старт/стоп кампаний, бюджеты, ставки, правила."""
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.deps import require_user
from app.models import (
    AutomationRule,
    BidderRule,
    Campaign,
    CampaignSchedule,
    Notification,
    Product,
    ProductInfo,
    User,
)
from app.schemas import (
    ApiKeyCreate,
    CampaignUpdate,
    ProductsBulkUpdate,
    RuleCreate,
    RuleOut,
    ScheduleCreate,
    ScheduleOut,
)
from app.services.campaigns import get_active_ozon_client, get_campaign_or_none
from app.services.logger import log_action
from app.services.ozon_client import OzonAPIError, OzonAuthError
from app.services.products import sync_products, update_bids
from app.services.statistics import collect_statistics

router = APIRouter(prefix="/api")


# ------------------------------------------------------------------
# Кампании
# ------------------------------------------------------------------

@router.post("/campaigns/{campaign_pk}/start")
async def api_start_campaign(campaign_pk: int, user: User = Depends(require_user), db: AsyncSession = Depends(get_db)):
    campaign = await get_campaign_or_none(db, user.id, campaign_pk)
    if not campaign:
        raise HTTPException(404, "Кампания не найдена")
    client = await get_active_ozon_client(db, user.id)
    try:
        await client.activate_campaign(campaign.campaign_id)
    except OzonAPIError as e:
        raise HTTPException(400, e.message)
    campaign.status = "RUNNING"
    await db.commit()
    await log_action(db, action="campaign_start", user_id=user.id,
                     entity_type="campaign", entity_name=campaign.title)
    return {"ok": True, "status": "RUNNING"}


@router.post("/campaigns/{campaign_pk}/stop")
async def api_stop_campaign(campaign_pk: int, user: User = Depends(require_user), db: AsyncSession = Depends(get_db)):
    campaign = await get_campaign_or_none(db, user.id, campaign_pk)
    if not campaign:
        raise HTTPException(404, "Кампания не найдена")
    client = await get_active_ozon_client(db, user.id)
    try:
        await client.deactivate_campaign(campaign.campaign_id)
    except OzonAPIError as e:
        raise HTTPException(400, e.message)
    campaign.status = "INACTIVE"
    await db.commit()
    await log_action(db, action="campaign_stop", user_id=user.id,
                     entity_type="campaign", entity_name=campaign.title)
    return {"ok": True, "status": "INACTIVE"}


@router.put("/campaigns/{campaign_pk}/budget")
async def api_update_budget(campaign_pk: int, body: CampaignUpdate,
                            user: User = Depends(require_user), db: AsyncSession = Depends(get_db)):
    campaign = await get_campaign_or_none(db, user.id, campaign_pk)
    if not campaign:
        raise HTTPException(404, "Кампания не найдена")
    client = await get_active_ozon_client(db, user.id)
    try:
        await client.update_campaign(
            campaign.campaign_id,
            daily_budget_rub=body.daily_budget,
            total_budget_rub=body.total_budget,
        )
    except OzonAPIError as e:
        raise HTTPException(400, e.message)
    if body.daily_budget is not None:
        campaign.daily_budget = body.daily_budget
    if body.total_budget is not None:
        campaign.total_budget = body.total_budget
    await db.commit()
    await log_action(db, action="budget_change", user_id=user.id,
                     entity_type="campaign", entity_name=campaign.title,
                     details={"daily_budget": body.daily_budget, "total_budget": body.total_budget})
    return {"ok": True}


@router.post("/campaigns/{campaign_pk}/sync")
async def api_sync_campaign(campaign_pk: int, user: User = Depends(require_user), db: AsyncSession = Depends(get_db)):
    campaign = await get_campaign_or_none(db, user.id, campaign_pk)
    if not campaign:
        raise HTTPException(404, "Кампания не найдена")
    client = await get_active_ozon_client(db, user.id)
    try:
        products = await sync_products(db, campaign, client)
    except OzonAPIError:
        # Кампания может не поддерживать товары (REF_BLOGGER, REF_VK и т.п.)
        products = 0
    return {"ok": True, "products_count": products}


# ------------------------------------------------------------------
# Ставки
# ------------------------------------------------------------------

@router.put("/products/bids")
async def api_update_bids(body: ProductsBulkUpdate, user: User = Depends(require_user),
                          db: AsyncSession = Depends(get_db)):
    campaign = await get_campaign_or_none(db, user.id, body.campaign_id)
    if not campaign:
        raise HTTPException(404, "Кампания не найдена")
    client = await get_active_ozon_client(db, user.id)
    items = [{"sku": p.sku, "bid": p.bid} for p in body.products]
    try:
        count = await update_bids(db, campaign, client, items)
    except OzonAPIError as e:
        raise HTTPException(400, e.message)
    await log_action(db, action="bids_update", user_id=user.id,
                     entity_type="campaign", entity_name=campaign.title,
                     details={"products_count": count})
    return {"ok": True, "updated": count}


# ------------------------------------------------------------------
# Авто-правила
# ------------------------------------------------------------------

@router.post("/rules")
async def api_create_rule(body: RuleCreate, user: User = Depends(require_user), db: AsyncSession = Depends(get_db)):
    rule = AutomationRule(
        user_id=user.id,
        name=body.name,
        rule_type=body.rule_type,
        campaign_id=body.campaign_id,
        params=body.params,
        is_active=body.is_active,
    )
    db.add(rule)
    await db.commit()
    await log_action(db, action="rule_create", user_id=user.id,
                     entity_type="rule", entity_name=rule.name)
    return {"ok": True, "id": rule.id}


@router.put("/rules/{rule_id}/toggle")
async def api_toggle_rule(rule_id: int, user: User = Depends(require_user), db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(AutomationRule).where(AutomationRule.id == rule_id, AutomationRule.user_id == user.id)
    )
    rule = result.scalar_one_or_none()
    if not rule:
        raise HTTPException(404, "Правило не найдено")
    rule.is_active = not rule.is_active
    await db.commit()
    return {"ok": True, "is_active": rule.is_active}


@router.delete("/rules/{rule_id}")
async def api_delete_rule(rule_id: int, user: User = Depends(require_user), db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(AutomationRule).where(AutomationRule.id == rule_id, AutomationRule.user_id == user.id)
    )
    rule = result.scalar_one_or_none()
    if not rule:
        raise HTTPException(404, "Правило не найдено")
    await db.delete(rule)
    await db.commit()
    return {"ok": True, "deleted": True}


# ------------------------------------------------------------------
# Расписания
# ------------------------------------------------------------------

@router.post("/schedules")
async def api_create_schedule(body: ScheduleCreate, user: User = Depends(require_user),
                              db: AsyncSession = Depends(get_db)):
    sched = CampaignSchedule(
        user_id=user.id,
        campaign_id=body.campaign_id,
        days_of_week=body.days_of_week,
        time_start=body.time_start,
        time_end=body.time_end,
        timezone=body.timezone,
    )
    db.add(sched)
    await db.commit()
    return {"ok": True, "id": sched.id}


@router.put("/schedules/{schedule_id}/toggle")
async def api_toggle_schedule(schedule_id: int, user: User = Depends(require_user),
                              db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(CampaignSchedule).where(
            CampaignSchedule.id == schedule_id,
            CampaignSchedule.user_id == user.id,
        )
    )
    sched = result.scalar_one_or_none()
    if not sched:
        raise HTTPException(404, "Расписание не найдено")
    sched.is_active = not sched.is_active
    await db.commit()
    return {"ok": True, "is_active": sched.is_active}


@router.delete("/schedules/{schedule_id}")
async def api_delete_schedule(schedule_id: int, user: User = Depends(require_user),
                              db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(CampaignSchedule).where(
            CampaignSchedule.id == schedule_id,
            CampaignSchedule.user_id == user.id,
        )
    )
    sched = result.scalar_one_or_none()
    if not sched:
        raise HTTPException(404, "Расписание не найдено")
    await db.delete(sched)
    await db.commit()
    return {"ok": True, "deleted": True}


# ------------------------------------------------------------------
# Статистика (принудительный сбор)
# ------------------------------------------------------------------

@router.post("/stats/collect")
async def api_collect_stats(user: User = Depends(require_user), db: AsyncSession = Depends(get_db)):
    try:
        client = await get_active_ozon_client(db, user.id)
        count = await collect_statistics(db, user.id, client, days=30)
    except OzonAuthError as e:
        raise HTTPException(400, e.message)
    except OzonAPIError as e:
        raise HTTPException(400, e.message)
    return {"ok": True, "processed": count}


# ------------------------------------------------------------------
# Уведомления
# ------------------------------------------------------------------

@router.post("/notifications/read")
async def api_mark_all_read(user: User = Depends(require_user), db: AsyncSession = Depends(get_db)):
    from sqlalchemy import update as sa_update
    await db.execute(
        sa_update(Notification).where(Notification.user_id == user.id).values(is_read=True)
    )
    await db.commit()
    return {"ok": True}


# ------------------------------------------------------------------
# Товары: экономика (себестоимость, цена, остатки, выкуп)
# ------------------------------------------------------------------

@router.post("/products/info")
async def api_save_product_info(user: User = Depends(require_user), db: AsyncSession = Depends(get_db),
                                request: Request = None):
    """Сохраняет экономику товара по SKU. body: {sku, price, cost_price, ...}"""
    body = await request.json()
    sku = str(body.get("sku", "")).strip()
    if not sku:
        raise HTTPException(400, "SKU обязателен")

    result = await db.execute(
        select(ProductInfo).where(ProductInfo.user_id == user.id, ProductInfo.sku == sku)
    )
    info = result.scalar_one_or_none()
    if info is None:
        info = ProductInfo(user_id=user.id, sku=sku)
        db.add(info)

    for field in ("price", "cost_price", "leftovers", "commission_pct", "logistics_cost",
                  "acquiring_pct", "buyout_pct", "promotion_discount_pct", "monthly_orders",
                  "monthly_revenue", "min_margin_pct"):
        if field in body:
            try:
                setattr(info, field, float(body[field]))
            except (TypeError, ValueError):
                pass
    if "fulfillment_type" in body and body["fulfillment_type"] in ("FBO", "FBS"):
        info.fulfillment_type = body["fulfillment_type"]
    if "in_promotion" in body:
        info.in_promotion = bool(body["in_promotion"])
    if "name" in body:
        info.name = str(body["name"])[:255]

    from datetime import datetime
    info.updated_at = datetime.utcnow()
    await db.commit()
    return {"ok": True}


@router.get("/products/info")
async def api_get_product_info(sku: str, user: User = Depends(require_user), db: AsyncSession = Depends(get_db)):
    """Возвращает экономику товара по SKU (для модального окна)."""
    result = await db.execute(
        select(ProductInfo).where(ProductInfo.user_id == user.id, ProductInfo.sku == sku)
    )
    info = result.scalar_one_or_none()
    if info is None:
        return {"econ": {}}
    from app.services.economics import calculate, from_info
    econ = calculate(from_info(info, sku=sku))
    return {
        "econ": {
            "price": econ.price,
            "cost_price": econ.cost_price,
            "commission_pct": econ.commission_pct,
            "logistics_cost": econ.logistics_cost,
            "acquiring_pct": econ.acquiring_pct,
            "buyout_pct": econ.buyout_pct,
            "in_promotion": econ.in_promotion,
            "promotion_discount_pct": econ.promotion_discount_pct,
            "monthly_orders": econ.monthly_orders,
            "monthly_revenue": econ.monthly_revenue,
            "min_margin_pct": econ.min_margin_pct,
        }
    }


@router.post("/products/info-defaults")
async def api_apply_product_defaults(user: User = Depends(require_user), db: AsyncSession = Depends(get_db),
                                     request: Request = None):
    """Применяет значения по умолчанию ко всем товарам пользователя.

    Обновляет только те поля, у которых значение ещё не задано (0/None).
    body: {commission_pct, logistics_cost, acquiring_pct, buyout_pct, min_margin_pct}
    """
    body = await request.json()
    from sqlalchemy import select as sa_select
    result = await db.execute(sa_select(ProductInfo).where(ProductInfo.user_id == user.id))
    infos = list(result.scalars().all())
    changed = 0
    for info in infos:
        modified = False
        if not info.commission_pct and body.get("commission_pct"):
            info.commission_pct = float(body["commission_pct"]); modified = True
        if not info.logistics_cost and body.get("logistics_cost"):
            info.logistics_cost = float(body["logistics_cost"]); modified = True
        if not info.acquiring_pct and body.get("acquiring_pct"):
            info.acquiring_pct = float(body["acquiring_pct"]); modified = True
        if info.buyout_pct == 100 and body.get("buyout_pct"):
            info.buyout_pct = float(body["buyout_pct"]); modified = True
        if not info.min_margin_pct and body.get("min_margin_pct"):
            info.min_margin_pct = float(body["min_margin_pct"]); modified = True
        if modified:
            changed += 1
    await db.commit()
    return {"ok": True, "updated": changed}


# ------------------------------------------------------------------
# Бидер: правила управления ставками
# ------------------------------------------------------------------

@router.post("/bidder/rules")
async def api_create_bidder_rule(user: User = Depends(require_user), db: AsyncSession = Depends(get_db),
                                 request: Request = None):
    """Создаёт правило бидера. body: {name, strategy, sku, campaign_id, params, is_active}"""
    body = await request.json()
    name = str(body.get("name", "")).strip()
    strategy = str(body.get("strategy", ""))
    if not name or strategy not in ("target_drr", "maintain_position", "ai_test"):
        raise HTTPException(400, "Некорректное правило: нужны name и strategy")

    rule = BidderRule(
        user_id=user.id,
        name=name,
        strategy=strategy,
        sku=str(body.get("sku", "") or ""),
        campaign_id=body.get("campaign_id"),
        params=body.get("params") or {},
        is_active=bool(body.get("is_active", True)),
    )
    db.add(rule)
    await db.commit()
    await log_action(db, action="bidder_rule_create", user_id=user.id,
                     entity_type="rule", entity_name=rule.name,
                     details={"strategy": strategy})
    return {"ok": True, "id": rule.id}


@router.put("/bidder/rules/{rule_id}/toggle")
async def api_toggle_bidder_rule(rule_id: int, user: User = Depends(require_user),
                                 db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(BidderRule).where(BidderRule.id == rule_id, BidderRule.user_id == user.id)
    )
    rule = result.scalar_one_or_none()
    if not rule:
        raise HTTPException(404, "Правило не найдено")
    rule.is_active = not rule.is_active
    await db.commit()
    return {"ok": True, "is_active": rule.is_active}


@router.delete("/bidder/rules/{rule_id}")
async def api_delete_bidder_rule(rule_id: int, user: User = Depends(require_user),
                                 db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(BidderRule).where(BidderRule.id == rule_id, BidderRule.user_id == user.id)
    )
    rule = result.scalar_one_or_none()
    if not rule:
        raise HTTPException(404, "Правило не найдено")
    await db.delete(rule)
    await db.commit()
    return {"ok": True, "deleted": True}


# ------------------------------------------------------------------
# Seller API: синхронизация экономики товаров из ЛК
# ------------------------------------------------------------------

@router.post("/seller/sync")
async def api_sync_seller(user: User = Depends(require_user), db: AsyncSession = Depends(get_db)):
    """Подтягивает остатки, цены, комиссии, акции из ЛК Ozon (Seller API)."""
    from app.services.seller_sync import get_active_seller_client, sync_seller_data
    client = await get_active_seller_client(db, user.id)
    if client is None:
        raise HTTPException(400, "Ключи Seller API не подключены. Добавьте их в «Настройках».")
    try:
        result = await sync_seller_data(db, user.id, client)
    except Exception as e:
        import logging, traceback
        logging.getLogger("seller_sync").error("Ошибка синхронизации: %s\n%s", e, traceback.format_exc())
        from app.services.seller_client import SellerAPIError
        if isinstance(e, SellerAPIError):
            raise HTTPException(400, e.message)
        raise HTTPException(500, f"Ошибка синхронизации: {e}")
    await log_action(db, action="seller_sync", user_id=user.id,
                     entity_type="product", entity_name="все товары",
                     details=result)
    return {"ok": True, **result}


# ------------------------------------------------------------------
# Экспорт CSV
# ------------------------------------------------------------------

@router.get("/stats/export")
async def export_stats_csv(
    request: Request,
    campaign_pk: str = "",
    days: int = 30,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    """Экспорт статистики в CSV (загружается как файл)."""
    from datetime import date, timedelta
    from app.services.campaigns import get_campaigns
    from app.services.statistics import get_stats_for_period
    from app.services.csv_export import csv_response

    campaign_pk_int = int(campaign_pk) if campaign_pk else None
    today = date.today()
    date_from = today - timedelta(days=days)

    stats = await get_stats_for_period(db, user.id, campaign_pk_int, date_from, today)
    campaigns = await get_campaigns(db, user.id)
    camp_map = {c.id: c.title for c in campaigns}

    return csv_response(
        [
            {
                "Дата": s.stat_date,
                "Кампания": camp_map.get(s.campaign_id, str(s.campaign_id)),
                "Показы": s.impressions,
                "Клики": s.clicks,
                "CTR": s.ctr,
                "Заказы": s.orders,
                "Выручка": s.revenue,
                "Расход": s.spend,
                "CPA": s.cpa,
                "ROMI": s.romi,
            }
            for s in stats
        ],
        fieldnames=["Дата", "Кампания", "Показы", "Клики", "CTR", "Заказы",
                     "Выручка", "Расход", "CPA", "ROMI"],
        filename=f"stats_{date_from.isoformat()}_{today.isoformat()}.csv",
    )


@router.get("/products/export")
async def export_products_csv(
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    """Экспорт экономики всех товаров в CSV."""
    from app.models import ProductInfo
    from app.services.csv_export import csv_response

    result = await db.execute(
        select(ProductInfo)
        .where(ProductInfo.user_id == user.id)
        .order_by(ProductInfo.name)
    )
    products = list(result.scalars().all())

    return csv_response(
        [
            {
                "SKU": p.sku,
                "Название": p.name,
                "Цена продажи": p.price,
                "Себестоимость": p.cost_price,
                "Остатки": p.leftovers,
                "Тип поставки": p.fulfillment_type,
                "Комиссия %": p.commission_pct,
                "Логистика": p.logistics_cost,
                "Эквайринг %": p.acquiring_pct,
                "% выкупа": p.buyout_pct,
                "В акции": "Да" if p.in_promotion else "Нет",
                "Скидка акции %": p.promotion_discount_pct,
                "Заказов за месяц": p.monthly_orders,
                "Маржа шт": (p.price - p.cost_price) if p.price and p.cost_price else 0,
            }
            for p in products
        ],
        fieldnames=["SKU", "Название", "Цена продажи", "Себестоимость", "Остатки",
                     "Тип поставки", "Комиссия %", "Логистика", "Эквайринг %",
                     "% выкупа", "В акции", "Скидка акции %", "Заказов за месяц", "Маржа шт"],
        filename="products_economics.csv",
    )