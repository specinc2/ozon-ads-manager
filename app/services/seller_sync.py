"""Синхронизация экономики товаров из Seller API (остатки, цены, комиссии, акции).

Привязка к товарам рекламных кампаний идёт по offer_id (артикул продавца),
который Ozon возвращает в v4/product/info/stocks вместе с Ozon SKU.
"""
import logging
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ApiKey, Product, ProductInfo, Campaign
from app.security import decrypt_value
from app.services.seller_client import SellerClient, SellerAPIError

logger = logging.getLogger("seller_sync")


async def get_active_seller_client(db: AsyncSession, user_id: int) -> SellerClient | None:
    """Возвращает Seller-клиент, если у пользователя есть ключи Seller API."""
    result = await db.execute(
        select(ApiKey).where(ApiKey.user_id == user_id, ApiKey.is_active.is_(True)).limit(1)
    )
    key = result.scalar_one_or_none()
    if key is None:
        return None
    seller_client_id = decrypt_value(key.seller_client_id_enc)
    seller_api_key = decrypt_value(key.seller_api_key_enc)
    if not seller_client_id or not seller_api_key:
        return None
    return SellerClient(seller_client_id, seller_api_key)


async def sync_seller_data(db: AsyncSession, user_id: int, client: SellerClient) -> dict:
    """Подтягивает экономику ВСЕХ товаров магазина из Seller API.

    Создаёт ProductInfo для каждого товара каталога (не только рекламных),
    заполняет остатки, цены, комиссии, акции, аналитику.
    Себестоимость Ozon не отдаёт — остаётся ручной ввод.

    Возвращает статистику: {updated, with_stock, with_commission, products}.
    """
    # Полный каталог товаров продавца
    try:
        all_products = await client.list_products()
    except SellerAPIError as e:
        logger.warning("Не удалось получить список товаров: %s", e)
        all_products = []

    # sku (Ozon) -> offer_id; также собираем имена из рекламных кампаний
    sku_to_offer: dict[str, str] = {}
    for item in all_products:
        sku = item.get("sku")
        offer_id = item.get("offer_id")
        if sku is not None and offer_id:
            sku_to_offer[str(sku)] = str(offer_id)

    # Все SKU, которые есть в каталоге (77+) или в рекламных кампаниях
    all_skus = set(sku_to_offer.keys())
    result = await db.execute(
        select(Product).join(Campaign, Campaign.id == Product.campaign_id)
        .where(Campaign.user_id == user_id)
    )
    ad_products = list(result.scalars().all())
    ad_names: dict[str, str] = {}
    for p in ad_products:
        all_skus.add(p.sku)
        ad_names.setdefault(p.sku, p.name or "")

    offer_ids: list[str] = []
    for sku in all_skus:
        offer = sku_to_offer.get(str(sku)) or str(sku)
        if offer not in offer_ids:
            offer_ids.append(offer)

    if not offer_ids:
        return {"updated": 0, "with_stock": 0, "with_commission": 0, "products": 0}

    stocks = await client.get_stocks(offer_ids)
    prices = await client.get_prices(offer_ids)

    # Названия товаров (v3/product/list их не отдаёт — берём из description)
    try:
        names = await client.get_product_names(offer_ids)
    except SellerAPIError as e:
        logger.warning("Названия товаров не получены: %s", e)
        names = {}

    # Аналитика за месяц (заказы, выручка) — метрика delivered_units устарела,
    # % выкупа пользователь может указать вручную в карточке товара
    today = date.today()
    month_ago = today - timedelta(days=30)
    try:
        analytics = await client.get_analytics(
            month_ago.isoformat(), today.isoformat(),
            ["ordered_units", "revenue"],
        )
    except SellerAPIError as e:
        logger.warning("Аналитика недоступна: %s", e)
        analytics = {}

    now = datetime.utcnow()
    updated = 0
    with_stock = 0
    with_commission = 0

    info_map: dict[str, ProductInfo] = {}
    existing = await db.execute(
        select(ProductInfo).where(ProductInfo.user_id == user_id)
    )
    for info in existing.scalars().all():
        info_map[info.sku] = info

    for sku in sorted(all_skus):
        offer = sku_to_offer.get(str(sku)) or str(sku)
        stock = stocks.get(offer)
        price = prices.get(offer)

        info = info_map.get(sku)
        if info is None:
            info = ProductInfo(user_id=user_id, sku=sku, name="")
            db.add(info)
            info_map[sku] = info
        elif not info.name and ad_names.get(sku):
            info.name = ad_names[sku]

        # Если нет названия — берём из Seller API
        if not info.name:
            offer = sku_to_offer.get(str(sku)) or str(sku)
            info.name = names.get(offer) or info.name or ""

        # Остатки
        if stock:
            info.leftovers = (stock.get("fbo_present") or 0) + (stock.get("fbs_present") or 0)
            if info.leftovers:
                with_stock += 1

        # Цены и комиссии (себестоимость не трогаем — только ручной ввод)
        if price:
            if price.get("price"):
                info.price = price["price"]
            info.commission_pct = price.get("commission_pct") or info.commission_pct
            info.logistics_cost = price.get("logistics_cost") or info.logistics_cost
            acquiring_rub = price.get("acquiring_rub") or 0
            if acquiring_rub and info.price:
                info.acquiring_pct = round(acquiring_rub / info.price * 100, 2)
            info.in_promotion = price.get("in_promotion", info.in_promotion)
            if price.get("promotion_discount_pct"):
                info.promotion_discount_pct = price["promotion_discount_pct"]
            if info.commission_pct:
                with_commission += 1

        # Аналитика: заказы и выручка за месяц
        metrics = analytics.get(str(sku))
        if metrics:
            ordered = float(metrics.get("ordered_units") or 0)
            revenue = float(metrics.get("revenue") or 0)
            if ordered:
                info.monthly_orders = int(ordered)
            if revenue:
                info.monthly_revenue = revenue

        info.updated_at = now
        updated += 1

    await db.commit()
    return {
        "updated": updated,
        "with_stock": with_stock,
        "with_commission": with_commission,
        "products": len(all_skus),
    }


async def save_seller_keys(
    db: AsyncSession, user_id: int, client_id: str, api_key: str,
) -> bool:
    """Сохраняет ключи Seller API (зашифрованными) в первую активную запись ApiKey."""
    result = await db.execute(
        select(ApiKey).where(ApiKey.user_id == user_id, ApiKey.is_active.is_(True)).limit(1)
    )
    key = result.scalar_one_or_none()
    if key is None:
        return False
    from app.security import encrypt_value
    key.seller_client_id_enc = encrypt_value(client_id)
    key.seller_api_key_enc = encrypt_value(api_key)
    await db.commit()
    return True
