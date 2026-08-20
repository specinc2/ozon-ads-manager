"""Поиск цен конкурентов по маркетплейсам.

Поддерживаемые источники:
- Wildberries (WB) — публичный API поиска, цены в копейках;
- Яндекс.Маркет — парсинг HTML-страницы поиска;
- Ozon и AliExpress — часто закрыты антиботом, при неудаче помечаются.

Каждый провайдер возвращает список цен (рубли) с названиями товаров.
"""
import asyncio
import logging
import re
import urllib.parse
from dataclasses import dataclass, field

import httpx

logger = logging.getLogger("market_search")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0 Safari/537.36")


@dataclass
class PricePoint:
    price: float          # цена в рублях
    name: str = ""
    marketplace: str = ""
    url: str = ""


@dataclass
class SearchResult:
    marketplace: str
    ok: bool
    prices: list[PricePoint] = field(default_factory=list)
    error: str = ""


class MarketSearch:
    """Ищет цены по запросу на нескольких маркетплейсах."""

    def __init__(self):
        self._client = httpx.AsyncClient(
            timeout=20.0,
            headers={"User-Agent": UA, "Accept-Language": "ru-RU,ru;q=0.9"},
            follow_redirects=True,
        )

    async def close(self):
        await self._client.aclose()

    async def search_all(self, query: str, limit: int = 20) -> list[SearchResult]:
        """Ищет по всем источникам параллельно. Ошибки не роняют общий результат."""
        results: list[SearchResult] = []
        for marketplace in ("wb", "ozon", "ym", "aliexpress"):
            try:
                if marketplace == "wb":
                    result = await self.search_wb(query, limit)
                elif marketplace == "ozon":
                    result = await self.search_ozon(query, limit)
                elif marketplace == "ym":
                    result = await self.search_yandex_market(query, limit)
                else:
                    result = await self.search_aliexpress(query, limit)
            except Exception as e:
                logger.warning("Поиск на %s не удался: %s", marketplace, e)
                result = SearchResult(marketplace=marketplace, ok=False, error=str(e)[:200])
            results.append(result)
        return results

    # ------------------------------------------------------------------
    # Wildberries
    # ------------------------------------------------------------------

    async def search_wb(self, query: str, limit: int = 20) -> SearchResult:
        url = "https://search.wb.ru/exactmatch/ru/common/v5/search?" + urllib.parse.urlencode({
            "query": query, "curr": "rub", "dest": "123585749",
            "resultset": "catalog", "sort": "popular", "limit": limit,
        })
        resp = None
        for attempt in range(3):
            resp = await self._client.get(url, headers={"Accept": "application/json"})
            if resp.status_code == 429:
                # WB жёстко лимитирует — ждём между попытками
                await asyncio.sleep(5 + attempt * 8)
                continue
            break
        if resp is None or resp.status_code == 429:
            return SearchResult(marketplace="wb", ok=False, error="Wildberries: лимит запросов (429), попробуйте позже")
        resp.raise_for_status()
        data = resp.json()
        points: list[PricePoint] = []
        for p in data.get("products", [])[:limit]:
            sizes = p.get("sizes") or []
            price_cop = (sizes[0].get("price") or {}).get("product") if sizes else None
            if price_cop is None:
                continue
            name = p.get("name") or ""
            points.append(PricePoint(
                price=round(price_cop / 100, 2),
                name=name,
                marketplace="wb",
                url=f"https://www.wildberries.ru/catalog/{p.get('id')}/detail.aspx",
            ))
        return SearchResult(marketplace="wb", ok=True, prices=points)

    # ------------------------------------------------------------------
    # Ozon
    # ------------------------------------------------------------------

    async def search_ozon(self, query: str, limit: int = 20) -> SearchResult:
        url = "https://www.ozon.ru/api/entrypoint/v4/search?" + urllib.parse.urlencode({
            "text": query, "page": 1,
        })
        resp = await self._client.get(url, headers={"Accept": "application/json"})
        if resp.status_code in (307, 403, 404) or "block" in resp.text.lower()[:200]:
            return SearchResult(marketplace="ozon", ok=False, error="Ozon закрыт антиботом (307/403)")
        resp.raise_for_status()
        data = resp.json()
        # Обработка, если всё же удалось получить данные
        items = data.get("items") or data.get("widgetStates") or []
        points: list[PricePoint] = []
        for it in items:
            if isinstance(it, dict):
                name = it.get("title") or it.get("name") or ""
                price_data = it.get("price") or {}
                price = price_data.get("price") or price_data.get("value")
                if price:
                    points.append(PricePoint(price=float(price), name=name, marketplace="ozon"))
        if not points:
            return SearchResult(marketplace="ozon", ok=False, error="Нет данных в ответе Ozon")
        return SearchResult(marketplace="ozon", ok=True, prices=points)

    # ------------------------------------------------------------------
    # Яндекс.Маркет
    # ------------------------------------------------------------------

    async def search_yandex_market(self, query: str, limit: int = 20) -> SearchResult:
        url = "https://market.yandex.ru/search?" + urllib.parse.urlencode({
            "text": query, "lr": "213",
        })
        resp = await self._client.get(url)
        if resp.status_code != 200:
            return SearchResult(marketplace="ym", ok=False, error=f"HTTP {resp.status_code}")
        html = resp.text

        points: list[PricePoint] = []
        # Ищем цены в JSON-данных страницы (data-baobab-name="price" и т.п.)
        # Шаблон: "price":{"value":1234.5,"currency":"RUR" или data-autotest-id
        patterns = [
            r'"price":\s*\{\s*"value":\s*([\d.]+)',
            r'"priceValue":\s*([\d.]+)',
            r'data-autotest-id="price[^"]*"[^>]*>([\d\s]+)\s*₽',
            r'([\d\s]{4,}\s?₽)',
        ]
        for pat in patterns:
            for m in re.finditer(pat, html):
                try:
                    raw = m.group(1).replace(" ", "").replace("\u00a0", "").replace("₽", "")
                    price = float(raw)
                    if 5 < price < 10_000_000:
                        points.append(PricePoint(price=price, marketplace="ym"))
                except (ValueError, IndexError):
                    continue
            if len(points) >= limit:
                break

        # Дедупликация и ограничение
        seen = set()
        uniq: list[PricePoint] = []
        for p in points:
            if p.price not in seen:
                seen.add(p.price)
                uniq.append(p)
            if len(uniq) >= limit:
                break
        if not uniq:
            return SearchResult(marketplace="ym", ok=False, error="Цены не найдены на странице")
        return SearchResult(marketplace="ym", ok=True, prices=uniq)

    # ------------------------------------------------------------------
    # AliExpress
    # ------------------------------------------------------------------

    async def search_aliexpress(self, query: str, limit: int = 20) -> SearchResult:
        url = "https://www.aliexpress.com/wholesale?" + urllib.parse.urlencode({"SearchText": query})
        resp = await self._client.get(url)
        if resp.status_code != 200:
            return SearchResult(marketplace="aliexpress", ok=False, error=f"HTTP {resp.status_code}")
        html = resp.text
        # AliExpress часто требует JS — цены обычно не извлекаются
        patterns = [
            r'"formatedPrice":"([\d.,]+)"',
            r'"minPrice":\s*([\d.]+)',
            r'US\s?\$?([\d.,]+)',
        ]
        points: list[PricePoint] = []
        for pat in patterns:
            for m in re.finditer(pat, html):
                try:
                    raw = m.group(1).replace(",", ".")
                    price = float(raw)
                    if 0.5 < price < 100_000:
                        # из USD в RUB примерно
                        points.append(PricePoint(price=round(price * 90, 2), marketplace="aliexpress"))
                except (ValueError, IndexError):
                    continue
            if len(points) >= limit:
                break
        if not points:
            return SearchResult(marketplace="aliexpress", ok=False, error="Требуется JS (цены не извлечены)")
        return SearchResult(marketplace="aliexpress", ok=True, prices=points[:limit])


