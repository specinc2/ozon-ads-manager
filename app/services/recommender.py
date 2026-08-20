"""Рекомендации по цене и рекламе на основе анализа рынка и экономики товара.

Использует те же принципы, что и economics.py, но для сценария «новый товар»:
- рекомендуемая цена из анализа вилок;
- маржа с учётом себестоимости, комиссии Ozon (по категории), логистики, эквайринга;
- рекомендация по рекламе: входить или нет, какой ДРР допустим.
"""
from dataclasses import dataclass


@dataclass
class Recommendation:
    recommended_price: float | None
    margin_per_unit: float = 0.0
    margin_pct: float = 0.0
    breakeven_drr: float = 0.0          # макс. допустимая доля рекламы от оборота
    ad_verdict: str = ""                # "enter" / "careful" / "skip"
    ad_reason: str = ""
    category_hint: str = ""
    summary: str = ""


def recommend(
    recommended_price: float | None,
    *,
    cost_price: float = 0.0,
    commission_pct: float = 20.0,
    logistics_cost: float = 50.0,
    acquiring_pct: float = 1.5,
    buyout_pct: float = 80.0,
    min_margin_pct: float = 10.0,
    category_name: str = "",
) -> Recommendation:
    """Строит рекомендацию для нового товара."""
    rec = Recommendation(recommended_price=recommended_price)

    if not recommended_price or recommended_price <= 0:
        rec.summary = "Не удалось определить рекомендуемую цену — недостаточно данных о конкурентах."
        rec.ad_verdict = "skip"
        return rec

    price = recommended_price
    buyout = max(min(buyout_pct, 100), 0) / 100

    # Расходы на единицу
    commission = price * commission_pct / 100
    acquiring = price * acquiring_pct / 100
    logistics = max(logistics_cost, 0.0)
    total_costs = commission + acquiring + logistics + max(cost_price, 0.0)

    # Маржа на проданную единицу и с учётом выкупа
    margin_unit = price - total_costs
    margin_ordered = buyout * price - total_costs
    rec.margin_per_unit = round(margin_ordered, 2)
    rec.margin_pct = round(margin_ordered / price * 100, 1) if price else 0.0

    # Максимально допустимая ДРР = маржа с выкупом минус минимальная маржа
    min_margin_rub = price * min_margin_pct / 100
    rec.breakeven_drr = round(max(margin_ordered - min_margin_rub, 0) / price * 100, 1)

    # Вердикт по рекламе
    if rec.margin_pct <= 0:
        rec.ad_verdict = "skip"
        rec.ad_reason = (f"Маржа отрицательная ({rec.margin_pct}%) при цене {price:.0f} ₽ — "
                         f"продавать невыгодно. Поднимите цену или снизьте себестоимость.")
    elif rec.margin_pct < min_margin_pct:
        rec.ad_verdict = "careful"
        rec.ad_reason = (f"Маржа {rec.margin_pct}% ниже желаемого порога {min_margin_pct:.0f}%. "
                         f"В рекламу входить рискованно — допустимая ДРР всего {rec.breakeven_drr}%.")
    else:
        rec.ad_verdict = "enter"
        rec.ad_reason = (f"Маржа {rec.margin_pct}% достаточна. Можно входить в рекламу: "
                         f"следите, чтобы ДРР не превышал {rec.breakeven_drr}% от оборота.")

    cat = f"категория «{category_name}»" if category_name else "категория не указана"
    rec.category_hint = f"Комиссия Ozon для {cat}: {commission_pct:.0f}% (настраивается)."

    rec.summary = (
        f"Рекомендуемая цена старта: **{price:.0f} ₽**. "
        f"Маржа с учётом выкупа {buyout_pct:.0f}%: {rec.margin_per_unit:.0f} ₽ ({rec.margin_pct}%). "
        f"{rec.ad_reason}"
    )
    return rec
