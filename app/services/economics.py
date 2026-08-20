"""Расчёт экономики товара: маржа, ДРР от всего оборота, рекомендации для бидера.

Учитывает:
- себестоимость товара;
- комиссию маркетплейса (% от цены);
- логистику (₽/шт);
- эквайринг (% от цены);
- % выкупа (возвраты снижают реальную выручку);
- участие в акции (скидка уменьшает цену продажи).

Все расчёты в рублях на единицу товара, если не указано иное.
"""
from dataclasses import dataclass, field

from app.models import ProductInfo


@dataclass
class ProductEconomics:
    """Результат расчёта экономики одного товара."""

    sku: str
    name: str = ""
    price: float = 0.0
    cost_price: float = 0.0
    leftovers: int = 0
    fulfillment_type: str = "FBO"
    commission_pct: float = 0.0
    logistics_cost: float = 0.0
    acquiring_pct: float = 0.0
    buyout_pct: float = 100.0
    in_promotion: bool = False
    promotion_discount_pct: float = 0.0
    min_margin_pct: float = 10.0
    monthly_orders: int = 0
    monthly_revenue: float = 0.0

    # --- Рассчитанные значения ---
    effective_price: float = 0.0          # цена с учётом скидки акции
    commission: float = 0.0               # комиссия ₽/шт
    acquiring: float = 0.0                # эквайринг ₽/шт
    total_costs: float = 0.0              # все переменные расходы ₽/шт
    margin_per_unit: float = 0.0          # валовая маржа на проданную единицу, ₽
    margin_per_ordered: float = 0.0       # маржа с учётом % выкупа, ₽
    margin_pct: float = 0.0               # маржинальность, % от цены
    margin_pct_of_ordered: float = 0.0    # маржинальность с учётом выкупа
    breakeven_spend_per_unit: float = 0.0  # сколько ₽ рекламы допустимо на 1 продажу

    # --- Реклама (заполняется снаружи) ---
    ad_spend: float = 0.0
    ad_revenue: float = 0.0
    ad_orders: int = 0
    total_revenue: float = 0.0            # весь оборот за месяц
    drr_of_total: float = 0.0             # ДРР от всего оборота, %
    ad_margin_impact: float = 0.0         # как реклама влияет на маржу, ₽
    margin_with_ads: float = 0.0          # маржа с учётом рекламных расходов, ₽/шт
    needs_bid_change: str = ""            # рекомендация: raise / lower / hold
    suggested_bid: float | None = None

    # --- Низкая посещаемость ---
    low_traffic: bool = False


def from_info(info: ProductInfo | None, sku: str = "", name: str = "") -> ProductEconomics:
    """Строит расчёт из записи ProductInfo (или пустой, если товар не заведён)."""
    if info is not None:
        return ProductEconomics(
            sku=info.sku,
            name=info.name or name,
            price=info.price,
            cost_price=info.cost_price,
            leftovers=info.leftovers,
            fulfillment_type=info.fulfillment_type,
            commission_pct=info.commission_pct,
            logistics_cost=info.logistics_cost,
            acquiring_pct=info.acquiring_pct,
            buyout_pct=info.buyout_pct,
            in_promotion=info.in_promotion,
            promotion_discount_pct=info.promotion_discount_pct,
            min_margin_pct=info.min_margin_pct,
            monthly_orders=info.monthly_orders,
            monthly_revenue=info.monthly_revenue,
        )
    return ProductEconomics(sku=sku, name=name)


def calculate(econ: ProductEconomics) -> ProductEconomics:
    """Заполняет рассчитанные поля экономики товара."""
    price = max(econ.price, 0.0)
    cost = max(econ.cost_price, 0.0)

    # Цена с учётом скидки акции
    discount = econ.promotion_discount_pct if econ.in_promotion else 0.0
    effective_price = price * (1 - discount / 100)
    econ.effective_price = effective_price

    # Переменные расходы на единицу
    commission = effective_price * econ.commission_pct / 100
    acquiring = effective_price * econ.acquiring_pct / 100
    logistics = max(econ.logistics_cost, 0.0)
    econ.commission = commission
    econ.acquiring = acquiring
    econ.total_costs = commission + acquiring + logistics + cost

    # Валовая маржа на проданную единицу (без учёта возвратов)
    margin = effective_price - econ.total_costs
    econ.margin_per_unit = margin
    econ.margin_pct = margin / effective_price * 100 if effective_price else 0.0

    # С учётом % выкупа: невыкупленные единицы приносят убыток (логистика/комиссия потрачены)
    buyout = max(min(econ.buyout_pct, 100), 0) / 100
    # На 1 отправленную единицу: выкуп — buyout*эффект.цена, расходы — все (cost+логистика+комиссия+эквайринг)
    # Комиссия и эквайринг возвращаются при невыкупе частично — упрощаем: считаем расходы полностью
    margin_ordered = buyout * effective_price - econ.total_costs
    econ.margin_per_ordered = margin_ordered
    econ.margin_pct_of_ordered = margin_ordered / effective_price * 100 if effective_price else 0.0

    # Сколько рекламы можно потратить на 1 продажу, сохраняя минимальную маржу
    min_margin_pct = econ.min_margin_pct or 0.0
    min_margin_rub = effective_price * min_margin_pct / 100
    econ.breakeven_spend_per_unit = max(margin_ordered - min_margin_rub, 0.0)

    # ДРР от всего оборота (рекламный расход / весь оборот артикула)
    total_revenue = econ.total_revenue or econ.monthly_revenue or 0.0
    if total_revenue > 0:
        econ.drr_of_total = econ.ad_spend / total_revenue * 100
    elif econ.ad_spend > 0:
        econ.drr_of_total = 100.0  # расхода нет — ДРР неопределён, помечаем как максимум
    econ.total_revenue = total_revenue

    # Влияние рекламы: маржа товара минус рекламный расход на единицу
    # (расход на 1 заказ = ad_spend / ad_orders, если заказы есть)
    if econ.ad_orders > 0:
        spend_per_order = econ.ad_spend / econ.ad_orders
        econ.ad_margin_impact = spend_per_order
        econ.margin_with_ads = margin_ordered - spend_per_order
    else:
        econ.ad_margin_impact = 0.0
        econ.margin_with_ads = margin_ordered

    return econ


def recommend_bid(econ: ProductEconomics, *, max_bid: float = 0.0, step_pct: float = 10.0) -> str:
    """Простая рекомендация по ставке на основе ДРР от оборота и маржи.

    Если реклама съедает маржу (маржа с учётом рекламы < минимального порога) —
    ставку снижаем. Если маржа с учётом рекламы выше порога и товар имеет
    хороший CTR — ставку можно поднять (есть запас).
    """
    if econ.price <= 0:
        return "hold"
    min_margin_pct = econ.min_margin_pct or 10.0
    # Реклама съедает слишком большую долю оборота — снижаем
    if econ.drr_of_total > 25:
        return "lower"
    # Реклама съедает маржу ниже порога — снижаем
    if econ.margin_with_ads < econ.price * min_margin_pct / 100:
        return "lower"
    # Маржа в порядке, ДРР низкий — можно пробовать расти
    if econ.drr_of_total < 15:
        return "raise"
    return "hold"
