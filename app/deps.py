"""Общие FastAPI-зависимости."""
from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import User
from app.security import read_session_token

SESSION_COOKIE = "ozon_session"


async def get_current_user(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> User | None:
    """Возвращает пользователя по cookie сессии или None (страницы открываются гостям)."""
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    user_id = read_session_token(token)
    if user_id is None:
        return None
    return await db.get(User, user_id)


async def require_user(
    user: User | None = Depends(get_current_user),
) -> User:
    """Требует авторизацию, иначе 401."""
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Требуется вход в систему")
    return user