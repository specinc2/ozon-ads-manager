# -*- coding: utf-8 -*-
"""ABC-анализ товаров: кто приносит прибыль, кто жжёт рекламу.

Классический ABC по вкладу в выручку (Парето):
- A — товары, дающие первые 80% накопленной выручки;
- B — следующие 15% (80–95%);
- C — хвост (последние 5%).

Дополнительно каждый товар помечается «жжёт рекламу», если фактический ДРР
от оборота выше допустимого (маржинальность с выкупом минус минимальная маржа).

Итоговые группы:
- A / B / C — классика по выручке;
- burn-флаг — реклама дороже допустимой (перерасход = ad_spend − лимит);
- no_data — нет ни выручки, ни расходов.
"""
from dataclasses import dataclass, field


@dataclass
class AbcRow:
    sku: str
    name: str
    group: str = ""            # A / B / C / no_data
    revenue: float = 0.0       # весь оборот за 30 дней, ₽
    cum_pct: float = 0.0       # накопленный % выручки
    ad_spend: float = 0.0      # рекламный расход за 30 дней, ₽
    drr: float = 0.0           # фактический ДРР от оборота, %
    drr_limit: float = 0.0     # допустимый ДРР (маржа − мин. маржа), %
    margin_pct: float = 0.0    # маржинальность с выкупом, %
    margin_month: float = 0.0  # валовая маржа за месяц, ₽
    ad_orders: int = 0
    burn: bool = False         # жжёт рекламу
    overspend: float = 0.0     # перерасход рекламы относительно лимита, ₽/мес
    in_ad: bool = False
    verdict: str = ""          # рекомендация человеческим языком
    low_traffic: bool = False


@dataclass
class AbcResult:
    rows: list[AbcRow] = field(default_factory=list)
    total_revenue: float = 0.0
    total_ad_spend: float = 0.0
    total_margin: float = 0.0
    overspend_total: float = 0.0          # суммарный перерасход по burn-товарам
    groups: dict = field(default_factory=dict)  # {A: {count, revenue, ad_spend, burn, overspend}, ...}
    burn_count: int = 0


def analyze(cards: list[dict]) -> AbcResult:
    """ABC-анализ по карточкам товаров (формат страницы «Товары»).

    cards: [{info, econ, in_ad, ...}] — econ уже рассчитан (calculate()).
    """
    result = AbcResult()

    # 1. Собираем метрики
    items: list[AbcRow] = []
    for c in cards:
        econ = c.get("econ")
        info = c.get("info")
        if econ is None or info is None:
            continue
        revenue = float(econ.total_revenue or 0)
        ad_spend = float(econ.ad_spend or 0)

        # Допустимый ДРР: маржинальность с выкупом − минимальная маржа (но ≥ 0)
        margin_pct = float(econ.margin_pct_of_ordered or 0)
        min_margin = float(econ.min_margin_pct or 0)
        drr_limit = max(margin_pct - min_margin, 0.0)

        drr = float(econ.drr_of_total or 0)
        # Перерасход: сколько реклама превышает допустимый уровень
        overspend = 0.0
        if revenue > 0 and drr_limit < 100:
            allowed_spend = revenue * drr_limit / 100
            overspend = max(ad_spend - allowed_spend, 0.0)
        elif ad_spend > 0:
            overspend = ad_spend  # выручки нет, а реклама идёт — всё в перерасход

        # Маржа за месяц: маржа с выкупом × все заказы (если заказы есть)
        orders_total = int(getattr(econ, "monthly_orders", 0) or econ.ad_orders or 0)
        margin_month = float(econ.margin_per_ordered or 0) * orders_total

        row = AbcRow(
            sku=str(info.sku),
            name=info.name or str(info.sku),
            revenue=revenue,
            ad_spend=ad_spend,
            drr=drr,
            drr_limit=drr_limit,
            margin_pct=margin_pct,
            margin_month=margin_month,
            ad_orders=int(econ.ad_orders or 0),
            burn=(ad_spend > 0 and overspend > 0),
            overspend=overspend,
            in_ad=bool(c.get("in_ad")),
            low_traffic=bool(getattr(econ, "low_traffic", False)),
        )
        items.append(row)

    # 2. ABC по выручке (только товары с выручкой)
    with_revenue = sorted([r for r in items if r.revenue > 0],
                          key=lambda r: r.revenue, reverse=True)
    total = sum(r.revenue for r in with_revenue)
    result.total_revenue = total
    cum = 0.0
    for r in with_revenue:
        cum += r.revenue
        r.cum_pct = cum / total * 100 if total else 0
        r.group = "A" if r.cum_pct <= 80 else ("B" if r.cum_pct <= 95 else "C")

    # Без выручки — отдельная группа
    for r in items:
        if r.revenue <= 0:
            r.group = "no_data"

    # 3. Вердикты
    for r in items:
        if r.group == "no_data":
            r.verdict = "Нет данных: синхронизируйте Seller API" if not r.in_ad \
                else "Реклама без выручки — проверить товар"
        elif r.burn:
            if r.group == "A":
                r.verdict = "Жжёт рекламу: снизить ставки (бидер)"
            elif r.group == "B":
                r.verdict = "Перерасход: снизить ставки или пауза"
            else:
                r.verdict = "Кандидат на отключение рекламы"
        else:
            if r.group == "A":
                r.verdict = "Звезда: можно масштабировать"
            elif r.group == "B":
                r.verdict = "Стабильный середняк"
            else:
                r.verdict = "Хвост: реклама не критична"

    # 4. Сводка по группам
    for g in ("A", "B", "C", "no_data"):
        grp = [r for r in items if r.group == g]
        result.groups[g] = {
            "count": len(grp),
            "revenue": sum(r.revenue for r in grp),
            "ad_spend": sum(r.ad_spend for r in grp),
            "burn": sum(1 for r in grp if r.burn),
            "overspend": sum(r.overspend for r in grp),
        }

    # 5. Итоги
    result.rows = sorted(items, key=lambda r: (-r.revenue, -r.ad_spend))
    result.total_ad_spend = sum(r.ad_spend for r in items)
    result.total_margin = sum(r.margin_month for r in items)
    result.overspend_total = sum(r.overspend for r in items if r.burn)
    result.burn_count = sum(1 for r in items if r.burn)
    return result
