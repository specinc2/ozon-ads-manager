"""Интеграционные тесты движка авто-правил с реальной БД (SQLite in-memory)."""
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models import AutomationRule, Campaign, Notification, Product, User
from app.services.rules_engine import RuleEngine


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        yield session
    await engine.dispose()


@pytest_asyncio.fixture
async def user(db):
    user = User(username="tester", email="t@t.ru", password_hash="hash")
    db.add(user)
    await db.commit()
    return user


class FakeClient:
    """Заглушка OzonClient, записывающая вызовы."""

    def __init__(self):
        self.calls = []
        self.bid_updates = []

    async def deactivate_campaign(self, campaign_id):
        self.calls.append(("stop", campaign_id))

    async def activate_campaign(self, campaign_id):
        self.calls.append(("start", campaign_id))

    async def update_products_bids(self, campaign_id, items):
        self.bid_updates.append((campaign_id, items))


async def test_budget_stop_triggers(db, user):
    """Кампания с 100% расходом бюджета останавливается правилом budget_stop."""
    campaign = Campaign(
        user_id=user.id, campaign_id="111", title="Кампания А",
        status="RUNNING", campaign_type="SEARCH",
        daily_budget=1000.0, spent=1000.0,  # 100% расход
    )
    rule = AutomationRule(
        user_id=user.id, name="Стоп", rule_type="budget_stop",
        params={"threshold": 100}, is_active=True,
    )
    db.add_all([campaign, rule])
    await db.commit()

    client = FakeClient()
    engine = RuleEngine(db, user.id, client)
    events = await engine.run_all()

    assert any("Остановлена" in e for e in events)
    assert ("stop", "111") in client.calls
    # Статус обновился в БД
    await db.refresh(campaign)
    assert campaign.status == "INACTIVE"
    # Создано уведомление
    notif = (await db.execute(
        __import__("sqlalchemy").select(Notification)
    )).scalars().first()
    assert notif is not None and "бюджет" in notif.message.lower()


async def test_budget_notify_no_stop(db, user):
    """Правило budget_notify не останавливает кампанию, только уведомляет."""
    campaign = Campaign(
        user_id=user.id, campaign_id="222", title="Кампания Б",
        status="RUNNING", daily_budget=1000.0, spent=900.0,  # 90% — порог 80%
    )
    rule = AutomationRule(
        user_id=user.id, name="Уведомление", rule_type="budget_notify",
        params={"threshold": 80}, is_active=True,
    )
    db.add_all([campaign, rule])
    await db.commit()

    client = FakeClient()
    engine = RuleEngine(db, user.id, client)
    events = await engine.run_all()

    assert any("Уведомление по бюджету" in e for e in events)
    assert client.calls == []  # стопов не было
    await db.refresh(campaign)
    assert campaign.status == "RUNNING"


async def test_auto_bid_changes_bids(db, user):
    """Авто-ставки: товар с CTR ниже порога получает сниженную ставку."""
    campaign = Campaign(
        user_id=user.id, campaign_id="333", title="Кампания В",
        status="RUNNING", daily_budget=1000.0,
    )
    # CTR = 2/1000 = 0.2% — ниже порога 1%
    product = Product(campaign_id=0, sku="100", name="Товар", bid=100.0,
                      impressions=1000, clicks=2, orders=0)
    db.add(campaign)
    await db.commit()
    await db.refresh(campaign)
    product.campaign_id = campaign.id
    db.add(product)
    rule = AutomationRule(
        user_id=user.id, name="Авто-ставки", rule_type="auto_bid",
        params={"metric": "ctr", "operator": "<", "value": 1.0,
                "action": "decrease_by_percent", "amount": 10,
                "min_bid": 5, "max_bid": 500},
        is_active=True,
    )
    db.add(rule)
    await db.commit()

    client = FakeClient()
    engine = RuleEngine(db, user.id, client)
    events = await engine.run_all()

    assert any("изменено ставок — 1" in e for e in events)
    # Новая ставка 90 (100 - 10%)
    await db.refresh(product)
    assert product.bid == 90.0


async def test_inactive_rules_skipped(db, user):
    """Неактивные правила не применяются."""
    campaign = Campaign(
        user_id=user.id, campaign_id="444", title="Кампания Г",
        status="RUNNING", daily_budget=100.0, spent=100.0,
    )
    rule = AutomationRule(
        user_id=user.id, name="Выключено", rule_type="budget_stop",
        params={"threshold": 100}, is_active=False,
    )
    db.add_all([campaign, rule])
    await db.commit()

    client = FakeClient()
    engine = RuleEngine(db, user.id, client)
    events = await engine.run_all()

    assert events == []
    assert client.calls == []
