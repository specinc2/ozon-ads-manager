# -*- coding: utf-8 -*-
"""Bright Data — официальный API цен товаров Ozon.

Датасет gd_... принимает URL карточки товара Ozon и возвращает:
название, цену (initial/final/membership), рейтинг, бренд и характеристики.
Работает без обхода антибота — сбор данных выполняет Bright Data.
"""
import asyncio
import logging
from typing import Any

import httpx

logger = logging.getLogger("bright_data")

API_BASE = "https://api.brightdata.com/datasets/v3/scrape"
PROGRESS_URL = "https://api.brightdata.com/datasets/v3/progress/{snapshot_id}"
SNAPSHOT_URL = "https://api.brightdata.com/datasets/v3/snapshot/{snapshot_id}"


class BrightDataError(Exception):
    """Ошибка Bright Data API."""


async def _get(client: httpx.AsyncClient, url: str, headers: dict) -> httpx.Response:
    resp = await client.get(url, headers=headers, timeout=30.0)
    return resp


async def fetch_prices_by_urls(
    api_key: str,
    dataset_id: str,
    urls: list[str],
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Получает цены товаров Ozon по URL карточек.

    Возвращает записи вида {url, sku, name, initial_price, final_price, ...}.
    Кидает BrightDataError при неактивном аккаунте / неверном ключе.
    """
    if not api_key or not dataset_id or not urls:
        return []

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {"input": [{"url": u, "country": ""} for u in urls], "limit_per_input": None}

    async with httpx.AsyncClient(timeout=120.0) as client:
        # 1. Запускаем сбор данных
        resp = await client.post(
            API_BASE,
            params={"dataset_id": dataset_id, "notify": "false", "include_errors": "true"},
            headers=headers,
            json=payload,
        )
        if resp.status_code == 400:
            body = resp.text
            if "Customer is not active" in body:
                raise BrightDataError(
                    "Аккаунт Bright Data неактивен — пополните счёт (brightdata.com → Billing)"
                )
            raise BrightDataError(f"Bright Data: неверный запрос ({body[:200]})")
        if resp.status_code not in (200, 202):
            raise BrightDataError(f"Bright Data: HTTP {resp.status_code} ({resp.text[:200]})")

        data = resp.json()

        # 2. Если готово сразу (список записей) — возвращаем
        if isinstance(data, list):
            return data[:limit]

        # 3. Иначе — ждём готовности snapshot
        snapshot_id = data.get("snapshot_id")
        if not snapshot_id:
            return []

        for _ in range(30):  # до ~5 минут
            await asyncio.sleep(10)
            try:
                prog = await _get(client, PROGRESS_URL.format(snapshot_id=snapshot_id), headers)
                if prog.status_code != 200:
                    continue
                status = prog.json().get("status")
                if status == "ready":
                    snap = await _get(client, SNAPSHOT_URL.format(snapshot_id=snapshot_id), headers)
                    if snap.status_code == 200:
                        records = snap.json()
                        if isinstance(records, list):
                            return records[:limit]
                        if isinstance(records, dict) and records.get("error"):
                            logger.warning("Bright Data snapshot error: %s", records["error"])
                            return []
                    return []
                if status == "error":
                    return []
            except httpx.HTTPError as e:
                logger.warning("Bright Data progress: %s", e)
    return []


def extract_price(record: dict[str, Any]) -> float | None:
    """Достаёт цену из записи Bright Data (приоритет: final → initial → membership)."""
    for key in ("final_price", "initial_price", "membership_price"):
        val = record.get(key)
        if val is not None:
            try:
                price = float(val)
                if price > 0:
                    return price
            except (TypeError, ValueError):
                continue
    return None
