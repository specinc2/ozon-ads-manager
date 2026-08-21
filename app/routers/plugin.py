# -*- coding: utf-8 -*-
"""Публичные эндпоинты для браузерного плагина Ozon.

Плагин читает карточку товара Ozon прямо в браузере пользователя
(антибот не нужен) и отправляет сюда название, цену, страну поставки.
Идентификация пользователя — по токену плагина (генерируется в настройках).
"""
import logging
import secrets

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import PriceCollect, ProxySetting, User
from app.security import hash_password

logger = logging.getLogger("plugin")

router = APIRouter(prefix="/plugin", tags=["plugin"])

# Токен плагина хранится в ProxySetting.plugin_token (новая колонка).
# Для совместимости со старыми БД читаем/создаём колонку лениво.


class PricePayload(BaseModel):
    url: str = Field("", max_length=2000)
    sku: str = Field("", max_length=64)
    name: str = Field("", max_length=1000)
    price: float = 0
    currency: str = "RUB"
    country: str = Field("", max_length=64)
    marketplace: str = "ozon"


async def _user_by_plugin_token(db: AsyncSession, token: str) -> User | None:
    if not token:
        return None
    result = await db.execute(
        select(ProxySetting).where(ProxySetting.plugin_token == token).limit(1)
    )
    setting = result.scalar_one_or_none()
    if setting is None:
        return None
    user_result = await db.execute(
        select(User).where(User.id == setting.user_id).limit(1)
    )
    return user_result.scalar_one_or_none()


@router.post("/collect")
async def collect_price(request: Request, db: AsyncSession = Depends(get_db)):
    """Принимает данные о цене от плагина: POST /plugin/collect?token=..."""
    token = request.query_params.get("token", "") or (request.headers.get("X-Plugin-Token") or "")
    user = await _user_by_plugin_token(db, token)
    if user is None:
        return JSONResponse({"ok": False, "error": "invalid_token"}, status_code=401)

    body = await request.json()
    payload = PricePayload(**body)

    if not payload.url and not payload.sku:
        return JSONResponse({"ok": False, "error": "url or sku required"}, status_code=400)
    if not payload.price or payload.price <= 0:
        return JSONResponse({"ok": False, "error": "price required"}, status_code=400)

    record = PriceCollect(
        user_id=user.id,
        url=payload.url,
        sku=payload.sku,
        name=payload.name,
        price=payload.price,
        currency=payload.currency or "RUB",
        country=payload.country,
        marketplace=payload.marketplace or "ozon",
    )
    db.add(record)
    await db.commit()
    return JSONResponse({"ok": True, "id": record.id})


@router.post("/token")
async def create_token(request: Request, db: AsyncSession = Depends(get_db)):
    """Создаёт/возвращает токен плагина для текущего пользователя.

    Ожидает обычную сессию (куки) — вызывается из настроек.
    """
    from app.routers.pages import get_current_user

    user = await get_current_user(request, db)
    if not user:
        return JSONResponse({"ok": False, "error": "auth required"}, status_code=401)

    result = await db.execute(
        select(ProxySetting).where(ProxySetting.user_id == user.id).limit(1)
    )
    setting = result.scalar_one_or_none()
    if setting is None:
        setting = ProxySetting(user_id=user.id)
        db.add(setting)

    if not setting.plugin_token:
        setting.plugin_token = secrets.token_urlsafe(24)
        await db.commit()
    return JSONResponse({"ok": True, "token": setting.plugin_token})
