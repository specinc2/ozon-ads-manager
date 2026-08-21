"""ORM-модели базы данных.

Таблицы:
- users           — пользователи (многопользовательский режим)
- api_keys        — ключи Ozon Performance API (шифруются)
- campaigns       — кэш кампаний из Ozon
- campaign_stats  — кэш статистики кампаний по дням
- products        — товары в кампании и их ставки
- automation_rules    — авто-правила (бюджет, ставки)
- campaign_schedules  — расписания работы кампаний
- action_log      — журнал действий пользователей и системы
- api_log         — журнал запросов к Ozon API (для отладки)
- notifications   — уведомления в интерфейсе
"""
from datetime import date, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    api_keys = relationship("ApiKey", back_populates="user", cascade="all, delete-orphan")
    campaigns = relationship("Campaign", back_populates="user", cascade="all, delete-orphan")


class ApiKey(Base):
    """Ключи Ozon Performance API (хранятся в зашифрованном виде)."""

    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(128), default="Основные ключи")

    # Зашифрованные значения
    client_id_enc: Mapped[str] = mapped_column(Text)
    client_secret_enc: Mapped[str] = mapped_column(Text)
    api_key_enc: Mapped[str] = mapped_column(Text, default="")
    api_key_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Ключи Seller API (для данных из ЛК: остатки, цены, комиссии)
    seller_client_id_enc: Mapped[str] = mapped_column(Text, default="")
    seller_api_key_enc: Mapped[str] = mapped_column(Text, default="")

    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    user = relationship("User", back_populates="api_keys")


class Campaign(Base):
    """Кэш кампаний, синхронизируется с Ozon Performance API."""

    __tablename__ = "campaigns"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    campaign_id: Mapped[str] = mapped_column(String(64), index=True)  # ID из Ozon

    title: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(32), default="UNKNOWN", index=True)  # RUNNING/STOPPED/...
    campaign_type: Mapped[str] = mapped_column(String(32), default="UNKNOWN")  # SEARCH/AUTOMATIC/...
    daily_budget: Mapped[float | None] = mapped_column(Float, nullable=True)
    weekly_budget: Mapped[float | None] = mapped_column(Float, nullable=True)
    total_budget: Mapped[float | None] = mapped_column(Float, nullable=True)
    spent: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Сводные метрики за период (из отчёта campaign/product): ДРР %, корзина, средняя цена клика
    drr: Mapped[float] = mapped_column(Float, default=0.0)
    to_cart: Mapped[int] = mapped_column(Integer, default=0)
    avg_click_price: Mapped[float] = mapped_column(Float, default=0.0)
    start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    user = relationship("User", back_populates="campaigns")
    stats = relationship("CampaignStat", back_populates="campaign", cascade="all, delete-orphan")
    products = relationship("Product", back_populates="campaign", cascade="all, delete-orphan")

    @property
    def budget_progress(self) -> float | None:
        """Процент израсходованного бюджета (дневного или недельного) 0..100+."""
        budget = self.daily_budget or self.weekly_budget
        if budget and budget > 0 and self.spent is not None:
            return round(self.spent / budget * 100, 2)
        return None


class CampaignStat(Base):
    """Кэш статистики кампании за один день."""

    __tablename__ = "campaign_stats"
    __table_args__ = (
        # одна запись на кампанию/день
        UniqueConstraint("campaign_id", "stat_date", name="uq_campaign_stat_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id"), index=True)
    stat_date: Mapped[date] = mapped_column(Date, index=True)

    impressions: Mapped[int] = mapped_column(Integer, default=0)
    clicks: Mapped[int] = mapped_column(Integer, default=0)
    ctr: Mapped[float] = mapped_column(Float, default=0.0)
    orders: Mapped[int] = mapped_column(Integer, default=0)
    revenue: Mapped[float] = mapped_column(Float, default=0.0)
    spend: Mapped[float] = mapped_column(Float, default=0.0)
    cpa: Mapped[float] = mapped_column(Float, default=0.0)
    romi: Mapped[float] = mapped_column(Float, default=0.0)

    campaign = relationship("Campaign", back_populates="stats")


class Product(Base):
    """Товар внутри кампании и его ставка."""

    __tablename__ = "products"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id"), index=True)
    sku: Mapped[str] = mapped_column(String(64), index=True)
    name: Mapped[str] = mapped_column(String(255), default="")
    bid: Mapped[float] = mapped_column(Float, default=0.0)

    impressions: Mapped[int] = mapped_column(Integer, default=0)
    clicks: Mapped[int] = mapped_column(Integer, default=0)
    orders: Mapped[int] = mapped_column(Integer, default=0)
    spend: Mapped[float] = mapped_column(Float, default=0.0)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    campaign = relationship("Campaign", back_populates="products")


