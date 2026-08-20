"""Движок авто-правил: контроль бюджета и динамические ставки.

Правила:
- budget_notify: если расход >= порога% от дневного бюджета — уведомить.
- budget_stop:   если расход >= порога% (по умолчанию 100) — остановить кампанию.
- auto_bid:      если метрика (ctr/conversion/cpa) хуже порога — изменить ставку
                 (уменьшить/увеличить на % или фиксированную сумму).
"""
import logging
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AutomationRule, Campaign, Notification, Product
from app.services.campaigns import get_campaign_or_none
from app.services.logger import log_action
from app.services.products import update_bids
from app.services.ozon_client import OzonAPIError, OzonClient

logger = logging.getLogger("rules")


class RuleEngine:
    """Применяет все активные правила пользователя. Вызывается планировщиком."""

    def __init__(self, db: AsyncSession, user_id: int, client: OzonClient):
        self.db = db
        self.user_id = user_id
        self.client = client
        self._campaign_cache: dict[int, Campaign] = {}

    # ------------------------------------------------------------------
    # Основная точка входа
    # ------------------------------------------------------------------

    async def run_all(self) -> list[str]:
        """Применяет все активные правила. Возвращает список сработавших действий."""
        result = await self.db.execute(
            select(AutomationRule).where(
                AutomationRule.user_id == self.user_id,
                AutomationRule.is_active.is_(True),
            )
        )
        rules = list(result.scalars().all())
        events: list[str] = []
        for rule in rules:
            try:
                event = await self._apply_rule(rule)
            except OzonAPIError as e:
                logger.warning("Правило %s (%s): ошибка API: %s", rule.id, rule.name, e)
                await self._notify("danger", f"Правило «{rule.name}»: {e.message}")
                continue
            if event:
                events.append(event)
        return events

    async def _apply_rule(self, rule: AutomationRule) -> str | None:
        campaign = None
        if rule.campaign_id:
            campaign = await self._get_campaign(rule.campaign_id)
            if campaign is None:
                return None  # кампания удалена — правило просто пропускаем

        if rule.rule_type in ("budget_notify", "budget_stop"):
            return await self._apply_budget_rule(rule, campaign)
        if rule.rule_type == "auto_bid":
            return await self._apply_bid_rule(rule, campaign)
        return None

    # ------------------------------------------------------------------
    # Авто-бюджет
    # ------------------------------------------------------------------

    async def _apply_budget_rule(self, rule: AutomationRule, campaign: Campaign | None) -> str | None:
        campaigns = [campaign] if campaign else await self._all_campaigns()
        threshold = float(rule.params.get("threshold", 100))
        stop = rule.rule_type == "budget_stop"
        events: list[str] = []

        for camp in campaigns:
            if camp.status != "RUNNING":
                continue
            progress = camp.budget_progress
            if progress is None:
                continue
            if progress >= threshold:
                if stop:
                    if progress < 100:
                        # до 100% только уведомляем, отключаем при 100% и выше
                        await self._notify(
                            "warning",
                            f"Кампания «{camp.title}» израсходовала {progress:.0f}% дневного бюджета",
                        )
                        continue
                    await self.client.deactivate_campaign(camp.campaign_id)
                    camp.status = "INACTIVE"
                    await self.db.commit()
                    await log_action(
                        self.db, action="budget_stop", user_id=self.user_id,
                        entity_type="campaign", entity_name=camp.title,
                        details={"threshold": threshold, "progress": progress},
                        source="automation",
                    )
                    await self._notify(
                        "danger",
                        f"Кампания «{camp.title}» отключена: дневной бюджет исчерпан (100%)",
                    )
                    events.append(f"Остановлена кампания «{camp.title}» по бюджету")
                else:
                    await self._notify(
                        "warning",
                        f"Кампания «{camp.title}» достигла {progress:.0f}% дневного бюджета (порог {threshold:.0f}%)",
                    )
                    events.append(f"Уведомление по бюджету: «{camp.title}» — {progress:.0f}%")

        if events:
            await self._mark_triggered(rule)
        return "; ".join(events) if events else None

    # ------------------------------------------------------------------
    # Авто-ставки
    # ------------------------------------------------------------------

    async def _apply_bid_rule(self, rule: AutomationRule, campaign: Campaign | None) -> str | None:
        metric = rule.params.get("metric", "ctr")
        operator = rule.params.get("operator", "<")
        value = float(rule.params.get("value", 1.0))
        action = rule.params.get("action", "decrease_by_percent")
        amount = float(rule.params.get("amount", 10))
        min_bid = float(rule.params.get("min_bid", 0))
        max_bid = float(rule.params.get("max_bid", 0))

        campaigns = [campaign] if campaign else await self._all_campaigns()
        events: list[str] = []

        for camp in campaigns:
            products = (await self.db.execute(
                select(Product).where(Product.campaign_id == camp.id)
            )).scalars().all()
            changed: list[dict] = []
            for product in products:
                metric_value = self._metric_value(product, metric)
                if metric_value is None:
                    continue
                if not self._compare(metric_value, operator, value):
                    continue
                new_bid = self._new_bid(product.bid, action, amount)
                if min_bid:
                    new_bid = max(new_bid, min_bid)
                if max_bid:
                    new_bid = min(new_bid, max_bid)
                if new_bid != product.bid and new_bid >= 0:
                    changed.append({"sku": product.sku, "bid": round(new_bid, 2)})

            if changed:
                try:
                    await update_bids(self.db, camp, self.client, changed)
                except OzonAPIError:
                    logger.warning("Не удалось обновить ставки для кампании %s", camp.title)
                    continue
                await log_action(
                    self.db, action="auto_bid", user_id=self.user_id,
                    entity_type="campaign", entity_name=camp.title,
                    details={"metric": metric, "operator": operator, "value": value,
                             "action": action, "amount": amount, "count": len(changed)},
                    source="automation",
                )
                await self._notify(
                    "info",
                    f"Авто-ставки: в «{camp.title}» обновлено ставок — {len(changed)} "
                    f"({metric} {operator} {value})",
                )
                events.append(f"«{camp.title}»: изменено ставок — {len(changed)}")

        if events:
            await self._mark_triggered(rule)
        return "; ".join(events) if events else None

    # ------------------------------------------------------------------
    # Вспомогательное
    # ------------------------------------------------------------------

    def _metric_value(self, product: Product, metric: str) -> float | None:
        if metric == "ctr":
            return round(product.clicks / product.impressions * 100, 2) if product.impressions else 0.0
        if metric == "conversion":
            return round(product.orders / product.clicks * 100, 2) if product.clicks else 0.0
        if metric == "cpa":
            return round(product.spend / product.orders, 2) if product.orders else None
        if metric == "orders":
            return float(product.orders)
        if metric == "impressions":
            return float(product.impressions)
        return None

    @staticmethod
    def _compare(metric_value: float, operator: str, value: float) -> bool:
        try:
            if operator == "<":
                return metric_value < value
            if operator == ">":
                return metric_value > value
            if operator == "<=":
                return metric_value <= value
            if operator == ">=":
                return metric_value >= value
            if operator == "==":
                return metric_value == value
        except TypeError:
            return False
        return False

    def _new_bid(self, current: float, action: str, amount: float) -> float:
        if action == "decrease_by_percent":
            return current * (1 - amount / 100)
        if action == "increase_by_percent":
            return current * (1 + amount / 100)
        if action == "decrease_by_amount":
            return current - amount
        if action == "increase_by_amount":
            return current + amount
        return current

    async def _get_campaign(self, campaign_pk: int) -> Campaign | None:
        if campaign_pk not in self._campaign_cache:
            self._campaign_cache[campaign_pk] = await get_campaign_or_none(
                self.db, self.user_id, campaign_pk
            )
        return self._campaign_cache[campaign_pk]

    async def _all_campaigns(self) -> list[Campaign]:
        result = await self.db.execute(
            select(Campaign).where(Campaign.user_id == self.user_id)
        )
        return list(result.scalars().all())

    async def _mark_triggered(self, rule: AutomationRule) -> None:
        rule.last_triggered_at = datetime.utcnow()
        await self.db.commit()

    async def _notify(self, level: str, message: str) -> None:
        self.db.add(Notification(user_id=self.user_id, level=level, message=message))
        await self.db.commit()
