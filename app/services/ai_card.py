# -*- coding: utf-8 -*-
"""ИИ-улучшатель карточек товаров Ozon (через OmniRoute на Финляндии).

Использует LLM (OpenAI-совместимый API) для генерации:
- SEO-названия (тип + бренд + модель + ключевые характеристики, до 200 симв.)
- Продающего описания (структура, преимущества, ключевые слова)
- Ключевых слов для поиска

Транспорт: AmneziaWG-туннель dv→FI (10.8.1.60↔10.8.1.1), OmniRoute на 10.8.1.1:20128.
"""
import json
import logging
import os

import httpx

logger = logging.getLogger("ai_card")

AI_API_URL = os.getenv("AI_API_URL", "http://10.8.1.1:20128/v1")
AI_API_KEY = os.getenv("AI_API_KEY", "")
AI_MODEL = os.getenv("AI_MODEL", "openrouter/z-ai/glm-5.2:free")

SYSTEM_PROMPT = """Ты — эксперт по карточкам товаров маркетплейса Ozon.
Ты улучшаешь карточки: SEO-название, продающее описание, ключевые слова.

Правила Ozon:
- Название: до 200 символов, стиль «Тип + Бренд/модель + ключевые характеристики»,
  без КАПСА, без слов «скидка/акция/хит», без контактов.
- Описание: до 3000 символов, структурированное: кто товар, для кого, ключевые
  преимущества (списком), сценарии использования, призыв. Без выдуманных характеристик,
  которых нет в исходных данных. Пиши по-русски.
- Ключевые слова: 15-25 штук, поисковые фразы покупателей (без повторов названия).

Отвечай СТРОГО JSON-объектом без markdown-обёртки:
{"title": "...", "description": "...", "keywords": ["...", "..."]}"""


class AICardError(Exception):
    """Ошибка ИИ-улучшения."""


async def improve_card(name: str, price: float = 0, category: str = "",
                       extra: str = "") -> dict:
    """Улучшает карточку товара. Возвращает {title, description, keywords}.

    name     — текущее название товара
    price    — цена продажи (для контекста)
    category — категория/тип (если известна)
    extra    — доп. характеристики текстом (необязательно)
    """
    if not AI_API_KEY:
        raise AICardError("ИИ не настроен: задайте AI_API_KEY в .env")

    user_msg = (
        f"Улучши карточку товара Ozon.\n"
        f"Текущее название: {name}\n"
        f"Цена: {price:.0f} ₽\n"
    )
    if category:
        user_msg += f"Категория: {category}\n"
    if extra:
        user_msg += f"Характеристики: {extra}\n"

    payload = {
        "model": AI_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ],
        "max_tokens": 2000,
        "temperature": 0.7,
    }
    headers = {"Authorization": f"Bearer {AI_API_KEY}", "Content-Type": "application/json"}

    async with httpx.AsyncClient(timeout=120.0) as client:
        try:
            resp = await client.post(
                f"{AI_API_URL}/chat/completions", headers=headers, json=payload
            )
        except httpx.HTTPError as e:
            raise AICardError(f"ИИ-шлюз недоступен: {str(e)[:120]}")
        if resp.status_code != 200:
            raise AICardError(f"ИИ-ошибка HTTP {resp.status_code}: {resp.text[:150]}")

        data = resp.json()
        choices = data.get("choices") or []
        if not choices:
            raise AICardError("ИИ не вернул ответ")
        content = (choices[0].get("message") or {}).get("content") or ""

    # Модель может вернуть JSON с обёрткой — вычищаем
    content = content.strip()
    if content.startswith("```"):
        content = content.strip("`")
        if content.startswith("json"):
            content = content[4:]
        content = content.strip()
    # Берём JSON от первой { до последней }
    start, end = content.find("{"), content.rfind("}")
    if start == -1 or end == -1:
        raise AICardError("ИИ вернул не-JSON ответ")
    try:
        result = json.loads(content[start:end + 1])
    except json.JSONDecodeError:
        raise AICardError("Не удалось разобрать ответ ИИ")

    title = str(result.get("title", ""))[:200]
    description = str(result.get("description", ""))[:6000]
    keywords = [str(k) for k in (result.get("keywords") or []) if str(k).strip()][:30]
    if not title or not description:
        raise AICardError("ИИ вернул пустой результат")

    return {"title": title, "description": description, "keywords": keywords}