class ProductInfo(Base):
    """Справочная информация о товаре (по SKU, для расчёта маржи и бидера).

    Поля себестоимости, цены, остатков и % выкупа пользователь указывает
    вручную (или подтягивает из ЛК маркетплейса в будущем).
    """

    __tablename__ = "product_info"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    sku: Mapped[str] = mapped_column(String(64), index=True)
    name: Mapped[str] = mapped_column(String(255), default="")

    # Экономика товара
    price: Mapped[float] = mapped_column(Float, default=0.0)           # цена продажи, ₽
    cost_price: Mapped[float] = mapped_column(Float, default=0.0)      # себестоимость, ₽
    leftovers: Mapped[int] = mapped_column(Integer, default=0)         # остатки, шт
    fulfillment_type: Mapped[str] = mapped_column(String(8), default="FBO")  # FBO / FBS
    commission_pct: Mapped[float] = mapped_column(Float, default=0.0)  # комиссия маркетплейса, %
    logistics_cost: Mapped[float] = mapped_column(Float, default=0.0)  # логистика, ₽/шт
    acquiring_pct: Mapped[float] = mapped_column(Float, default=0.0)   # эквайринг, %
    buyout_pct: Mapped[float] = mapped_column(Float, default=100.0)    # % выкупа за месяц
    in_promotion: Mapped[bool] = mapped_column(Boolean, default=False) # участие в акции
    promotion_discount_pct: Mapped[float] = mapped_column(Float, default=0.0)  # скидка акции, %
    monthly_orders: Mapped[int] = mapped_column(Integer, default=0)    # заказов за месяц (всего)
    monthly_revenue: Mapped[float] = mapped_column(Float, default=0.0) # выручка за месяц, ₽ (весь оборот)
    min_margin_pct: Mapped[float] = mapped_column(Float, default=10.0) # минимальный порог маржи, %

    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ProxySetting(Base):
    """Прокси для анализатора цен (обход антибота Ozon/AliExpress).

    Хранит URL прокси, дату окончания и куки Ozon из браузера пользователя.
    Куки позволяют пройти JS-challenge Ozon: браузер пользователя его решает,
    а куки применяются на сервере с прокси.
    """

    __tablename__ = "proxy_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    proxy_url: Mapped[str] = mapped_column(Text, default="")   # http://user:pass@ip:port
    ozon_cookies: Mapped[str] = mapped_column(Text, default="")  # JSON-строка с куками Ozon
    expires_at: Mapped[date | None] = mapped_column(Date, nullable=True)
    # Bright Data (официальный API цен Ozon по URL товара)
    bd_api_key: Mapped[str] = mapped_column(Text, default="")      # Bearer-токен API
    bd_dataset_id: Mapped[str] = mapped_column(Text, default="")   # dataset_id (gd_...)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    @property
    def days_left(self) -> int | None:
        """Сколько дней осталось до окончания прокси."""
        if self.expires_at is None:
            return None
        return (self.expires_at - date.today()).days


class BidderRule(Base):
    """Правило бидера — автоматическое управление ставками товара.

    strategy:
      target_drr       — целевая ДРР от всего оборота: подбираем ставку так,
                         чтобы рекламный расход не превышал N% оборота артикула.
      maintain_position— поддержание позиции в выдаче (топ-3/топ-10) при
                         минимальном расходе: поднимаем ставку, пока товар
                         не попадает в целевую выдачу.
      ai_test          — итеративный подбор ставки: шагами повышаем ставку,
                         анализируем CTR/позицию/ДРР, находим оптимум.
    """
    __tablename__ = "bidder_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(128))
    strategy: Mapped[str] = mapped_column(String(32), index=True)
    sku: Mapped[str] = mapped_column(String(64), default="", index=True)  # "" = все товары
    campaign_id: Mapped[int | None] = mapped_column(ForeignKey("campaigns.id"), nullable=True, index=True)
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_triggered_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class AutomationRule(Base):
    """Авто-правило: контроль бюджета или изменение ставок.

    rule_type: budget_notify | budget_stop | auto_bid
    params (JSON):
      budget_*: {"threshold": 90}  — процент от дневного бюджета
      auto_bid: {"metric": "ctr", "operator": "<", "value": 1.0,
                 "action": "decrease_by_percent"|"increase_by_percent"|"decrease_by_amount"|"increase_by_amount",
                 "amount": 10, "min_bid": 5, "max_bid": 500}
    """
    __tablename__ = "automation_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(128))
    rule_type: Mapped[str] = mapped_column(String(32), index=True)
    campaign_id: Mapped[int | None] = mapped_column(ForeignKey("campaigns.id"), nullable=True, index=True)
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_triggered_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class CampaignSchedule(Base):
    """Расписание работы кампании: в какие дни недели и часы она активна."""

    __tablename__ = "campaign_schedules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id"), index=True)
    days_of_week: Mapped[str] = mapped_column(String(32), default="1,2,3,4,5,6,7")  # 1=Пн ... 7=Вс
    time_start: Mapped[str] = mapped_column(String(5), default="10:00")
    time_end: Mapped[str] = mapped_column(String(5), default="23:00")
    timezone: Mapped[str] = mapped_column(String(64), default="Europe/Moscow")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ActionLog(Base):
    """Журнал действий пользователей и авто-правил."""

    __tablename__ = "action_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True, nullable=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    action: Mapped[str] = mapped_column(String(64))  # campaign_start, campaign_stop, budget_change, rule_triggered...
    entity_type: Mapped[str] = mapped_column(String(32), default="")  # campaign / rule / product
    entity_name: Mapped[str] = mapped_column(String(255), default="")
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    source: Mapped[str] = mapped_column(String(16), default="manual")  # manual / automation / system


class ApiLog(Base):
    """Журнал запросов к Ozon API (без секретов)."""

    __tablename__ = "api_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True, nullable=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    endpoint: Mapped[str] = mapped_column(String(255))
    method: Mapped[str] = mapped_column(String(8))
    status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str] = mapped_column(Text, default="")


class Notification(Base):
    """Уведомление в интерфейсе."""

    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    level: Mapped[str] = mapped_column(String(16), default="info")  # info / warning / danger
    message: Mapped[str] = mapped_column(Text)
    is_read: Mapped[bool] = mapped_column(Boolean, default=False)
