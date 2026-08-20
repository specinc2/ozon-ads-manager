"""HTML-страницы приложения: дашборд, кампании, статистика, правила, настройки."""
from datetime import date, timedelta, timezone

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import BASE_DIR
from app.database import get_db
from app.deps import SESSION_COOKIE, get_current_user
from app.models import (
    ActionLog,
    ApiKey,
    ApiLog,
    AutomationRule,
    BidderRule,
    Campaign,
    CampaignSchedule,
    Notification,
    Product,
    ProductInfo,
    User,
)
from app.security import (
    create_session_token,
    encrypt_value,
    hash_password,
    verify_password,
)
from app.services.campaigns import (
    get_active_ozon_client,
    get_campaign_or_none,
    get_campaigns,
    sync_campaigns,
)
from app.services.ozon_client import OzonAPIError, OzonAuthError, OzonClient
from app.services.products import get_products
from app.services.statistics import get_aggregated_stats, get_stats_for_period

router = APIRouter()
templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))

MSK_OFFSET = timedelta(hours=3)


def msk_time(dt) -> str:
    """Переводит UTC в МСК (UTC+3) и форматирует для отображения."""
    if dt is None:
        return ""
    return (dt + MSK_OFFSET).strftime("%d.%m.%Y %H:%M:%S")


templates.env.filters["msk"] = msk_time


def flash(request: Request, message: str, category: str = "info") -> None:
    """Сохраняет flash-сообщение в session (подписанная cookie Starlette)."""
    session = request.session
    flashes = session.setdefault("_flashes", [])
    flashes.append({"category": category, "message": message})
    session["_flashes"] = flashes


async def _common_context(request: Request, db: AsyncSession) -> dict:
    """Базовый контекст для всех шаблонов: пользователь и flash-сообщения."""
    user = await get_current_user(request, db)
    flashes = request.session.pop("_flashes", []) if request.session else []
    return {
        "request": request,
        "user": user,
        "flashes": flashes,
    }


# ------------------------------------------------------------------
# Аутентификация
# ------------------------------------------------------------------

@router.get("/login")
async def login_page(request: Request, db: AsyncSession = Depends(get_db)):
    return templates.TemplateResponse("login.html", await _common_context(request, db))


@router.post("/login")
async def login_action(request: Request, db: AsyncSession = Depends(get_db)):
    form = await request.form()
    login = form.get("username", "").strip()
    password = form.get("password", "")

    # Вход по имени пользователя ИЛИ по email
    result = await db.execute(
        select(User).where((User.username == login) | (User.email == login))
    )
    user = result.scalar_one_or_none()
    if not user or not verify_password(password, user.password_hash):
        flash(request, "Неверное имя пользователя, email или пароль", "danger")
        return templates.TemplateResponse("login.html", await _common_context(request, db))

    token = create_session_token(user.id)
    resp = RedirectResponse("/", status_code=302)
    resp.set_cookie(key=SESSION_COOKIE, value=token, httponly=True, max_age=7 * 86400, samesite="lax")
    return resp


@router.get("/register")
async def register_page(request: Request, db: AsyncSession = Depends(get_db)):
    return templates.TemplateResponse("register.html", await _common_context(request, db))


@router.post("/register")
async def register_action(request: Request, db: AsyncSession = Depends(get_db)):
    form = await request.form()
    username = form.get("username", "").strip()
    email = form.get("email", "").strip()
    password = form.get("password", "")

    if not username or not email or not password:
        flash(request, "Все поля обязательны", "danger")
        return templates.TemplateResponse("register.html", await _common_context(request, db))

    existing = (await db.execute(
        select(User).where((User.username == username) | (User.email == email))
    )).scalar_one_or_none()
    if existing:
        flash(request, "Пользователь с таким именем или email уже существует", "danger")
        return templates.TemplateResponse("register.html", await _common_context(request, db))

    user = User(username=username, email=email, password_hash=hash_password(password))
    db.add(user)
    await db.commit()

    token = create_session_token(user.id)
    resp = RedirectResponse("/", status_code=302)
    resp.set_cookie(key=SESSION_COOKIE, value=token, httponly=True, max_age=7 * 86400, samesite="lax")
    return resp


