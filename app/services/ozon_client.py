"""Асинхронный клиент Ozon Performance API (версия 2.0).

Ключевые отличия от старого API (проверены на реальных ключах 2026-08):
- Авторизация: POST /api/client/token с grant_type=client_credentials
  в JSON-теле; все запросы идут с заголовком Authorization: Bearer <token>.
- Все пути начинаются с /api/client/...
- Бюджеты передаются в микрорублях (миллионная доля рубля): 1 000 000 = 1 ₽.
- Статистика: GET /api/client/statistics/daily/json — синхронный JSON-отчёт;
  POST /api/client/statistics/json — асинхронный отчёт по UUID.
- Денежные значения в отчётах — строки с запятой-разделителем ("3,31").

Ретраи с экспоненциальной задержкой при 429/5xx, ограничение параллельных
запросов, пагинация больших ответов.
"""
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any

import httpx

from app.config import settings

logger = logging.getLogger("ozon")

MAX_RETRIES = 4
BASE_DELAY = 1.0  # секунда, далее экспоненциально
REQUEST_TIMEOUT = 30.0
MICRO_RUBLES = 1_000_000  # бюджет в API задаётся в микрорублях


class OzonAPIError(Exception):
    """Ошибка API Ozon с понятным сообщением для пользователя."""

    def __init__(self, message: str, status: int | None = None, payload: Any = None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.payload = payload


class OzonAuthError(OzonAPIError):
    """Неверные ключи или истёкший токен."""


def rubles_from_micro(value: Any) -> float | None:
    """Переводит бюджет из микрорублей (строка/число) в рубли."""
    if value is None or value == "":
        return None
    try:
        return float(value) / MICRO_RUBLES
    except (TypeError, ValueError):
        return None


def parse_decimal(value: Any) -> float:
    """Парсит денежное значение из отчёта: строка '3,31' → 3.31."""
    if value is None:
        return 0.0
    s = str(value).replace(",", ".").replace(" ", "")
    try:
        return float(s)
    except ValueError:
        return 0.0


class OzonClient:
    """Клиент для одного набора ключей пользователя.

    transport: необязательный httpx.AsyncBaseTransport для тестов (mock).
    """

    def __init__(self, client_id: str, client_secret: str, transport: httpx.AsyncBaseTransport | None = None):
        self.client_id = client_id.strip()
        self.client_secret = client_secret.strip()
        self.api_key: str | None = None
        self.api_key_expires: datetime | None = None
        self._sem = asyncio.Semaphore(5)
        self._transport = transport

    # ------------------------------------------------------------------
    # Токен
    # ------------------------------------------------------------------

    def _http_client(self) -> httpx.AsyncClient:
        """Создаёт HTTP-клиент (с mock-транспортом в тестах)."""
        return httpx.AsyncClient(timeout=REQUEST_TIMEOUT, transport=self._transport)

    async def _ensure_token(self) -> str:
        """Возвращает действующий access_token, получая новый при необходимости."""
        now = datetime.utcnow()
        if self.api_key and self.api_key_expires and now < self.api_key_expires - timedelta(minutes=5):
            return self.api_key

        async with self._http_client() as client:
            resp = await client.post(
                f"{settings.ozon_base_url}/api/client/token",
                headers={"Content-Type": "application/json"},
                json={
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "grant_type": "client_credentials",
                },
            )
        if resp.status_code != 200:
            raise OzonAuthError(
                f"Не удалось получить токен (HTTP {resp.status_code}): {resp.text[:200]}",
                status=resp.status_code,
            )
        data = resp.json()
        self.api_key = data.get("access_token")
        expires_in = data.get("expires_in")
        if expires_in:
            try:
                self.api_key_expires = now + timedelta(seconds=int(expires_in))
            except (TypeError, ValueError):
                self.api_key_expires = now + timedelta(hours=1)
        else:
            self.api_key_expires = now + timedelta(hours=1)
        if not self.api_key:
            raise OzonAuthError("Ответ token-эндпоинта не содержит access_token", payload=data)
        return self.api_key

    # ------------------------------------------------------------------
    # Низкоуровневый запрос с ретраями
    # ------------------------------------------------------------------

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json_body: dict | None = None,
    ) -> Any:
        """Выполняет запрос к Ozon API с ретраями (без записи логов — для тестов)."""
        headers = {"Authorization": f"Bearer {await self._ensure_token()}"}

        last_error: OzonAPIError | None = None
        for attempt in range(MAX_RETRIES):
            async with self._sem:
                try:
                    async with self._http_client() as client:
                        resp = await client.request(
                            method, f"{settings.ozon_base_url}{path}",
                            headers=headers, params=params, json=json_body,
                        )
                except httpx.HTTPError as e:
                    last_error = OzonAPIError(f"Ошибка сети при запросе {path}: {e}")
                    await asyncio.sleep(BASE_DELAY * (2 ** attempt))
                    continue

            if resp.status_code in (429, 500, 502, 503, 504):
                retry_after = resp.headers.get("Retry-After")
                try:
                    delay = float(retry_after) if retry_after else BASE_DELAY * (2 ** attempt)
                except ValueError:
                    delay = BASE_DELAY * (2 ** attempt)
                logger.warning("Ozon %s %s -> %s, retry %s через %.1fs", method, path, resp.status_code, attempt + 1, delay)
                await asyncio.sleep(delay)
                last_error = OzonAPIError(f"Ozon вернул {resp.status_code} для {path}", status=resp.status_code)
                continue

            if resp.status_code == 401:
                # токен мог истечь — сбросим и попробуем один раз с новым
                if attempt == 0:
                    self.api_key = None
                    self.api_key_expires = None
                    headers = {"Authorization": f"Bearer {await self._ensure_token()}"}
                    continue
                raise OzonAuthError(
                    f"Ozon отклонил запрос {path} (HTTP 401). Проверьте ключи.",
                    status=resp.status_code,
                )

            if resp.status_code >= 400:
                raise OzonAPIError(
                    f"Ozon вернул ошибку {resp.status_code} для {path}: {resp.text[:300]}",
                    status=resp.status_code,
                )

            try:
                return resp.json()
            except ValueError:
                return {"raw": resp.text}

        assert last_error is not None
        raise last_error

    # ------------------------------------------------------------------
    # Кампании
    # ------------------------------------------------------------------

    async def list_campaigns(self, state: str | None = None) -> list[dict]:
        """GET /api/client/campaign — список кампаний (с пагинацией)."""
        page = 1
        page_size = 100
        all_items: list[dict] = []
        while True:
            params: dict = {"page": page, "pageSize": page_size}
            if state:
                params["state"] = state
            data = await self._request("GET", "/api/client/campaign", params=params)
            items = data.get("list") or []
            all_items.extend(items)
            if len(items) < page_size:
                break
            page += 1
        return all_items

    async def get_campaign(self, campaign_id: str | int) -> dict:
        """GET /api/client/campaign — детали одной кампании (через фильтр)."""
        data = await self._request(
            "GET", "/api/client/campaign",
            params={"campaignIds": str(campaign_id)},
        )
        items = data.get("list") or []
        return items[0] if items else {}

    async def activate_campaign(self, campaign_id: str | int) -> Any:
        """POST /api/client/campaign/{id}/activate — запуск кампании."""
        return await self._request("POST", f"/api/client/campaign/{campaign_id}/activate", json_body={})

    async def deactivate_campaign(self, campaign_id: str | int) -> Any:
        """POST /api/client/campaign/{id}/deactivate — остановка кампании."""
        return await self._request("POST", f"/api/client/campaign/{campaign_id}/deactivate", json_body={})

    async def update_campaign(
        self, campaign_id: str | int, *,
        daily_budget_rub: float | None = None,
        weekly_budget_rub: float | None = None,
        total_budget_rub: float | None = None,
    ) -> Any:
        """PATCH /api/client/campaign/{id} — изменение бюджетов (в рублях).

        Бюджеты пересчитываются в микрорубли, как требует API.
        """
        body: dict[str, Any] = {}
        if daily_budget_rub is not None:
            body["dailyBudget"] = str(int(daily_budget_rub * MICRO_RUBLES))
        if weekly_budget_rub is not None:
            body["weeklyBudget"] = str(int(weekly_budget_rub * MICRO_RUBLES))
        if total_budget_rub is not None:
            body["budget"] = str(int(total_budget_rub * MICRO_RUBLES))
        return await self._request("PATCH", f"/api/client/campaign/{campaign_id}", json_body=body)

    # ------------------------------------------------------------------
    # Товары и ставки
    # ------------------------------------------------------------------

    async def get_products(self, campaign_id: str | int) -> list[dict]:
        """GET /api/client/campaign/{id}/v2/products — товары кампании (с пагинацией)."""
        page = 1
        page_size = 100
        all_items: list[dict] = []
        while True:
            data = await self._request(
                "GET", f"/api/client/campaign/{campaign_id}/v2/products",
                params={"page": page, "pageSize": page_size},
            )
            items = data.get("products") or []
            all_items.extend(items)
            if len(items) < page_size:
                break
            page += 1
        return all_items

    async def get_competitive_bids(self, campaign_id: str | int, skus: list[str]) -> list[dict]:
        """GET /api/client/campaign/{id}/products/bids/competitive — конкурентные ставки по товарам.

        Позволяет оценить, какие ставки нужно установить для попадания
        в топ-3, топ-10 и т.п.
        skus: список SKU товаров (до 200 за раз).
        """
        params = [("skus", s) for s in skus]
        data = await self._request(
            "GET", f"/api/client/campaign/{campaign_id}/products/bids/competitive",
            params=params,
        )
        return data.get("bids") or []

    async def update_products_bids(self, campaign_id: str | int, items: list[dict]) -> Any:
        """PUT /api/client/campaign/{id}/products — массовое изменение ставок.

        items: [{"sku": 123, "bid": 45.0}, ...]
        """
        body = {"bids": [{"sku": str(i["sku"]), "bid": str(i["bid"])} for i in items]}
        return await self._request("PUT", f"/api/client/campaign/{campaign_id}/products", json_body=body)

    # ------------------------------------------------------------------
    # Статистика
    # ------------------------------------------------------------------

    async def get_daily_statistics(
        self, campaign_ids: list[str], date_from: str, date_to: str,
    ) -> list[dict]:
        """GET /api/client/statistics/daily/json — синхронный дневной отчёт.

        Удобен для кэширования: возвращает {rows: [...]} сразу, без UUID.
        campaignIds передаются повторяющимися параметрами (?campaignIds=1&campaignIds=2).
        """
        params: list[tuple[str, str]] = [
            ("dateFrom", date_from),
            ("dateTo", date_to),
        ]
        for cid in campaign_ids:
            params.append(("campaignIds", cid))
        data = await self._request("GET", "/api/client/statistics/daily/json", params=params)
        return data.get("rows") or []

    async def get_campaign_summary_report(
        self, date_from: str, date_to: str, campaign_ids: list[str] | None = None,
    ) -> list[dict]:
        """GET /api/client/statistics/campaign/product/json — сводный отчёт по кампаниям.

        Содержит: показы, клики, CTR, средняя ставка, расход, заказы, выручка,
        ДРР (доля рекламных расходов), добавления в корзину (toCart).
        Работает для кампаний «Оплата за клик» (SKU).
        """
        params: list[tuple[str, str]] = [
            ("dateFrom", date_from),
            ("dateTo", date_to),
        ]
        for cid in (campaign_ids or []):
            params.append(("campaignIds", cid))
        data = await self._request("GET", "/api/client/statistics/campaign/product/json", params=params)
        return data.get("rows") or []

    async def submit_statistics_report(
        self, campaign_ids: list[str], date_from: str, date_to: str,
        group_by: str = "DATE",
    ) -> str:
        """POST /api/client/statistics/json — асинхронная генерация отчёта.

        Возвращает UUID запроса; отчёт скачивается по нему после готовности.
        """
        body = {
            "campaigns": campaign_ids,
            "dateFrom": date_from,
            "dateTo": date_to,
            "groupBy": group_by,
        }
        data = await self._request("POST", "/api/client/statistics/json", json_body=body)
        uuid = data.get("UUID") or data.get("uuid")
        if not uuid:
            raise OzonAPIError("Ответ статистики не содержит UUID", payload=data)
        return uuid

    async def get_report_status(self, uuid: str) -> dict:
        """GET /api/client/statistics/{UUID} — статус формирования отчёта."""
        return await self._request("GET", f"/api/client/statistics/{uuid}")

    async def download_report(self, uuid: str) -> str:
        """GET /api/client/statistics/report?UUID=... — скачивание отчёта (CSV/JSON)."""
        data = await self._request(
            "GET", "/api/client/statistics/report", params={"UUID": uuid},
        )
        return data.get("raw") or str(data)

    async def wait_for_report(self, uuid: str, timeout: int = 120) -> dict:
        """Ждёт готовности отчёта, опрашивая статус."""
        elapsed = 0
        while elapsed < timeout:
            status = await self.get_report_status(uuid)
            state = status.get("state", "")
            if state == "OK":
                return status
            if state == "ERROR":
                raise OzonAPIError(f"Ошибка формирования отчёта: {status.get('error', '')}", payload=status)
            await asyncio.sleep(3)
            elapsed += 3
        raise OzonAPIError("Таймаут ожидания отчёта", payload=status)


