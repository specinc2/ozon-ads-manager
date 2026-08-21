"""Поиск по фото через Яндекс.Картинки и извлечение цен.

Поток:
1. Пользователь загружает фото — оно сохраняется на НАШЕМ сервере
   и доступно по URL (https://<домен>/static-uploads/...).
2. Яндекс.Картинки ищут похожие изображения по этому URL (rpt=imageview).
   Важно: фото должно быть доступно по публичному URL — Яндекс не сможет
   обработать ссылку на чужой CDN с антиботом (например ir.ozone.ru).
3. Из HTML-ответа извлекаем:
   - ссылки на карточки товаров (Ozon, Wildberries, Яндекс.Маркет, AliExpress)
   - цены прямо из выдачи ("Цена 262₽. Старая цена 275₽")
4. Дальше по ссылкам на Ozon берём точные цены через Bright Data
   (в analyzer.py).
"""
import logging
import re
import urllib.parse
from dataclasses import dataclass, field

import httpx

logger = logging.getLogger("photo_search")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0 Safari/537.36")

MARKETPLACE_DOMAINS = {
    "ozon.ru": "Ozon",
    "wildberries.ru": "Wildberries",
    "market.yandex.ru": "Яндекс.Маркет",
    "aliexpress.com": "AliExpress",
    "wb.ru": "Wildberries",
    "sbermegamarket.ru": "Мегамаркет",
}


@dataclass
class FoundLink:
    url: str
    marketplace: str
    title: str = ""
    price: float = 0  # цена из выдачи Яндекса (если найдена)
    image: str = ""   # миниатюра товара из выдачи


@dataclass
class PhotoSearchResult:
    ok: bool
    links: list[FoundLink] = field(default_factory=list)
    error: str = ""
    total_results: int = 0
    prices: list[float] = field(default_factory=list)  # цены из выдачи
    search_urls: dict = field(default_factory=dict)  # внешние сервисы поиска по фото


class YandexPhotoSearch:
    """Поиск по фото в Яндекс.Картинках с извлечением цен и ссылок на товары."""

    def __init__(self):
        self._client = httpx.AsyncClient(
            timeout=25.0,
            headers={"User-Agent": UA, "Accept-Language": "ru-RU,ru;q=0.9"},
            follow_redirects=True,
        )

    async def close(self):
        await self._client.aclose()

    async def search_by_url(self, image_url: str, limit: int = 30) -> PhotoSearchResult:
        """Ищет похожие товары по URL фото (должен быть доступен публично)."""
        search_urls = {
            "yandex_products": "https://yandex.ru/images/search?" + urllib.parse.urlencode({
                "rpt": "imageview", "url": image_url, "cbir_page": "products",
            }),
            "yandex_similar": "https://yandex.ru/images/search?" + urllib.parse.urlencode({
                "rpt": "imageview", "url": image_url, "cbir_page": "similar",
            }),
            "google_lens": "https://lens.google.com/uploadbyurl?" + urllib.parse.urlencode({
                "url": image_url,
            }),
        }

        url = "https://yandex.ru/images/search?" + urllib.parse.urlencode({
            "rpt": "imageview", "url": image_url, "cbir_page": "products",
        })
        try:
            resp = await self._client.get(url)
        except httpx.HTTPError as e:
            return PhotoSearchResult(ok=False, error=f"Сеть: {e}", search_urls=search_urls)
        if resp.status_code != 200:
            return PhotoSearchResult(ok=False, error=f"HTTP {resp.status_code}", search_urls=search_urls)

        html = resp.text
        links = self._extract_marketplace_links(html, limit)
        prices = self._extract_prices(html)
        total = self._count_results(html)

        if not links and "captcha" in html.lower():
            return PhotoSearchResult(ok=False, error="Яндекс показал капчу — попробуйте позже",
                                     search_urls=search_urls)
        return PhotoSearchResult(ok=bool(links) or bool(prices) or total > 0,
                                 links=links, prices=prices,
                                 total_results=total, search_urls=search_urls)

    def _extract_marketplace_links(self, html: str, limit: int) -> list[FoundLink]:
        """Ищет ссылки на карточки товаров в выдаче Яндекса + миниатюры и цены."""
        found: list[FoundLink] = []
        seen: set[str] = set()

        # Миниатюры товаров из выдачи (Яндекс хранит их на avatars.mds.yandex.net)
        images = re.findall(r'<img[^>]*src="(https://avatars\.mds\.yandex\.net/[^"]+)"[^>]*>', html)
        # Картинки в data-атрибутах (JSON-разметка)
        if not images:
            images = re.findall(r'"image":"(https://avatars\.mds\.yandex\.net/[^"]+)"', html)
        images = [img.replace("&amp;", "&") for img in images]
        img_iter = iter(images)

        # Ссылки вида href="https://www.ozon.ru/product/..." (и другие маркетплейсы)
        for m in re.finditer(r'href="(https?://[^"]+)"', html):
            raw_url = m.group(1)
            url = urllib.parse.unquote(raw_url)
            market = self._detect_marketplace(url)
            if market and "/product/" in url and url not in seen:
                seen.add(url)
                image = next(img_iter, "")
                found.append(FoundLink(url=url[:500], marketplace=market, image=image))
                if len(found) >= limit:
                    break

        return found

    def _extract_prices(self, html: str) -> list[float]:
        """Цены из выдачи Яндекса: "Цена 262₽. Старая цена 275₽"."""
        prices: list[float] = []
        seen: set[float] = set()
        patterns = [
            r'Цена\s*([\d\s\u00a0]+)₽',
            r'([\d\s\u00a0]{2,9})\s?₽',
        ]
        for pat in patterns:
            for m in re.finditer(pat, html):
                try:
                    raw = m.group(1).replace(" ", "").replace("\u00a0", "")
                    price = float(raw)
                    if 5 < price < 10_000_000 and price not in seen:
                        seen.add(price)
                        prices.append(price)
                except (ValueError, IndexError):
                    continue
            if len(prices) >= 30:
                break
        return prices[:30]

    def _count_results(self, html: str) -> int:
        m = re.search(r'(\d[\d\s]*)\s*(?:результат|изображени)', html)
        if m:
            try:
                return int(m.group(1).replace(" ", ""))
            except ValueError:
                pass
        return 0

    @staticmethod
    def _detect_marketplace(url: str) -> str | None:
        lowered = url.lower()
        for domain, name in MARKETPLACE_DOMAINS.items():
            if domain in lowered:
                return name
        return None


# ---------------------------------------------------------------------------
# Загрузка фото
# ---------------------------------------------------------------------------

import os
import uuid
from pathlib import Path

UPLOAD_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "uploads"


def save_upload(photo_bytes: bytes, ext: str = "jpg") -> str:
    """Сохраняет фото в data/uploads и возвращает относительный URL."""
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"{uuid.uuid4().hex}.{ext}"
    (UPLOAD_DIR / filename).write_bytes(photo_bytes)
    return f"/static-uploads/{filename}"