@router.get("/logout")
async def logout():
    resp = RedirectResponse("/login", status_code=302)
    resp.delete_cookie(SESSION_COOKIE)
    return resp


# ------------------------------------------------------------------
# FAQ (доступен всем, в т.ч. до регистрации)
# ------------------------------------------------------------------

@router.get("/faq")
async def faq_page(request: Request, db: AsyncSession = Depends(get_db)):
    return templates.TemplateResponse("faq.html", await _common_context(request, db))


# ------------------------------------------------------------------
# Дашборд
# ------------------------------------------------------------------

@router.get("/")
async def dashboard(request: Request, db: AsyncSession = Depends(get_db)):
    ctx = await _common_context(request, db)
    user = ctx["user"]
    if not user:
        return RedirectResponse("/login", status_code=302)

    today = date.today()
    week_ago = today - timedelta(days=7)
    month_ago = today - timedelta(days=30)

    ctx["week_stats"] = await get_aggregated_stats(db, user.id, None, week_ago, today)
    ctx["month_stats"] = await get_aggregated_stats(db, user.id, None, month_ago, today)

    result = await db.execute(
        select(Campaign.status, func.count(Campaign.id)).where(
            Campaign.user_id == user.id
        ).group_by(Campaign.status)
    )
    ctx["status_counts"] = dict(result.all())

    result = await db.execute(
        select(Notification).where(
            Notification.user_id == user.id, Notification.is_read.is_(False)
        ).order_by(Notification.ts.desc()).limit(10)
    )
    ctx["unread_notifications"] = list(result.scalars().all())

    return templates.TemplateResponse("dashboard.html", ctx)


# ------------------------------------------------------------------
# Кампании
# ------------------------------------------------------------------

