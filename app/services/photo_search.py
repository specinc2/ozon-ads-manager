"""Поиск по фото через Яндекс.Картинки и извлечение ссылок на маркетплейсы.

Поток:
1. Пользователь загружает фото — оно сохраняется и становится доступно по URL.
2. Яндекс.Картинки ищут похожие изображения (rpt=imageview&url=<url>).
3. Из HTML-ответа извлекаем ссылки на Ozon, Wildberries, Яндекс.Маркет, AliExpress.
4. Для найденных ссылок пытаемся вытащить цены (там, где это возможно без JS).

Ограничения: Яндекс может показывать капчу; Ozon/Ali закрыты антиботом —
тогда ссылки всё равно показываются пользователю как «найдено», а цена помечается
как недоступная.
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


@dataclass
class PhotoSearchResult:
    ok: bool
    links: list[FoundLink] = field(default_factory=list)
    error: str = ""
    total_results: int = 0
    search_urls: dict = field(default_factory=dict)  # внешние сервисы поиска по фото


class YandexPhotoSearch:
    """Поиск по фото в Яндекс.Картинках.

    Яндекс возвращает JS-страницу, из которой надёжно извлекаются только ссылки
    на внешние сервисы. Поэтому дополнительно формируем готовые URL для поиска
    по фото в Яндекс.Картинках и Google Lens — пользователь может открыть их,
    чтобы увидеть похожие товары и ссылки на маркетплейсы.
    """

    def __init__(self):
        self._client = httpx.AsyncClient(
            timeout=18.0,
            headers={"User-Agent": UA, "Accept-Language": "ru-RU,ru;q=0.9"},
            follow_redirects=True,
        )

    async def close(self):
        await self._client.aclose()

    async def search_by_url(self, image_url: str, limit: int = 30) -> PhotoSearchResult:
        """Ищет похожие изображения по URL фото.

        Пытается извлечь ссылки на маркетплейсы из HTML-ответа Яндекса,
        а также возвращает ссылки на внешние сервисы поиска по фото.
        """
        search_urls = {
            "yandex": "https://yandex.ru/images/search?" + urllib.parse.urlencode({
                "rpt": "imageview", "url": image_url,
            }),
            "google_lens": "https://lens.google.com/uploadbyurl?" + urllib.parse.urlencode({
                "url": image_url,
            }),
        }

        url = "https://yandex.ru/images/search?" + urllib.parse.urlencode({
            "rpt": "imageview", "url": image_url,
            "cbir_id": "cbir_id", "cbir_page": "similar",
        })
        try:
            resp = await self._client.get(url)
        except httpx.HTTPError as e:
            return PhotoSearchResult(ok=False, error=f"Сеть: {e}", search_urls=search_urls)
        if resp.status_code != 200:
            return PhotoSearchResult(ok=False, error=f"HTTP {resp.status_code}", search_urls=search_urls)

        html = resp.text
        links = self._extract_marketplace_links(html, limit)
        total = self._count_results(html)

        if not links and "captcha" in html.lower():
            return PhotoSearchResult(ok=False, error="Яндекс показал капчу — попробуйте позже",
                                     search_urls=search_urls)
        return PhotoSearchResult(ok=bool(links) or total > 0, links=links,
                                 total_results=total, search_urls=search_urls)

    def _extract_marketplace_links(self, html: str, limit: int) -> list[FoundLink]:
        """Ищет ссылки на маркетплейсы в HTML поиска по фото."""
        found: list[FoundLink] = []
        seen: set[str] = set()

        # Ссылки вида "href":"https://www.ozon.ru/..." или href="https://..."
        for m in re.finditer(r'href="(https?://[^"]+)"', html):
            raw_url = m.group(1)
            url = urllib.parse.unquote(raw_url)
            market = self._detect_marketplace(url)
            if market and url not in seen:
                seen.add(url)
                found.append(FoundLink(url=url[:500], marketplace=market))
                if len(found) >= limit:
                    break

        # Дубликаты с www и без — оставляем первые
        return found

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
