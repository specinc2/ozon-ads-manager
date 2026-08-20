"""Движок бидера — автоматическое управление ставками товаров.

Три стратегии:
1. target_drr — целевая ДРР от всего оборота артикула.
   Считаем: рекламный расход / (рекламная выручка + органическая выручка).
   Если ДРР выше цели — снижаем ставку; если ниже цели и есть запас маржи — повышаем.
2. maintain_position — поддержание позиции в выдаче при минимальном расходе.
   Через конкурентные ставки API узнаём, какие ставки у конкурентов, и держим
   свою чуть выше порога целевой позиции (топ-3/топ-10), но не тратим лишнего.
3. ai_test — итеративный подбор ставки (как «человек-аналитик»):
   начинаем с нижней ставки, шагами повышаем, анализируем CTR и ДРР,
   останавливаемся, когда ДРР достигает цели или CTR падает.
"""
import logging
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import BidderRule, Campaign, Product, ProductInfo
from app.services.economics import calculate, from_info
from app.services.logger import log_action
from app.services.ozon_client import OzonAPIError, OzonClient, parse_decimal

logger = logging.getLogger("bidder")


class BidderEngine:
    """Применяет активные правила бидера для одного пользователя."""

    def __init__(self, db: AsyncSession, user_id: int, client: OzonClient):
        self.db = db
        self.user_id = user_id
        self.client = client

    async def run_all(self) -> list[str]:
        """Применяет все активные правила. Возвращает список сработавших действий."""
        result = await self.db.execute(
            select(BidderRule).where(
                BidderRule.user_id == self.user_id,
                BidderRule.is_active.is_(True),
            )
        )
        rules = list(result.scalars().all())
        events: list[str] = []
        for rule in rules:
            try:
                event = await self._apply_rule(rule)
            except OzonAPIError as e:
                logger.warning("Бидер %s (%s): ошибка API: %s", rule.id, rule.name, e)
                continue
            if event:
                events.append(event)
        return events

    async def _apply_rule(self, rule: BidderRule) -> str | None:
        if rule.strategy == "target_drr":
            return await self._apply_target_drr(rule)
        if rule.strategy == "maintain_position":
            return await self._apply_maintain_position(rule)
        if rule.strategy == "ai_test":
            return await self._apply_ai_test(rule)
        return None

    # ------------------------------------------------------------------
    # Общие помощники
    # ------------------------------------------------------------------

    async def _rule_products(self, rule: BidderRule) -> list[Product]:
        """Товары, к которым применяется правило (по SKU или все)."""
        stmt = (
            select(Product)
            .join(Campaign, Campaign.id == Product.campaign_id)
            .where(Campaign.user_id == self.user_id)
        )
        if rule.campaign_id:
            stmt = stmt.where(Product.campaign_id == rule.campaign_id)
        if rule.sku:
            stmt = stmt.where(Product.sku == rule.sku)
        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    async def _get_info(self, sku: str) -> ProductInfo | None:
        result = await self.db.execute(
            select(ProductInfo).where(ProductInfo.user_id == self.user_id, ProductInfo.sku == sku)
        )
        return result.scalar_one_or_none()

    async def _set_bid(self, product: Product, new_bid: float) -> None:
        """Меняет ставку товара в Ozon и в кэше."""
        campaign = await self.db.get(Campaign, product.campaign_id)
        if campaign is None:
            return
        await self.client.update_products_bids(
            campaign.campaign_id, [{"sku": product.sku, "bid": round(new_bid, 2)}]
        )
        product.bid = round(new_bid, 2)
        await self.db.commit()

    async def _log_bid_change(self, rule: BidderRule, product: Product, old_bid: float,
                              new_bid: float, reason: str) -> None:
        await log_action(
            self.db, action="bidder_bid_change", user_id=self.user_id,
            entity_type="product", entity_name=product.name or product.sku,
            details={"rule": rule.name, "strategy": rule.strategy, "sku": product.sku,
                     "old_bid": old_bid, "new_bid": new_bid, "reason": reason},
            source="automation",
        )

    # ------------------------------------------------------------------
    # Стратегия 1: целевая ДРР от всего оборота
    # ------------------------------------------------------------------

    async def _apply_target_drr(self, rule: BidderRule) -> str | None:
        target_drr = float(rule.params.get("target_drr", 10.0))
        step_pct = float(rule.params.get("step_pct", 10.0))
        min_bid = float(rule.params.get("min_bid", 0))
        max_bid = float(rule.params.get("max_bid", 0))
        lookback_days = int(rule.params.get("lookback_days", 7))

        products = await self._rule_products(rule)
        events: list[str] = []
        changed = 0

        for product in products:
            info = await self._get_info(product.sku)
            econ = from_info(info, sku=product.sku, name=product.name)
            if econ.price <= 0 or econ.monthly_revenue <= 0:
                continue  # без экономики товара правило не работает

            # Расход и выручка за период из статистики товара (рекламные)
            ad_spend, ad_revenue, _ = await self._product_ad_stats(product, lookback_days)
            econ.ad_spend = ad_spend
            econ.ad_revenue = ad_revenue
            econ.ad_orders = await self._product_ad_orders(product, lookback_days)
            econ.total_revenue = econ.monthly_revenue  # весь оборот артикула за месяц
            calculate(econ)

            # Решение по ставке
            action = "hold"
            if econ.drr_of_total > target_drr * 1.2:
                action = "lower"
            elif econ.drr_of_total < target_drr * 0.5 and econ.margin_with_ads > 0:
                action = "raise"

            if action == "hold":
                continue

            old_bid = product.bid
            new_bid = old_bid
            if action == "lower":
                new_bid = old_bid * (1 - step_pct / 100)
            else:
                new_bid = old_bid * (1 + step_pct / 100)
            if min_bid:
                new_bid = max(new_bid, min_bid)
            if max_bid:
                new_bid = min(new_bid, max_bid)
            if abs(new_bid - old_bid) < 0.01:
                continue

            await self._set_bid(product, new_bid)
            await self._log_bid_change(
                rule, product, old_bid, new_bid,
                f"ДРР {econ.drr_of_total:.1f}% при цели {target_drr:.0f}%",
            )
            changed += 1

        if changed:
            rule.last_triggered_at = datetime.utcnow()
            await self.db.commit()
            events.append(f"[{rule.name}] изменено ставок: {changed} (ДРР-цель)")
        return "; ".join(events) if events else None

    # ------------------------------------------------------------------
    # Стратегия 2: поддержание позиции (конкурентные ставки)
    # ------------------------------------------------------------------

    async def _apply_maintain_position(self, rule: BidderRule) -> str | None:
        position = int(rule.params.get("position", 10))  # топ-3 / топ-10 / ...
        multiplier = float(rule.params.get("multiplier", 1.05))  # +5% к порогу
        min_bid = float(rule.params.get("min_bid", 0))
        max_bid = float(rule.params.get("max_bid", 0))

        products = await self._rule_products(rule)
        # Группируем по кампаниям
        by_campaign: dict[int, list[Product]] = {}
        for p in products:
            by_campaign.setdefault(p.campaign_id, []).append(p)

        events: list[str] = []
        changed = 0
        for campaign_pk, camp_products in by_campaign.items():
            campaign = await self.db.get(Campaign, campaign_pk)
            if campaign is None:
                continue
            skus = [p.sku for p in camp_products]
            try:
                bids = await self.client.get_competitive_bids(campaign.campaign_id, skus)
            except OzonAPIError:
                continue  # конкурентные ставки доступны не для всех кампаний

            bid_map = {str(b.get("sku")): b.get("bid") for b in bids}
            for product in camp_products:
                comp = bid_map.get(str(product.sku))
                if comp is None:
                    continue
                try:
                    comp_value = parse_decimal(comp)
                except (TypeError, ValueError):
                    continue
                if comp_value <= 0:
                    continue

                # Целевая ставка: чуть выше N-й конкурентной ставки
                target = comp_value * multiplier
                if min_bid:
                    target = max(target, min_bid)
                if max_bid:
                    target = min(target, max_bid)

                old_bid = product.bid
                # Если уже достаточно для позиции — не тратим лишнего
                if old_bid >= comp_value * 0.95 and old_bid <= target:
                    continue
                await self._set_bid(product, target)
                await self._log_bid_change(
                    rule, product, old_bid, target,
                    f"поддержание топ-{position} (конкурентная ставка {comp_value})",
                )
                changed += 1

        if changed:
            rule.last_triggered_at = datetime.utcnow()
            await self.db.commit()
            events.append(f"[{rule.name}] изменено ставок: {changed} (позиция топ-{position})")
        return "; ".join(events) if events else None

    # ------------------------------------------------------------------
    # Стратегия 3: ИИ-подбор (итеративное тестирование ставок)
    # ------------------------------------------------------------------

    async def _apply_ai_test(self, rule: BidderRule) -> str | None:
        start_bid = float(rule.params.get("start_bid", 30.0))
        step_pct = float(rule.params.get("step_pct", 20.0))
        max_bid = float(rule.params.get("max_bid", 0))
        min_bid = float(rule.params.get("min_bid", 0))
        target_drr = float(rule.params.get("target_drr", 10.0))
        min_ctr = float(rule.params.get("min_ctr", 0.5))  # если CTR ниже — снижаем
        lookback_days = int(rule.params.get("lookback_days", 3))

        products = await self._rule_products(rule)
        events: list[str] = []
        changed = 0

        for product in products:
            info = await self._get_info(product.sku)
            econ = from_info(info, sku=product.sku, name=product.name)
            ad_spend, ad_revenue, impressions = await self._product_ad_stats(product, lookback_days)
            clicks = await self._product_ad_clicks(product, lookback_days)

            ctr = clicks / impressions * 100 if impressions else 0.0
            drr = ad_spend / econ.monthly_revenue * 100 if econ.monthly_revenue > 0 else (
                100.0 if ad_spend > 0 else 0.0
            )

            # Если нет показов — ставка слишком низкая, повышаем до стартовой
            if impressions == 0:
                if product.bid < start_bid:
                    old_bid = product.bid
                    await self._set_bid(product, start_bid)
                    await self._log_bid_change(rule, product, old_bid, start_bid,
                                               "нет показов — поднимаем до стартовой")
                    changed += 1
                continue

            # Анализ результатов теста
            action = "hold"
            if ctr < min_ctr and impressions > 50:
                action = "lower"  # нерелевантно — снижаем
            elif drr > target_drr:
                action = "lower"  # перерасход
            elif drr < target_drr * 0.6 and ctr >= min_ctr:
                action = "raise"  # есть запас — пробуем выше

            if action == "hold":
                continue

            old_bid = product.bid
            new_bid = old_bid * (1 - step_pct / 100) if action == "lower" else old_bid * (1 + step_pct / 100)
            if min_bid:
                new_bid = max(new_bid, min_bid)
            if max_bid:
                new_bid = min(new_bid, max_bid)
            if abs(new_bid - old_bid) < 0.01:
                continue

            await self._set_bid(product, new_bid)
            await self._log_bid_change(
                rule, product, old_bid, new_bid,
                f"CTR {ctr:.2f}%, ДРР {drr:.1f}% (цель {target_drr:.0f}%)",
            )
            changed += 1

        if changed:
            rule.last_triggered_at = datetime.utcnow()
            await self.db.commit()
            events.append(f"[{rule.name}] изменено ставок: {changed} (ИИ-подбор)")
        return "; ".join(events) if events else None

    # ------------------------------------------------------------------
    # Статистика товара за период (из кэша campaign_stats)
    # ------------------------------------------------------------------

    async def _product_ad_stats(self, product: Product, days: int) -> tuple[float, float, int]:
        """Суммарный расход, рекламная выручка и показы товара в кампании за N дней."""
        from app.models import CampaignStat
        from sqlalchemy import func

        cutoff = datetime.utcnow().date() - timedelta(days=days)
        result = await self.db.execute(
            select(
                func.coalesce(func.sum(CampaignStat.spend), 0),
                func.coalesce(func.sum(CampaignStat.revenue), 0),
                func.coalesce(func.sum(CampaignStat.impressions), 0),
            ).where(
                CampaignStat.campaign_id == product.campaign_id,
                CampaignStat.stat_date >= cutoff,
            )
        )
        spend, revenue, impressions = result.one()
        return float(spend), float(revenue), int(impressions)

    async def _product_ad_orders(self, product: Product, days: int) -> int:
        from app.models import CampaignStat
        from sqlalchemy import func

        cutoff = datetime.utcnow().date() - timedelta(days=days)
        result = await self.db.execute(
            select(func.coalesce(func.sum(CampaignStat.orders), 0)).where(
                CampaignStat.campaign_id == product.campaign_id,
                CampaignStat.stat_date >= cutoff,
            )
        )
        return int(result.scalar() or 0)

    async def _product_ad_clicks(self, product: Product, days: int) -> int:
        from app.models import CampaignStat
        from sqlalchemy import func

        cutoff = datetime.utcnow().date() - timedelta(days=days)
        result = await self.db.execute(
            select(func.coalesce(func.sum(CampaignStat.clicks), 0)).where(
                CampaignStat.campaign_id == product.campaign_id,
                CampaignStat.stat_date >= cutoff,
            )
        )
        return int(result.scalar() or 0)