@router.get("/campaigns")
async def campaign_list(
    request: Request,
    status: str | None = None,
    campaign_type: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    ctx = await _common_context(request, db)
    user = ctx["user"]
    if not user:
        return RedirectResponse("/login", status_code=302)

    if request.query_params.get("sync"):
        try:
            client = await get_active_ozon_client(db, user.id)
            count = await sync_campaigns(db, user.id, client)
            flash(request, f"Синхронизировано кампаний: {count}", "success")
        except OzonAuthError as e:
            flash(request, e.message, "danger")
        except OzonAPIError as e:
            flash(request, f"Ошибка Ozon: {e.message}", "danger")

    ctx["campaigns"] = await get_campaigns(db, user.id, status=status, campaign_type=campaign_type)
    ctx["filter_status"] = status
    ctx["filter_type"] = campaign_type

    # Число загруженных товаров по каждой кампании (для автозагрузки)
    if ctx["campaigns"]:
        result = await db.execute(
            select(Campaign.id, func.count(Product.id)).select_from(Campaign)
            .outerjoin(Product, Product.campaign_id == Campaign.id)
            .where(Campaign.user_id == user.id)
            .group_by(Campaign.id)
        )
        ctx["product_counts"] = dict(result.all())

        # Суммарные заказы по каждой кампании (за 30 дней)
        from app.models import CampaignStat
        result = await db.execute(
            select(CampaignStat.campaign_id, func.coalesce(func.sum(CampaignStat.orders), 0))
            .join(Campaign, Campaign.id == CampaignStat.campaign_id)
            .where(Campaign.user_id == user.id)
            .group_by(CampaignStat.campaign_id)
        )
        ctx["orders_counts"] = dict(result.all())
    else:
        ctx["product_counts"] = {}
        ctx["orders_counts"] = {}

    result = await db.execute(
        select(Campaign.campaign_type).where(Campaign.user_id == user.id).distinct()
    )
    ctx["types"] = [r[0] for r in result.all() if r[0]]

    return templates.TemplateResponse("campaigns.html", ctx)


@router.get("/campaigns/{campaign_pk}")
async def campaign_detail(request: Request, campaign_pk: int, db: AsyncSession = Depends(get_db)):
    ctx = await _common_context(request, db)
    user = ctx["user"]
    if not user:
        return RedirectResponse("/login", status_code=302)

    campaign = await get_campaign_or_none(db, user.id, campaign_pk)
    if not campaign:
        flash(request, "Кампания не найдена", "danger")
        return RedirectResponse("/campaigns", status_code=302)

    ctx["campaign"] = campaign
    ctx["products"] = await get_products(db, campaign_pk)
    # Для JS (bulk-изменение ставок) — сериализуемые словари
    ctx["products_json"] = [
        {"sku": p.sku, "bid": p.bid, "name": p.name}
        for p in ctx["products"]
    ]

    today = date.today()
    month_ago = today - timedelta(days=30)
    ctx["stats"] = await get_stats_for_period(db, user.id, campaign_pk, month_ago, today)
    ctx["stats_json"] = [
        {
            "stat_date": str(s.stat_date),
            "impressions": s.impressions,
            "clicks": s.clicks,
            "ctr": s.ctr,
            "orders": s.orders,
            "revenue": s.revenue,
            "spend": s.spend,
            "cpa": s.cpa,
            "romi": s.romi,
        }
        for s in ctx["stats"]
    ]
    ctx["agg"] = await get_aggregated_stats(db, user.id, campaign_pk, month_ago, today)

    return templates.TemplateResponse("campaign_detail.html", ctx)


# ------------------------------------------------------------------
# Статистика
# ------------------------------------------------------------------

@router.get("/stats")
async def stats_page(
    request: Request,
    campaign_pk: str = "",
    days: int = 30,
    db: AsyncSession = Depends(get_db),
):
    ctx = await _common_context(request, db)
    user = ctx["user"]
    if not user:
        return RedirectResponse("/login", status_code=302)

    # Пустая строка = «Все кампании» (иначе FastAPI упадёт на int_parsing)
    campaign_pk_int = int(campaign_pk) if campaign_pk else None

    today = date.today()
    date_from = today - timedelta(days=days)
    ctx["date_from"] = date_from.isoformat()
    ctx["date_to"] = today.isoformat()
    ctx["stats"] = await get_stats_for_period(db, user.id, campaign_pk_int, date_from, today)
    # Преобразуем ORM-объекты в словари для JSON-сериализации в шаблоне (Chart.js)
    ctx["stats_json"] = [
        {
            "stat_date": str(s.stat_date),
            "impressions": s.impressions,
            "clicks": s.clicks,
            "ctr": s.ctr,
            "orders": s.orders,
            "revenue": s.revenue,
            "spend": s.spend,
            "cpa": s.cpa,
            "romi": s.romi,
        }
        for s in ctx["stats"]
    ]
    ctx["campaigns"] = await get_campaigns(db, user.id)
    ctx["selected_campaign"] = campaign_pk_int
    ctx["days"] = days
    # Название выбранной кампании для подписи
    if campaign_pk_int:
        camp = next((c for c in ctx["campaigns"] if c.id == campaign_pk_int), None)
        ctx["selected_campaign_title"] = camp.title if camp else str(campaign_pk_int)
    return templates.TemplateResponse("stats.html", ctx)


# ------------------------------------------------------------------
# Авто-правила
# ------------------------------------------------------------------

@router.get("/rules")
async def rules_page(request: Request, db: AsyncSession = Depends(get_db)):
    ctx = await _common_context(request, db)
    user = ctx["user"]
    if not user:
        return RedirectResponse("/login", status_code=302)

    result = await db.execute(
        select(AutomationRule).where(AutomationRule.user_id == user.id).order_by(AutomationRule.created_at.desc())
    )
    ctx["rules"] = list(result.scalars().all())
    ctx["campaigns"] = await get_campaigns(db, user.id)
    return templates.TemplateResponse("rules.html", ctx)


# ------------------------------------------------------------------
# Расписания
# ------------------------------------------------------------------

@router.get("/schedules")
async def schedules_page(request: Request, db: AsyncSession = Depends(get_db)):
    ctx = await _common_context(request, db)
    user = ctx["user"]
    if not user:
        return RedirectResponse("/login", status_code=302)

    result = await db.execute(
        select(CampaignSchedule).where(CampaignSchedule.user_id == user.id).order_by(CampaignSchedule.created_at.desc())
    )
    ctx["schedules"] = list(result.scalars().all())
    ctx["campaigns"] = await get_campaigns(db, user.id)
    return templates.TemplateResponse("schedules.html", ctx)


# ------------------------------------------------------------------
# Настройки (API-ключи)
# ------------------------------------------------------------------

@router.get("/settings")
async def settings_page(request: Request, db: AsyncSession = Depends(get_db)):
    ctx = await _common_context(request, db)
    user = ctx["user"]
    if not user:
        return RedirectResponse("/login", status_code=302)

    result = await db.execute(
        select(ApiKey).where(ApiKey.user_id == user.id).order_by(ApiKey.created_at.desc())
    )
    api_keys = list(result.scalars().all())
    # Подготавливаем маскированные данные для шаблона (без расшифрованных секретов)
    from app.security import decrypt_value
    key_infos = []
    for k in api_keys:
        client_id = decrypt_value(k.client_id_enc)
        masked = (client_id[:6] + "..." + client_id[-4:]) if len(client_id) > 12 else "***"
        seller_enabled = bool(k.seller_client_id_enc and k.seller_api_key_enc)
        key_infos.append({
            "id": k.id,
            "name": k.name,
            "is_active": k.is_active,
            "client_id_masked": masked,
            "last_verified_at": k.last_verified_at,
            "api_key_expires_at": k.api_key_expires_at,
            "seller_enabled": seller_enabled,
            "created_at": k.created_at,
        })
    ctx["api_keys"] = api_keys
    ctx["key_infos"] = key_infos
    ctx["has_seller_keys"] = any(k["seller_enabled"] and k["is_active"] for k in key_infos)
    return templates.TemplateResponse("settings.html", ctx)


@router.post("/settings/seller-keys")
async def save_seller_keys(request: Request, db: AsyncSession = Depends(get_db)):
    ctx = await _common_context(request, db)
    user = ctx["user"]
    if not user:
        return RedirectResponse("/login", status_code=302)

    form = await request.form()
    seller_client_id = form.get("seller_client_id", "").strip()
    seller_api_key = form.get("seller_api_key", "").strip()

    if not seller_client_id or not seller_api_key:
        flash(request, "Client-Id и Api-Key обязательны", "danger")
        return RedirectResponse("/settings", status_code=302)

    from app.services.seller_sync import save_seller_keys
    ok = await save_seller_keys(db, user.id, seller_client_id, seller_api_key)
    if ok:
        flash(request, "Ключи Seller API сохранены", "success")
    else:
        flash(request, "Сначала добавьте ключи Performance API", "danger")
    return RedirectResponse("/settings", status_code=302)


@router.post("/settings/keys")
async def add_api_key(request: Request, db: AsyncSession = Depends(get_db)):
    ctx = await _common_context(request, db)
    user = ctx["user"]
    if not user:
        return RedirectResponse("/login", status_code=302)

    form = await request.form()
    client_id = form.get("client_id", "").strip()
    client_secret = form.get("client_secret", "").strip()
    name = form.get("name", "Основные ключи").strip()

    if not client_id or not client_secret:
        flash(request, "Client-Id и Client-Secret обязательны", "danger")
        return RedirectResponse("/settings", status_code=302)

    # Проверяем ключи тестовым запросом (GET /api/client/campaign)
    test_client = OzonClient(client_id, client_secret)
    try:
        await test_client.list_campaigns()
    except OzonAuthError as e:
        flash(request, f"Ключи не прошли проверку: {e.message}", "danger")
        return RedirectResponse("/settings", status_code=302)
    except OzonAPIError:
        pass  # другие ошибки игнорируем — ключи могут быть верны, кампаний может не быть

    api_key = ApiKey(
        user_id=user.id,
        name=name,
        client_id_enc=encrypt_value(client_id),
        client_secret_enc=encrypt_value(client_secret),
        api_key_expires_at=test_client.api_key_expires,
        is_active=True,
    )
    db.add(api_key)
    await db.commit()
    flash(request, "API-ключи добавлены и проверены", "success")
    return RedirectResponse("/settings", status_code=302)


@router.post("/settings/keys/{key_id}/toggle")
async def toggle_api_key(request: Request, key_id: int, db: AsyncSession = Depends(get_db)):
    ctx = await _common_context(request, db)
    user = ctx["user"]
    if not user:
        return RedirectResponse("/login", status_code=302)
    result = await db.execute(
        select(ApiKey).where(ApiKey.id == key_id, ApiKey.user_id == user.id)
    )
    key = result.scalar_one_or_none()
    if key:
        key.is_active = not key.is_active
        await db.commit()
    return RedirectResponse("/settings", status_code=302)


@router.post("/settings/keys/{key_id}/delete")
async def delete_api_key(request: Request, key_id: int, db: AsyncSession = Depends(get_db)):
    ctx = await _common_context(request, db)
    user = ctx["user"]
    if not user:
        return RedirectResponse("/login", status_code=302)
    result = await db.execute(
        select(ApiKey).where(ApiKey.id == key_id, ApiKey.user_id == user.id)
    )
    key = result.scalar_one_or_none()
    if key:
        await db.delete(key)
        await db.commit()
    return RedirectResponse("/settings", status_code=302)


# ------------------------------------------------------------------
# Журнал
# ------------------------------------------------------------------

@router.get("/logs")
async def logs_page(request: Request, db: AsyncSession = Depends(get_db)):
    ctx = await _common_context(request, db)
    user = ctx["user"]
    if not user:
        return RedirectResponse("/login", status_code=302)

    result = await db.execute(
        select(ActionLog).where(ActionLog.user_id == user.id).order_by(ActionLog.ts.desc()).limit(200)
    )
    ctx["action_logs"] = list(result.scalars().all())

    result = await db.execute(
        select(ApiLog).where(ApiLog.user_id == user.id).order_by(ApiLog.ts.desc()).limit(100)
    )
    ctx["api_logs"] = list(result.scalars().all())
    return templates.TemplateResponse("logs.html", ctx)


# ------------------------------------------------------------------
# Товары
# ------------------------------------------------------------------

@router.get("/products")
async def products_page(request: Request, db: AsyncSession = Depends(get_db)):
    ctx = await _common_context(request, db)
    user = ctx["user"]
    if not user:
        return RedirectResponse("/login", status_code=302)

    # Все ProductInfo (экономика) — все товары из ЛК, а не только рекламные
    from datetime import date, timedelta
    cutoff = date.today() - timedelta(days=30)

    from sqlalchemy import func
    from app.models import CampaignStat, Product, Campaign

    # Все товары из каталога (ProductInfo)
    result = await db.execute(
        select(ProductInfo).where(ProductInfo.user_id == user.id).order_by(ProductInfo.name)
    )
    all_infos = list(result.scalars().all())

    # Рекламная статистика по SKU (агрегированная за 30 дней по всем кампаниям)
    ad_stats_rows = await db.execute(
        select(
            Product.sku,
            func.coalesce(func.sum(CampaignStat.spend), 0),
            func.coalesce(func.sum(CampaignStat.revenue), 0),
            func.coalesce(func.sum(CampaignStat.orders), 0),
            func.coalesce(func.sum(CampaignStat.impressions), 0),
            func.coalesce(func.sum(CampaignStat.clicks), 0),
        ).select_from(Product)
        .join(CampaignStat, CampaignStat.campaign_id == Product.campaign_id)
        .join(Campaign, Campaign.id == Product.campaign_id)
        .where(Campaign.user_id == user.id, CampaignStat.stat_date >= cutoff)
        .group_by(Product.sku)
    )
    ad_stats: dict[str, tuple] = {}
    for row in ad_stats_rows:
        sku = str(row[0])
        ad_stats[sku] = (float(row[1]), float(row[2]), int(row[3]), int(row[4]), int(row[5]))

    # Собираем карточки с экономикой
    from app.services.economics import calculate, from_info
    card_list = []
    for info in all_infos:
        econ = from_info(info, sku=info.sku, name=info.name or "")
        # Рекламная статистика
        stats = ad_stats.get(info.sku, (0, 0, 0, 0, 0))
        econ.ad_spend = stats[0]
        econ.ad_revenue = stats[1]
        econ.ad_orders = stats[2]
        impressions = int(stats[3])
        clicks = int(stats[4])
        if econ.monthly_revenue <= 0:
            econ.total_revenue = econ.ad_revenue
        calculate(econ)

        # Низкая посещаемость (по рекламным показам)
        econ.low_traffic = bool(impressions < 100 and stats[2] < 5)

        # Кампании, в которых участвует товар (для информации)
        camp_result = await db.execute(
            select(Campaign.title).select_from(Product)
            .join(Campaign, Campaign.id == Product.campaign_id)
            .where(Campaign.user_id == user.id, Product.sku == info.sku)
            .distinct()
        )
        campaign_titles = [r[0] for r in camp_result.all()]

        card_list.append({
            "info": info,
            "econ": econ,
            "impressions": impressions,
            "clicks": clicks,
            "campaign_titles": ", ".join(campaign_titles[:3]) + ("…" if len(campaign_titles) > 3 else ""),
            "in_ad": bool(campaign_titles),
        })

    ctx["cards"] = card_list
    ctx["campaigns"] = await get_campaigns(db, user.id)

    # Подключены ли ключи Seller API (для кнопки синхронизации)
    seller_keys = (await db.execute(
        select(ApiKey).where(ApiKey.user_id == user.id, ApiKey.is_active.is_(True))
    )).scalars().all()
    ctx["has_seller_keys"] = any(k.seller_client_id_enc and k.seller_api_key_enc for k in seller_keys)
    return templates.TemplateResponse("products.html", ctx)


# ------------------------------------------------------------------
# Бидер (правила управления ставками)
# ------------------------------------------------------------------

@router.get("/bidder")
async def bidder_page(request: Request, db: AsyncSession = Depends(get_db)):
    ctx = await _common_context(request, db)
    user = ctx["user"]
    if not user:
        return RedirectResponse("/login", status_code=302)

    result = await db.execute(
        select(BidderRule).where(BidderRule.user_id == user.id).order_by(BidderRule.created_at.desc())
    )
    ctx["rules"] = list(result.scalars().all())
    ctx["campaigns"] = await get_campaigns(db, user.id)
    return templates.TemplateResponse("bidder.html", ctx)


# ------------------------------------------------------------------
# Уведомления
# ------------------------------------------------------------------

@router.post("/notifications/read")
async def mark_notifications_read(request: Request, db: AsyncSession = Depends(get_db)):
    ctx = await _common_context(request, db)
    user = ctx["user"]
    if not user:
        return RedirectResponse("/login", status_code=302)
    await db.execute(
        select(Notification).where(Notification.user_id == user.id, Notification.is_read.is_(False)).with_for_update()
    )
    from sqlalchemy import update as sa_update
    await db.execute(
        sa_update(Notification).where(Notification.user_id == user.id).values(is_read=True)
    )
    await db.commit()
    return RedirectResponse("/", status_code=302)
