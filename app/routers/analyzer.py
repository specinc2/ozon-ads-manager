"""Роутер анализатора цен: страница и API-обработка запроса."""
from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import BASE_DIR
from app.database import get_db
from app.deps import get_current_user
from app.services.market_search import MarketSearch, analyze_prices
from app.services.recommender import recommend

router = APIRouter()
templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))


@router.get("/analyzer")
async def analyzer_page(request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    return templates.TemplateResponse("analyzer.html", {"request": request, "user": user, "flashes": []})


@router.post("/analyzer/api")
async def analyzer_api(request: Request, db: AsyncSession = Depends(get_db)):
    """Обрабатывает запрос анализа: поиск цен + рекомендации."""
    user = await get_current_user(request, db)
    if not user:
        return {"ok": False, "error": "Требуется вход"}

    form = await request.form()
    product_name = (form.get("product_name") or "").strip()
    cost_price = _float(form.get("cost_price"))
    commission_pct = _float(form.get("commission_pct"), default=20.0)
    logistics_cost = _float(form.get("logistics_cost"), default=50.0)
    acquiring_pct = _float(form.get("acquiring_pct"), default=1.5)
    buyout_pct = _float(form.get("buyout_pct"), default=80.0)
    min_margin_pct = _float(form.get("min_margin_pct"), default=10.0)
    category = (form.get("category") or "").strip()

    if not product_name:
        return {"ok": False, "error": "Укажите название товара"}

    # Поиск цен по маркетплейсам
    searcher = MarketSearch()
    try:
        results = await searcher.search_all(product_name, limit=20)
    finally:
        await searcher.close()

    # Собираем все цены (только из успешных источников)
    all_prices: list[float] = []
    sources = []
    for r in results:
        if r.ok and r.prices:
            sources.append(r.marketplace)
            all_prices.extend(p.price for p in r.prices)

    analysis = analyze_prices(all_prices, bucket_size=100.0)

    # Рекомендация
    rec = recommend(
        analysis["recommended_price"],
        cost_price=cost_price,
        commission_pct=commission_pct,
        logistics_cost=logistics_cost,
        acquiring_pct=acquiring_pct,
        buyout_pct=buyout_pct,
        min_margin_pct=min_margin_pct,
        category_name=category,
    )

    # Готовим данные для шаблона
    source_status = {
        r.marketplace: {"ok": r.ok, "count": len(r.prices), "error": r.error}
        for r in results
    }

    return {
        "ok": True,
        "product_name": product_name,
        "sources": source_status,
        "buckets": [
            {"label": b.label, "count": b.count, "percent": b.percent}
            for b in analysis["buckets"]
        ],
        "stats": {
            "total": analysis["total"],
            "median": analysis["median"],
            "mean": analysis["mean"],
            "min": analysis["min"],
            "max": analysis["max"],
            "recommended": analysis["recommended_price"],
        },
        "recommendation": {
            "price": rec.recommended_price,
            "margin_per_unit": rec.margin_per_unit,
            "margin_pct": rec.margin_pct,
            "breakeven_drr": rec.breakeven_drr,
            "ad_verdict": rec.ad_verdict,
            "ad_reason": rec.ad_reason,
            "category_hint": rec.category_hint,
            "summary": rec.summary,
        },
    }


def _float(value, default: float = 0.0) -> float:
    try:
        return float(value) if value not in (None, "") else default
    except (TypeError, ValueError):
        return default