# ---------------------------------------------------------------------------
# Нормализация ответов Ozon
# ---------------------------------------------------------------------------

CAMPAIGN_TYPE_MAP = {
    "SKU": "Оплата за клик",
    "BANNER": "Баннерная",
    "SEARCH_PROMO": "Оплата за заказ",
    "REF_BLOGGER": "Блогеры",
    "REF_VK": "ВКонтакте",
    "VIDEO": "Видеобаннер",
    "MEDIA": "Медийная",
}

STATE_MAP = {
    "CAMPAIGN_STATE_RUNNING": "RUNNING",
    "CAMPAIGN_STATE_PLANNED": "PLANNED",
    "CAMPAIGN_STATE_STOPPED": "STOPPED",
    "CAMPAIGN_STATE_INACTIVE": "INACTIVE",
    "CAMPAIGN_STATE_ARCHIVED": "ARCHIVED",
    "CAMPAIGN_STATE_FINISHED": "FINISHED",
    "CAMPAIGN_STATE_MODERATION_DRAFT": "DRAFT",
    "CAMPAIGN_STATE_MODERATION_IN_PROGRESS": "MODERATION",
    "CAMPAIGN_STATE_MODERATION_FAILED": "MODERATION_FAILED",
}


def normalize_campaign(raw: dict) -> dict:
    """Приводит ответ GET /api/client/campaign к единой структуре для кэша в БД.

    Бюджеты из микрорублей переводятся в рубли.
    """
    cid = raw.get("id") or raw.get("campaign_id") or raw.get("campaignId")
    ctype = raw.get("advObjectType") or raw.get("type") or "UNKNOWN"
    return {
        "campaign_id": str(cid),
        "title": raw.get("title") or f"Кампания {cid}",
        "status": STATE_MAP.get(raw.get("state", ""), raw.get("state") or "UNKNOWN"),
        "campaign_type": CAMPAIGN_TYPE_MAP.get(str(ctype), str(ctype)),
        "daily_budget": rubles_from_micro(raw.get("dailyBudget")) if raw.get("dailyBudget") not in (None, "", "0") else None,
        "weekly_budget": rubles_from_micro(raw.get("weeklyBudget")) if raw.get("weeklyBudget") not in (None, "", "0") else None,
        "total_budget": rubles_from_micro(raw.get("budget")) if raw.get("budget") not in (None, "", "0") else None,
        "spent": None,  # расход берём из статистики
        "start_date": _parse_date(raw.get("fromDate")),
        "end_date": _parse_date(raw.get("toDate")),
    }