# ---------------------------------------------------------------------------
# Анализ вилок цен
# ---------------------------------------------------------------------------

@dataclass
class PriceBucket:
    label: str
    price_from: float
    price_to: float
    count: int
    percent: float


def analyze_prices(prices: list[float], bucket_size: float = 100.0) -> dict:
    """Разбивает цены на вилки и считает доли продавцов в каждой.

    bucket_size — ширина вилки в рублях (по умолчанию 100 ₽).
    Возвращает словарь с вилками, медианой, средней, рекомендацией.
    """
    if not prices:
        return {"buckets": [], "median": None, "mean": None, "min": None, "max": None,
                "recommended_price": None}

    prices = sorted(prices)
    total = len(prices)
    min_p, max_p = prices[0], prices[-1]

    # Вилки: от min округляем вниз до кратности bucket_size
    start = int(min_p // bucket_size) * bucket_size
    buckets: dict[tuple[float, float], int] = {}
    for p in prices:
        idx = int(p // bucket_size)
        lo = idx * bucket_size
        hi = (idx + 1) * bucket_size
        buckets[(lo, hi)] = buckets.get((lo, hi), 0) + 1

    bucket_list: list[PriceBucket] = []
    for (lo, hi), count in sorted(buckets.items()):
        bucket_list.append(PriceBucket(
            label=f"{int(lo)}–{int(hi)} ₽",
            price_from=lo, price_to=hi, count=count,
            percent=round(count / total * 100, 1),
        ))

    # Медиана
    mid = total // 2
    median = prices[mid] if total % 2 else (prices[mid - 1] + prices[mid]) / 2
    mean = sum(prices) / total

    # Рекомендация: медиана чуть ниже (конкурентная цена)
    recommended = round(median * 0.95, 2)
    # Корректируем на самый населённый сегмент
    top_bucket = max(bucket_list, key=lambda b: b.count)
    if top_bucket.price_from < recommended < top_bucket.price_to:
        recommended = round(top_bucket.price_from + (top_bucket.price_to - top_bucket.price_from) * 0.4, 2)

    return {
        "buckets": bucket_list,
        "median": round(median, 2),
        "mean": round(mean, 2),
        "min": min_p,
        "max": max_p,
        "recommended_price": recommended,
        "total": total,
    }
