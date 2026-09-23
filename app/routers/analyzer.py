"""Роутер анализатора цен: страница и API-обработка запроса."""
from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import BASE_DIR
from app.database import get_db
from app.deps import get_current_user
from app.services.market_search import MarketSearch, analyze_prices
from app.services.photo_search import YandexPhotoSearch, save_upload
from app.services.recommender import recommend

router = APIRouter()
templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))

MAX_PHOTO_SIZE = 10 * 1024 * 1024  # 10 МБ


@router.get("/analyzer")
async def analyzer_page(request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    # Подставляем средние значения из Seller API (реальные комиссии/логистика продавца)
    defaults = await get_seller_defaults(db, user.id)

    # Проверяем, подключён ли Bright Data (для точных цен Ozon)
    from app.models import ProxySetting
    proxy_result = await db.execute(
        select(ProxySetting).where(ProxySetting.user_id == user.id).limit(1)
    )
    proxy_setting = proxy_result.scalar_one_or_none()

    return templates.TemplateResponse("analyzer.html", {
        "request": request, "user": user, "flashes": [],
        "defaults": defaults, "proxy_setting": proxy_setting,
    })


@router.get("/analyzer/history")
async def analyzer_history(request: Request, db: AsyncSession = Depends(get_db)):
    """История поисков анализатора: фото, найденные товары, цены, фото ссылок."""
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    from app.models import AnalyzerHistory

    result = await db.execute(
        select(AnalyzerHistory)
        .where(AnalyzerHistory.user_id == user.id)
        .order_by(AnalyzerHistory.ts.desc())
        .limit(100)
    )
    records = list(result.scalars().all())

    # Превращаем JSON-поля в структуры для шаблона
    import json as _json
    history_items = []
    for rec in records:
        try:
            items = _json.loads(rec.items_json or "[]")
        except Exception:
            items = []
        try:
            stats = _json.loads(rec.stats_json or "{}")
        except Exception:
            stats = {}
        try:
            photo_prices = _json.loads(rec.photo_prices or "[]")
        except Exception:
            photo_prices = []
        try:
            photo_urls = _json.loads(rec.photo_urls_json or "[]")
        except Exception:
            photo_urls = []
        if not photo_urls and rec.photo_url:
            photo_urls = [rec.photo_url]
        history_items.append({
            "id": rec.id,
            "query": rec.query,
            "photo_url": rec.photo_url,
            "photo_urls": photo_urls,
            "photo_prices": photo_prices,
            "goods": items,
            "stats": stats,
            "ts": rec.ts,
            "total": len(items),
        })

    return templates.TemplateResponse("analyzer_history.html", {
        "request": request, "user": user, "flashes": [],
        "history": history_items,
    })


@router.get("/analyzer/history/export")
async def analyzer_history_export(request: Request, db: AsyncSession = Depends(get_db)):
    """Экспорт истории анализатора в CSV (все поиски с товарами и ценами)."""
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    import json as _json
    from app.models import AnalyzerHistory
    from app.services.csv_export import csv_response

    result = await db.execute(
        select(AnalyzerHistory)
        .where(AnalyzerHistory.user_id == user.id)
        .order_by(AnalyzerHistory.ts.desc())
        .limit(500)
    )
    records = list(result.scalars().all())

    rows = []
    for rec in records:
        try:
            items = _json.loads(rec.items_json or "[]")
        except Exception:
            items = []
        try:
            photo_prices = _json.loads(rec.photo_prices or "[]")
        except Exception:
            photo_prices = []
        for it in items:
            rows.append({
                "Время": rec.ts,
                "Запрос": rec.query,
                "Название": it.get("title", ""),
                "Маркетплейс": it.get("marketplace", ""),
                "Цена": it.get("price", 0),
                "Ссылка": it.get("url", ""),
                "Фото товара": it.get("image", ""),
            })
        # Если товаров нет — хотя бы строка с ценами из выдачи
        if not items:
            rows.append({
                "Время": rec.ts,
                "Запрос": rec.query,
                "Название": "",
                "Маркетплейс": "",
                "Цена": "",
                "Ссылка": "",
                "Фото товара": "",
                "Цены из выдачи": "; ".join(f"{p:.0f} ₽" for p in photo_prices[:20]),
            })

    return csv_response(
        rows,
        fieldnames=["Время", "Запрос", "Название", "Маркетплейс", "Цена",
                     "Ссылка", "Фото товара", "Цены из выдачи"],
        filename="analyzer_history.csv",
    )


async def get_seller_defaults(db: AsyncSession, user_id: int) -> dict:
    """Реальные комиссии по категориям + средняя логистика/эквайринг/выкуп (из Seller API)."""
    from sqlalchemy import func, select
    from app.models import ProductInfo

    # Уникальные комиссии (категорийные значения из Seller API)
    result = await db.execute(
        select(ProductInfo.commission_pct)
        .where(ProductInfo.user_id == user_id, ProductInfo.commission_pct > 0)
        .group_by(ProductInfo.commission_pct)
        .order_by(ProductInfo.commission_pct)
    )
    commissions = [round(float(r[0]), 1) for r in result.all()]

    # Средняя логистика, эквайринг, выкуп
    result = await db.execute(
        select(
            func.avg(ProductInfo.logistics_cost),
            func.avg(ProductInfo.acquiring_pct),
            func.avg(ProductInfo.buyout_pct),
        ).where(ProductInfo.user_id == user_id, ProductInfo.commission_pct > 0)
    )
    row = result.one()
    return {
        "commissions": commissions or [20.0],
        "default_commission": commissions[-1] if commissions else 20.0,
        "logistics_cost": round(row[0], 2) if row[0] else 50.0,
        "acquiring_pct": round(row[1], 2) if row[1] else 1.5,
        "buyout_pct": round(row[2], 1) if row[2] else 100.0,
    }


# Защита от параллельных дублей: {user_id: True} пока анализ выполняется
_running_analyses: dict[int, bool] = {}


@router.post("/analyzer/api")
async def analyzer_api(request: Request, db: AsyncSession = Depends(get_db)):
    """Принимает заявку на анализ и запускает его В ФОНЕ.

    Отвечает мгновенно: результат анализа появится в Истории
    (плюс уведомление на дашборде). Страницу можно закрыть.
    """
    user = await get_current_user(request, db)
    if not user:
        from fastapi.responses import JSONResponse
        return JSONResponse({"ok": False, "error": "Требуется вход. Обновите страницу и войдите."}, status_code=401)

    form = await request.form()
    product_name = (form.get("product_name") or "").strip()
    ozon_url = (form.get("ozon_url") or "").strip()
    economics = {
        "cost_price": _float(form.get("cost_price")),
        "commission_pct": _float(form.get("commission_pct"), default=20.0),
        "logistics_cost": _float(form.get("logistics_cost"), default=50.0),
        "acquiring_pct": _float(form.get("acquiring_pct"), default=1.5),
        "buyout_pct": _float(form.get("buyout_pct"), default=80.0),
        "min_margin_pct": _float(form.get("min_margin_pct"), default=10.0),
        "category": (form.get("category") or "").strip(),
    }

    # Сохраняем фото сразу (upload-объекты живут только в этом запросе)
    photo_urls: list[str] = []  # относительные пути (/static-uploads/...)
    photos = form.getlist("photos") if hasattr(form, "getlist") else []
    if not photos:
        single = form.get("photo")
        if single and hasattr(single, "filename") and single.filename:
            photos = [single]
    for photo in photos:
        if not (photo and hasattr(photo, "filename") and photo.filename):
            continue
        try:
            data = await photo.read()
            if len(data) > MAX_PHOTO_SIZE:
                continue
            ext = (photo.filename.rsplit(".", 1)[-1] or "jpg").lower()
            if ext not in ("jpg", "jpeg", "png", "webp"):
                ext = "jpg"
            photo_urls.append(save_upload(data, ext))
        except Exception:
            continue

    if not product_name and not photo_urls:
        return {"ok": False, "error": "Укажите название товара или загрузите фото"}

    # Защита: не запускаем второй анализ, пока первый не закончился
    if _running_analyses.get(user.id):
        return {"ok": False,
                "error": "Предыдущий анализ ещё выполняется — результат будет в Истории"}

    _running_analyses[user.id] = True

    # Абсолютные URL фото (домен вычисляем сейчас, request живёт только здесь)
    base_url = _base_url(request)
    abs_photo_urls = [base_url + p for p in photo_urls]

    import asyncio
    asyncio.create_task(_run_analysis(
        user_id=user.id,
        product_name=product_name,
        ozon_url=ozon_url,
        economics=economics,
        photo_urls=photo_urls,
        abs_photo_urls=abs_photo_urls,
    ))

    return {
        "ok": True,
        "queued": True,
        "message": "Анализ запущен в фоне. Можете закрыть страницу — результат появится в Истории (1-3 минуты).",
    }


async def _run_analysis(
    user_id: int,
    product_name: str,
    ozon_url: str,
    economics: dict,
    photo_urls: list[str],
    abs_photo_urls: list[str],
) -> None:
    """Фоновый анализ: поиск по фото + маркетплейсы + Bright Data → История + уведомление."""
    import logging
    log = logging.getLogger("analyzer")
    try:
        # Своя сессия БД (сессия HTTP-запроса уже закрыта)
        from app.database import async_session
        async with async_session() as db:
            photo_links: list[dict] = []
            photo_prices: list[float] = []
            seen_urls: set[str] = set()

            # 1. Поиск по каждому фото в Яндекс.Картинках
            for abs_url in abs_photo_urls:
                try:
                    searcher = YandexPhotoSearch()
                    try:
                        result = await searcher.search_by_url(abs_url)
                        for link in result.links:
                            if link.url not in seen_urls:
                                seen_urls.add(link.url)
                                photo_links.append({
                                    "url": link.url, "marketplace": link.marketplace,
                                    "title": link.title, "price": link.price, "image": link.image,
                                })
                        for p in result.prices:
                            if p not in photo_prices:
                                photo_prices.append(p)
                    finally:
                        await searcher.close()
                except Exception as e:
                    log.warning("Ошибка поиска по фото: %s", e)

            # 2. Текстовый поиск по маркетплейсам
            from app.models import ProxySetting
            proxy_result = await db.execute(
                select(ProxySetting).where(ProxySetting.user_id == user_id).limit(1)
            )
            proxy_setting = proxy_result.scalar_one_or_none()

            searcher = MarketSearch()
            try:
                query = product_name or "товар"
                results = await searcher.search_all(query, limit=15)
            finally:
                await searcher.close()

            all_prices: list[float] = []
            for r in results:
                if r.ok and r.prices:
                    all_prices.extend(p.price for p in r.prices)
            if photo_prices:
                all_prices.extend(photo_prices)

            # 3. Bright Data: точные цены Ozon по URL карточек
            bd_prices: list[float] = []
            if proxy_setting and proxy_setting.bd_api_key and proxy_setting.bd_dataset_id:
                ozon_urls = [
                    link["url"] for link in photo_links
                    if "ozon" in (link.get("url") or "").lower()
                ]
                if ozon_url and "ozon" in ozon_url.lower() and ozon_url not in ozon_urls:
                    ozon_urls.append(ozon_url)
                ozon_urls = ozon_urls[:10]
                if ozon_urls:
                    from app.services.bright_data import (
                        BrightDataError, extract_price, fetch_prices_by_urls,
                    )
                    try:
                        records = await fetch_prices_by_urls(
                            proxy_setting.bd_api_key, proxy_setting.bd_dataset_id, ozon_urls
                        )
                        for rec in records:
                            price = extract_price(rec)
                            if price:
                                bd_prices.append(price)
                        all_prices.extend(bd_prices)
                    except (BrightDataError, Exception) as e:
                        log.warning("Bright Data: %s", e)

            # 4. Анализ и рекомендация
            analysis = analyze_prices(all_prices, bucket_size=100.0)
            rec = recommend(
                analysis["recommended_price"],
                cost_price=economics["cost_price"],
                commission_pct=economics["commission_pct"],
                logistics_cost=economics["logistics_cost"],
                acquiring_pct=economics["acquiring_pct"],
                buyout_pct=economics["buyout_pct"],
                min_margin_pct=economics["min_margin_pct"],
                category_name=economics["category"],
            )

            # 5. Сохранение в Историю
            import json as _json
            from app.models import AnalyzerHistory, Notification
            items_for_history = [
                {
                    "url": link.get("url", ""),
                    "marketplace": link.get("marketplace", ""),
                    "title": link.get("title", ""),
                    "price": link.get("price", 0),
                    "image": link.get("image", ""),
                }
                for link in photo_links
            ]
            seen_item_urls = {it["url"] for it in items_for_history if it["url"]}
            for r in results:
                if not (r.ok and r.prices):
                    continue
                for p in r.prices:
                    if not p.url or p.url in seen_item_urls:
                        continue
                    seen_item_urls.add(p.url)
                    items_for_history.append({
                        "url": p.url, "marketplace": p.marketplace,
                        "title": p.name, "price": p.price, "image": "",
                    })

            stats = {
                "total": analysis["total"], "median": analysis["median"],
                "mean": analysis["mean"], "min": analysis["min"],
                "max": analysis["max"], "recommended": analysis["recommended_price"],
            }
            history = AnalyzerHistory(
                user_id=user_id,
                query=product_name,
                photo_url=photo_urls[0] if photo_urls else "",
                photo_urls_json=_json.dumps(photo_urls, ensure_ascii=False),
                photo_prices=_json.dumps(photo_prices, ensure_ascii=False),
                items_json=_json.dumps(items_for_history, ensure_ascii=False),
                stats_json=_json.dumps(stats, ensure_ascii=False),
            )
            db.add(history)

            # 6. Уведомление: анализ готов
            title = product_name or "по фото"
            db.add(Notification(
                user_id=user_id, level="info",
                message=f"Анализ «{title}» готов: {analysis['total']} цен, "
                        f"медиана {analysis['median']:.0f} ₽. "
                        f"Смотреть: История анализатора.",
            ))
            await db.commit()
            log.info("Фоновый анализ для user %s завершён: %d цен", user_id, analysis["total"])
    except Exception as e:
        log.error("Фоновый анализ упал (user %s): %s", user_id, e)
        try:
            from app.database import async_session
            from app.models import Notification
            async with async_session() as db:
                db.add(Notification(
                    user_id=user_id, level="warning",
                    message=f"Анализ «{product_name or 'по фото'}» завершился ошибкой: {str(e)[:150]}",
                ))
                await db.commit()
        except Exception:
            pass
    finally:
        _running_analyses.pop(user_id, None)


def _base_url(request: Request) -> str:
    """Базовый публичный URL с учётом reverse-proxy (для фото, которые читает Яндекс)."""
    scheme = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.hostname
    return f"{scheme}://{host}"



def _float(value, default: float = 0.0) -> float:
    try:
        return float(value) if value not in (None, "") else default
    except (TypeError, ValueError):
        return default