def normalize_daily_stat_row(raw: dict) -> dict:
    """Приводит строку GET /statistics/daily/json к единой структуре.

    Поля отчёта: id, title, date, views, clicks, moneySpent, orders, ordersMoney.
    """
    impressions = _int(raw.get("views"))
    clicks = _int(raw.get("clicks"))
    orders = _int(raw.get("orders"))
    spend = parse_decimal(raw.get("moneySpent"))
    revenue = parse_decimal(raw.get("ordersMoney"))
    return {
        "campaign_id": str(raw.get("id") or ""),
        "date": _parse_date(raw.get("date")),
        "impressions": impressions,
        "clicks": clicks,
        "ctr": round(clicks / impressions * 100, 2) if impressions else 0.0,
        "orders": orders,
        "revenue": revenue,
        "spend": spend,
        "cpa": round(spend / orders, 2) if orders else 0.0,
        "romi": round(revenue / spend, 2) if spend else 0.0,
    }


def normalize_stat_row(raw: dict) -> dict:
    """Приводит строку асинхронного отчёта POST /statistics/json.

    Формат отчёта может отличаться от daily — поля ищем по нескольким именам.
    """
    m = raw.get("metrics") or raw
    impressions = _int(m.get("impressions") or m.get("views"))
    clicks = _int(m.get("clicks"))
    orders = _int(m.get("orders"))
    spend = parse_decimal(m.get("spend") or m.get("moneySpent"))
    revenue = parse_decimal(m.get("orders_sum") or m.get("ordersMoney"))
    return {
        "date": raw.get("day") or raw.get("date"),
        "impressions": impressions,
        "clicks": clicks,
        "ctr": round(clicks / impressions * 100, 2) if impressions else 0.0,
        "orders": orders,
        "revenue": revenue,
        "spend": spend,
        "cpa": round(spend / orders, 2) if orders else 0.0,
        "romi": round(revenue / spend, 2) if spend else 0.0,
    }


def _int(value: Any) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _parse_date(value: Any):
    """Парсит дату из '2026-07-16' или '2026-07-16T07:54:10Z'."""
    if not value:
        return None
    from datetime import date
    s = str(value)[:10]
    try:
        return date.fromisoformat(s)
    except ValueError:
        return None
