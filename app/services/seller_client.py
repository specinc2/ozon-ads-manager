"""Асинхронный клиент Ozon Seller API (для данных из личного кабинета).

- Авторизация: заголовки Client-Id + Api-Key (не Bearer-токен, как в Performance API).
- Хост: https://api-seller.ozon.ru
- Ключевые эндпоинты (проверены 2026-08):
  POST /v3/product/list             — список товаров (пагинация last_id)
  POST /v4/product/info/stocks      — остатки по товарам (FBO/FBS)
  POST /v5/product/info/prices      — цены, комиссии, логистика, эквайринг, акции

Из v5/product/info/prices берём:
  price.price / price.old_price     — цены
  commissions.sales_percent_fbo/fbs — комиссия маркетплейса, %
  commissions.fbo_deliv_to_customer_amount, fbo_direct_flow_trans_min/max_amount —
    логистика FBO, ₽
  acquiring                          — эквайринг, ₽ (по факту)
  marketing_actions                  — участие в акциях со скидками
"""
import logging

import httpx

from app.config import settings

logger = logging.getLogger("ozon_seller")

MAX_RETRIES = 3
BASE_DELAY = 1.0
REQUEST_TIMEOUT = 30.0


class SellerAPIError(Exception):
    """Ошибка Seller API с понятным сообщением."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.message = message
        self.status = status


class SellerClient:
    """Клиент Seller API для одного набора ключей."""

    def __init__(self, client_id: str, api_key: str, transport: httpx.AsyncBaseTransport | None = None):
        self.client_id = client_id.strip()
        self.api_key = api_key.strip()
        self._transport = transport

    def _headers(self) -> dict:
        return {
            "Client-Id": self.client_id,
            "Api-Key": self.api_key,
            "Content-Type": "application/json",
        }

    async def _request(self, method: str, path: str, json_body: dict | None = None) -> dict:
        last_error: SellerAPIError | None = None
        for attempt in range(MAX_RETRIES):
            async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT, transport=self._transport) as client:
                resp = await client.request(
                    method, f"https://api-seller.ozon.ru{path}",
                    headers=self._headers(), json=json_body,
                )
            if resp.status_code in (429, 500, 502, 503, 504):
                last_error = SellerAPIError(f"Seller API вернул {resp.status_code} для {path}", resp.status_code)
                logger.warning("Seller %s %s -> %s, retry %s", method, path, resp.status_code, attempt + 1)
                continue
            if resp.status_code >= 400:
                raise SellerAPIError(
                    f"Seller API: ошибка {resp.status_code} для {path}: {resp.text[:200]}",
                    resp.status_code,
                )
            try:
                return resp.json()
            except ValueError:
                return {}

        assert last_error is not None
        raise last_error

    # ------------------------------------------------------------------
    # Список товаров
    # ------------------------------------------------------------------

    async def list_products(self, limit: int = 1000) -> list[dict]:
        """POST /v3/product/list — все товары с пагинацией по last_id."""
        items: list[dict] = []
        last_id = ""
        while True:
            body = {"filter": {}, "limit": limit, "last_id": last_id}
            data = await self._request("POST", "/v3/product/list", body)
            result = data.get("result") or {}
            batch = result.get("items") or []
            items.extend(batch)
            last_id = result.get("last_id", "")
            if not last_id or len(batch) < limit:
                break
        return items

    # ------------------------------------------------------------------
    # Остатки
    # ------------------------------------------------------------------

    async def get_stocks(self, offer_ids: list[str]) -> dict[str, dict]:
        """POST /v4/product/info/stocks — остатки по товарам.

        Возвращает {offer_id: {fbo_present, fbs_present, sku}}.
        """
        # API принимает до 100 offer_id за запрос
        result: dict[str, dict] = {}
        BATCH = 100
        for i in range(0, len(offer_ids), BATCH):
            batch = offer_ids[i:i + BATCH]
            body = {"filter": {"offer_id": batch}, "limit": 100}
            data = await self._request("POST", "/v4/product/info/stocks", body)
            for item in data.get("items") or []:
                offer_id = item.get("offer_id", "")
                stocks = {s.get("type"): s for s in item.get("stocks") or []}
                fbo = stocks.get("fbo") or {}
                fbs = stocks.get("fbs") or {}
                result[offer_id] = {
                    "product_id": item.get("product_id"),
                    "sku": fbo.get("sku") or fbs.get("sku"),
                    "fbo_present": int(fbo.get("present") or 0),
                    "fbo_reserved": int(fbo.get("reserved") or 0),
                    "fbs_present": int(fbs.get("present") or 0),
                    "fbs_reserved": int(fbs.get("reserved") or 0),
                }
        return result

    # ------------------------------------------------------------------
    # Цены, комиссии, акции
    # ------------------------------------------------------------------

    async def get_prices(self, offer_ids: list[str]) -> dict[str, dict]:
        """POST /v5/product/info/prices — цены, комиссии, логистика, акции.

        Возвращает {offer_id: {price, old_price, commission_pct, logistics_cost,
        acquiring_rub, in_promotion, promotion_discount_pct, volume_weight}}.
        """
        result: dict[str, dict] = {}
        BATCH = 100
        for i in range(0, len(offer_ids), BATCH):
            batch = offer_ids[i:i + BATCH]
            body = {"filter": {"offer_id": batch}, "limit": 100}
            data = await self._request("POST", "/v5/product/info/prices", body)
            for item in data.get("items") or []:
                offer_id = item.get("offer_id", "")
                price_obj = item.get("price") or {}
                commissions = item.get("commissions") or {}

                # Комиссия: FBO или FBS (по наличию; по умолчанию FBO)
                commission_pct = float(commissions.get("sales_percent_fbo")
                                       or commissions.get("sales_percent_fbs") or 0)

                # Логистика FBO: доставка клиенту + прямая перевозка (берём среднее)
                deliv = float(commissions.get("fbo_deliv_to_customer_amount") or 0)
                trans_min = float(commissions.get("fbo_direct_flow_trans_min_amount") or 0)
                trans_max = float(commissions.get("fbo_direct_flow_trans_max_amount") or 0)
                logistics = deliv + ((trans_min + trans_max) / 2 if trans_max else trans_min)

                # Акции: наибольшая скидка среди активных
                promo_discount = 0.0
                in_promotion = False
                actions = (item.get("marketing_actions") or {}).get("actions") or []
                for action in actions:
                    discount = float(action.get("value") or 0)
                    if discount > promo_discount:
                        promo_discount = discount
                        in_promotion = True

                result[offer_id] = {
                    "product_id": item.get("product_id"),
                    "price": float(price_obj.get("price") or 0),
                    "old_price": float(price_obj.get("old_price") or 0),
                    "commission_pct": commission_pct,
                    "logistics_cost": round(logistics, 2),
                    "acquiring_rub": float(item.get("acquiring") or 0),
                    "in_promotion": in_promotion,
                    "promotion_discount_pct": round(promo_discount, 1),
                    "volume_weight": float(item.get("volume_weight") or 0),
                }
        return result

    # ------------------------------------------------------------------
    # Аналитика: выкупы, продажи, заказы за месяц
    # ------------------------------------------------------------------

    async def get_analytics(
        self, date_from: str, date_to: str, metrics: list[str], offer_ids: list[str] | None = None,
    ) -> dict[str, dict]:
        """POST /v1/analytics/data — агрегированная аналитика по товарам.

        Метрики приходят значениями в порядке запроса (без имён), поэтому
        маппим по индексу. Возвращает {sku: {metric: value}}.
        """
        body = {
            "date_from": date_from,
            "date_to": date_to,
            "metrics": metrics,
            "dimension": ["sku"],
            "filters": [],
            "sort": [{"key": "sku", "order": "ASC"}],
            "limit": 1000,
            "offset": 0,
        }
        if offer_ids:
            body["filters"].append({"key": "sku", "op": "IN", "values": offer_ids})
        data = await self._request("POST", "/v1/analytics/data", body)
        result: dict[str, dict] = {}
        rows = (data.get("result") or {}).get("data") or []
        for row in rows:
            dims = row.get("dimensions") or []
            if not dims:
                continue
            dim0 = dims[0]
            sku = str(dim0.get("id") or dim0) if isinstance(dim0, dict) else str(dim0)
            values = row.get("metrics") or []
            result[sku] = {metrics[i]: values[i] for i in range(min(len(metrics), len(values)))}
        return result


# ---------------------------------------------------------------------------
# Полезные константы метрик аналитики
# ---------------------------------------------------------------------------

ANALYTICS_METRICS = {
    "ordered_units": "Заказы, шт",
    "delivered_units": "Выкупы, шт",
    "returns": "Возвраты, шт",
    "revenue": "Выручка, ₽",
    "hits_view_search": "Показы в поиске",
    "adv_view_all": "Показы рекламы",
    "adv_sum_all": "Расход на рекламу, ₽",
}


def buyout_rate(ordered: float, delivered: float) -> float:
    """% выкупа = выкупы / заказы * 100."""
    if ordered > 0:
        return round(delivered / ordered * 100, 1)
    return 100.0
