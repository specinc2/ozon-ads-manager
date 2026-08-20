"""Pydantic-схемы для API и страниц."""
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, Field


# --- Аутентификация ---
class UserRegister(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    email: str = Field(min_length=5, max_length=255)
    password: str = Field(min_length=6)


class UserLogin(BaseModel):
    username: str
    password: str


class UserOut(BaseModel):
    id: int
    username: str
    email: str

    model_config = {"from_attributes": True}


# --- API-ключи ---
class ApiKeyCreate(BaseModel):
    client_id: str = Field(min_length=1)
    client_secret: str = Field(min_length=1)


class ApiKeyOut(BaseModel):
    id: int
    name: str
    is_active: bool
    last_verified_at: datetime | None = None

    model_config = {"from_attributes": True}


# --- Кампании ---
class CampaignOut(BaseModel):
    id: int
    campaign_id: str
    title: str
    status: str
    campaign_type: str
    daily_budget: float | None = None
    total_budget: float | None = None
    spent: float | None = None
    budget_progress: float | None = None
    start_date: date | None = None
    end_date: date | None = None
    last_synced_at: datetime | None = None

    model_config = {"from_attributes": True}


class CampaignUpdate(BaseModel):
    daily_budget: float | None = None
    total_budget: float | None = None


# --- Статистика ---
class StatRow(BaseModel):
    date: date
    impressions: int = 0
    clicks: int = 0
    ctr: float = 0.0
    orders: int = 0
    revenue: float = 0.0
    spend: float = 0.0
    cpa: float = 0.0
    romi: float = 0.0


# --- Товары ---
class ProductBid(BaseModel):
    sku: str
    bid: float


class ProductsBulkUpdate(BaseModel):
    campaign_id: int
    products: list[ProductBid]


# --- Авто-правила ---
class RuleCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    rule_type: str = Field(pattern=r"^(budget_notify|budget_stop|auto_bid)$")
    campaign_id: int | None = None
    params: dict[str, Any] = {}
    is_active: bool = True


class RuleOut(BaseModel):
    id: int
    name: str
    rule_type: str
    campaign_id: int | None = None
    params: dict[str, Any] = {}
    is_active: bool
    last_triggered_at: datetime | None = None

    model_config = {"from_attributes": True}


# --- Расписания ---
class ScheduleCreate(BaseModel):
    campaign_id: int
    days_of_week: str = "1,2,3,4,5,6,7"
    time_start: str = "10:00"
    time_end: str = "23:00"
    timezone: str = "Europe/Moscow"


class ScheduleOut(BaseModel):
    id: int
    campaign_id: int
    days_of_week: str
    time_start: str
    time_end: str
    timezone: str
    is_active: bool

    model_config = {"from_attributes": True}