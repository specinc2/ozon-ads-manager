"""Периодические задачи (APScheduler).

Задачи выполняются раз в SCHEDULER_INTERVAL_MINUTES минут (по ТЗ — не реже 5):
- синхронизация списка кампаний из Ozon в кэш
- сбор статистики за последние 30 дней
- применение авто-правил (бюджет, ставки)
- применение расписаний (старт/стоп по времени)

Все задачи работают с собственными сессиями БД, поэтому безопасны
для запуска вне контекста запроса.
"""
import asyncio
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import select

from app.config import settings
from app.database import async_session
from app.models import ApiKey, User
from app.security import decrypt_value
from app.services.campaigns import sync_campaigns
from app.services.ozon_client import OzonAPIError, OzonClient
from app.services.rules_engine import RuleEngine
from app.services.schedule_service import ScheduleService
from app.services.statistics import collect_statistics

logger = logging.getLogger("jobs")

_scheduler: AsyncIOScheduler | None = None


def start_scheduler() -> None:
    global _scheduler
    if _scheduler and _scheduler.running:
        return
    _scheduler = AsyncIOScheduler()
    interval = max(settings.scheduler_interval_minutes, 8)  # лимиты API: не чаще 8 минут
    _scheduler.add_job(
        run_periodic_jobs,
        IntervalTrigger(minutes=interval),
        id="periodic_jobs",
        max_instances=1,
        coalesce=True,
    )
    _scheduler.start()
    logger.info("Планировщик запущен, интервал %s мин", interval)


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler:
        _scheduler.shutdown(wait=False)
        _scheduler = None


async def run_periodic_jobs() -> None:
    """Запускается планировщиком: обрабатывает всех пользователей."""
    async with async_session() as db:
        result = await db.execute(select(ApiKey).where(ApiKey.is_active.is_(True)))
        keys = list(result.scalars().all())
        users: dict[int, ApiKey] = {}
        for key in keys:
            users.setdefault(key.user_id, key)

    for user_id, key in users.items():
        try:
            client = OzonClient(
                client_id=decrypt_value(key.client_id_enc),
                client_secret=decrypt_value(key.client_secret_enc),
            )
            await run_for_user(user_id, client)
        except OzonAPIError as e:
            logger.warning("Периодическая задача для пользователя %s: %s", user_id, e.message)
        except Exception as e:  # никакие ошибки не должны ронять планировщик
            logger.exception("Периодическая задача для пользователя %s упала: %s", user_id, e)


async def run_for_user(user_id: int, client: OzonClient) -> None:
    """Полный цикл задач для одного пользователя."""
    async with async_session() as db:
        try:
            await sync_campaigns(db, user_id, client)
            await collect_statistics(db, user_id, client, days=30)
        except OzonAPIError as e:
            logger.warning("Синхронизация пользователя %s: %s", user_id, e.message)

        # Экономика товаров из ЛК (Seller API), если ключи подключены
        try:
            from app.services.seller_sync import get_active_seller_client, sync_seller_data
            seller_client = await get_active_seller_client(db, user_id)
            if seller_client is not None:
                result = await sync_seller_data(db, user_id, seller_client)
                if result.get("updated"):
                    logger.info("Seller-синхронизация [user %s]: %s", user_id, result)
        except Exception as e:
            logger.exception("Ошибка Seller-синхронизации [user %s]: %s", user_id, e)

        # Авто-правила
        try:
            engine = RuleEngine(db, user_id, client)
            events = await engine.run_all()
            if events:
                logger.info("Авто-правила [user %s]: %s", user_id, "; ".join(events))
        except Exception as e:
            logger.exception("Ошибка движка правил [user %s]: %s", user_id, e)

        # Бидер (управление ставками)
        try:
            from app.services.bidder import BidderEngine
            bidder = BidderEngine(db, user_id, client)
            events = await bidder.run_all()
            if events:
                logger.info("Бидер [user %s]: %s", user_id, "; ".join(events))
        except Exception as e:
            logger.exception("Ошибка бидера [user %s]: %s", user_id, e)

        # Расписания
        try:
            sched_service = ScheduleService(db, user_id, client)
            events = await sched_service.apply_all()
            if events:
                logger.info("Расписания [user %s]: %s", user_id, "; ".join(events))
        except Exception as e:
            logger.exception("Ошибка расписаний [user %s]: %s", user_id, e)
