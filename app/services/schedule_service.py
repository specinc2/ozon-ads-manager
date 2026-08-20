"""Расписания работы кампаний: автоматический старт/остановка по дням недели и времени."""
import logging
from datetime import datetime

from zoneinfo import ZoneInfo
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Campaign, CampaignSchedule
from app.services.logger import log_action
from app.services.ozon_client import OzonAPIError, OzonClient

logger = logging.getLogger("schedule")

DOW_MAP = {1: "Пн", 2: "Вт", 3: "Ср", 4: "Чт", 5: "Пт", 6: "Сб", 7: "Вс"}


def now_in_tz(tz_name: str) -> datetime:
    """Текущее время в часовом поясе расписания."""
    try:
        return datetime.now(ZoneInfo(tz_name))
    except Exception:
        return datetime.now()


def parse_days(days_str: str) -> set[int]:
    """Парсит строку дней недели «1,3,5» в множество {1,3,5}."""
    result: set[int] = set()
    for part in days_str.split(","):
        part = part.strip()
        if part.isdigit() and 1 <= int(part) <= 7:
            result.add(int(part))
    return result


class ScheduleService:
    """Проверяет расписания и применяет нужные действия к кампаниям."""

    def __init__(self, db: AsyncSession, user_id: int, client: OzonClient):
        self.db = db
        self.user_id = user_id
        self.client = client

    async def apply_all(self) -> list[str]:
        """Проходит по всем активным расписаниям и стартует/останавливает кампании."""
        result = await self.db.execute(
            select(CampaignSchedule).where(
                CampaignSchedule.user_id == self.user_id,
                CampaignSchedule.is_active.is_(True),
            )
        )
        schedules = list(result.scalars().all())
        events: list[str] = []
        for sched in schedules:
            try:
                event = await self._apply_one(sched)
            except OzonAPIError as e:
                logger.warning("Расписание %s: ошибка API: %s", sched.id, e)
                continue
            if event:
                events.append(event)
        return events

    async def _apply_one(self, sched: CampaignSchedule) -> str | None:
        campaign = (await self.db.execute(
            select(Campaign).where(
                Campaign.user_id == self.user_id,
                Campaign.id == sched.campaign_id,
            )
        )).scalar_one_or_none()
        if campaign is None:
            return None  # кампания удалена

        now = now_in_tz(sched.timezone)
        days = parse_days(sched.days_of_week)
        # datetime.isoweekday(): 1=Пн ... 7=Вс — совпадает с нашим форматом
        active_today = now.isoweekday() in days
        active_now = False
        if active_today:
            try:
                hh, mm = map(int, sched.time_start.split(":"))
                start_min = hh * 60 + mm
                hh, mm = map(int, sched.time_end.split(":"))
                end_min = hh * 60 + mm
                current_min = now.hour * 60 + now.minute
                active_now = start_min <= current_min <= end_min
            except ValueError:
                active_now = False

        if active_now and campaign.status != "RUNNING":
            await self.client.activate_campaign(campaign.campaign_id)
            campaign.status = "RUNNING"
            await self.db.commit()
            await log_action(
                self.db, action="schedule_start", user_id=self.user_id,
                entity_type="campaign", entity_name=campaign.title,
                details={"schedule_id": sched.id}, source="automation",
            )
            return f"Кампания «{campaign.title}» запущена по расписанию"

        if not active_now and campaign.status == "RUNNING":
            await self.client.deactivate_campaign(campaign.campaign_id)
            campaign.status = "INACTIVE"
            await self.db.commit()
            await log_action(
                self.db, action="schedule_stop", user_id=self.user_id,
                entity_type="campaign", entity_name=campaign.title,
                details={"schedule_id": sched.id}, source="automation",
            )
            return f"Кампания «{campaign.title}» остановлена по расписанию"

        return None
