# -*- coding: utf-8 -*-
"""ИИ-улучшатель карточек товаров Ozon (через OmniRoute на Финляндии).

Использует LLM (OpenAI-совместимый API) для генерации:
- SEO-названия (тип + бренд + модель + ключевые характеристики, до 200 симв.)
- Продающего описания (структура, преимущества, ключевые слова)
- Ключевых слов для поиска

Транспорт: AmneziaWG-туннель dv→FI (10.8.1.60↔10.8.1.1), OmniRoute на 10.8.1.1:20128.

Отказоустойчивость:
- список моделей по приоритету (AI_MODELS в .env, через запятую);
- таймаут на модель — 90 секунд, при отказе/таймауте пробуем следующую;
- 5 отказов подряд → модель уходит в кулдаун на 1 час;
- ответ несуществующей модели (404) → вечный кулдаун до рестарта;
- при успехе счётчик отказов модели сбрасывается.
"""
import json
import logging
import os
import time

import httpx

logger = logging.getLogger("ai_card")

AI_API_URL = os.getenv("AI_API_URL", "http://10.8.1.1:20128/v1")
AI_API_KEY = os.getenv("AI_API_KEY", "")
# Модели по приоритету; живая проверенная — первая. Список переопределяется в .env.
_DEFAULT_MODELS = [
    "openrouter/z-ai/glm-5.2:free",
    "openrouter/z-ai/glm-5.2:flash:free",
    "openrouter/deepseek/deepseek-chat:free",
    "openrouter/qwen/qwen3-plus:free",
    "openrouter/meta-llama/llama-4-scout:free",
]
AI_MODELS = [m.strip() for m in os.getenv("AI_MODELS", "").split(",") if m.strip()] or _DEFAULT_MODELS

MODEL_TIMEOUT = 90.0        # 1,5 минуты на модель
MAX_CONSECUTIVE_FAILS = 5   # отказов подряд до кулдауна
COOLDOWN_SECONDS = 3600.0   # кулдаун модели — 1 час

# Состояние моделей в памяти процесса: {model: {"fails": int, "cooldown_until": float}}
_model_state: dict[str, dict] = {}

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


def _is_on_cooldown(model: str) -> bool:
    st = _model_state.get(model)
    return bool(st and st.get("cooldown_until", 0) > time.time())


def _register_fail(model: str, permanent: bool = False) -> None:
    st = _model_state.setdefault(model, {"fails": 0, "cooldown_until": 0.0})
    st["fails"] += 1
    if permanent:
        # Модель не существует — в кулдаун «навсегда» (до рестарта процесса)
        st["cooldown_until"] = time.time() + 10 * 365 * 86400
        logger.warning("AI-модель %s не существует — отключена до рестарта", model)
    elif st["fails"] >= MAX_CONSECUTIVE_FAILS:
        st["cooldown_until"] = time.time() + COOLDOWN_SECONDS
        logger.warning("AI-модель %s: %d отказов подряд — кулдаун 1 час",
                       model, st["fails"])


def _register_success(model: str) -> None:
    _model_state[model] = {"fails": 0, "cooldown_until": 0.0}


def _models_to_try() -> list[str]:
    """Живые модели по приоритету; если все в кулдауне — первая из списка."""
    alive = [m for m in AI_MODELS if not _is_on_cooldown(m)]
    return alive or [AI_MODELS[0]]


async def _try_model(model: str, user_msg: str) -> str:
    """Один запрос к модели. Возвращает текст ответа или бросает AICardError."""
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ],
        "max_tokens": 2500,
        "temperature": 0.7,
    }
    headers = {"Authorization": f"Bearer {AI_API_KEY}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=MODEL_TIMEOUT) as client:
        try:
            resp = await client.post(f"{AI_API_URL}/chat/completions",
                                     headers=headers, json=payload)
        except httpx.HTTPError as e:
            raise AICardError(f"таймаут/сеть: {str(e)[:100]}")
    if resp.status_code == 404 or "is not a valid model" in resp.text or "does not exist" in resp.text:
        _register_fail(model, permanent=True)
        raise AICardError("модель не существует (404)")
    if resp.status_code != 200:
        raise AICardError(f"HTTP {resp.status_code}: {resp.text[:100]}")
    data = resp.json()
    choices = data.get("choices") or []
    if not choices:
        raise AICardError("пустой ответ")
    content = (choices[0].get("message") or {}).get("content") or ""
    if not content.strip():
        raise AICardError("пустой content")
    return content


def _parse_card(content: str) -> dict:
    """Разбирает JSON-ответ модели в карточку."""
    content = content.strip()
    if content.startswith("```"):
        content = content.strip("`")
        if content.startswith("json"):
            content = content[4:]
        content = content.strip()
    start, end = content.find("{"), content.rfind("}")
    if start == -1 or end == -1:
        raise AICardError("ИИ вернул не-JSON")
    try:
        result = json.loads(content[start:end + 1])
    except json.JSONDecodeError:
        raise AICardError("не удалось разобрать JSON")
    title = str(result.get("title", ""))[:200]
    description = str(result.get("description", ""))[:6000]
    keywords = [str(k) for k in (result.get("keywords") or []) if str(k).strip()][:30]
    if not title or not description:
        raise AICardError("пустой результат")
    return {"title": title, "description": description, "keywords": keywords}


async def improve_card(name: str, price: float = 0, category: str = "",
                       extra: str = "") -> dict:
    """Улучшает карточку: пробует модели по списку, возвращает карточку + модель.

    Возвращает {title, description, keywords, model}.
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

    errors: list[str] = []
    tried: list[str] = []
    for model in _models_to_try():
        tried.append(model)
        try:
            content = await _try_model(model, user_msg)
            card = _parse_card(content)
            _register_success(model)
            card["model"] = model
            return card
        except AICardError as e:
            msg = str(e)
            if "не существует" not in msg:  # 404 уже учтён permanent-кулдауном
                _register_fail(model)
            errors.append(f"{model}: {msg}")
            logger.warning("AI-модель %s отказала: %s — пробуем следующую", model, msg)
            continue

    raise AICardError(
        f"ни одна модель не ответила (пробовали: {', '.join(tried)}). "
        f"Последние ошибки: {'; '.join(errors[-2:])}"
    )


# ---------------------------------------------------------------------------
# Rich Content JSON (Ozon)
# ---------------------------------------------------------------------------

RICH_SYSTEM_PROMPT = """Ты — эксперт по продающим описаниям товаров Ozon Rich Content.
Структурируй описание товара в секции для богатого контента.

Отвечай СТРОГО JSON без markdown:
{
  "intro": "краткое вступление 1-2 предложения",
  "sections": [
    {"title": "Заголовок секции", "paragraphs": ["абзац текста"], "bullets": ["пункт списка"]},
    ...
  ]
}

Требования: 3-5 секций («Описание», «Преимущества», «Как использовать», «Кому подойдёт»).
Секция может иметь paragraphs И/ИЛИ bullets (bullets — 3-6 пунктов).
Пиши по-русски, без выдуманных характеристик, которых нет в данных."""


def _build_rich_json(intro: str, sections: list) -> dict:
    """Превращает секции ИИ в Rich Content JSON Ozon (виджеты)."""
    content: list[dict] = []

    def text_block(text: str, size: str = "size2") -> dict:
        return {"widgetName": "raTextBlock",
                "text": {"size": size, "color": "color1", "content": [str(text)]}}

    if intro:
        content.append(text_block(intro))
    for sec in sections or []:
        title = str(sec.get("title", "")).strip()
        if title:
            # Заголовок секции — крупный текст (size5 ≈ h3)
            content.append(text_block(title, "size5"))
        for p in sec.get("paragraphs") or []:
            if str(p).strip():
                content.append(text_block(p))
        bullets = [str(b).strip() for b in (sec.get("bullets") or []) if str(b).strip()]
        if bullets:
            content.append({
                "widgetName": "list",
                "theme": "bullet",
                "blocks": [{"widgetName": "raTextBlock",
                            "text": {"size": "size2", "color": "color1",
                                     "content": [b]}} for b in bullets],
            })
    return {"content": content, "version": 0.3}


def _parse_rich(content: str) -> dict:
    """Разбирает JSON-секции ИИ и строит Rich Content JSON."""
    content = content.strip()
    if content.startswith("```"):
        content = content.strip("`")
        if content.startswith("json"):
            content = content[4:]
        content = content.strip()
    start, end = content.find("{"), content.rfind("}")
    if start == -1 or end == -1:
        raise AICardError("ИИ вернул не-JSON")
    try:
        data = json.loads(content[start:end + 1])
    except json.JSONDecodeError:
        raise AICardError("не удалось разобрать JSON rich-контента")
    sections = data.get("sections") or []
    if not sections and not data.get("intro"):
        raise AICardError("пустой rich-контент")
    return _build_rich_json(data.get("intro", ""), sections)


async def generate_rich_content(name: str, price: float = 0, category: str = "",
                                extra: str = "") -> dict:
    """Генерирует Rich Content JSON Ozon для товара. Возвращает {rich, model}."""
    if not AI_API_KEY:
        raise AICardError("ИИ не настроен: задайте AI_API_KEY в .env")

    user_msg = f"Товар: {name}\nЦена: {price:.0f} ₽\n"
    if category:
        user_msg += f"Категория: {category}\n"
    if extra:
        user_msg += f"Данные: {extra}\n"

    errors: list[str] = []
    for model in _models_to_try():
        try:
            payload = {
                "model": model,
                "messages": [
                    {"role": "system", "content": RICH_SYSTEM_PROMPT},
                    {"role": "user", "content": user_msg},
                ],
                "max_tokens": 3000,
                "temperature": 0.7,
            }
            headers = {"Authorization": f"Bearer {AI_API_KEY}",
                       "Content-Type": "application/json"}
            async with httpx.AsyncClient(timeout=MODEL_TIMEOUT) as client:
                resp = await client.post(f"{AI_API_URL}/chat/completions",
                                         headers=headers, json=payload)
            if resp.status_code == 404 or "is not a valid model" in resp.text:
                _register_fail(model, permanent=True)
                continue
            if resp.status_code != 200:
                raise AICardError(f"HTTP {resp.status_code}")
            choices = resp.json().get("choices") or []
            if not choices:
                raise AICardError("пустой ответ")
            raw = (choices[0].get("message") or {}).get("content") or ""
            rich = _parse_rich(raw)
            _register_success(model)
            return {"rich": rich, "model": model}
        except AICardError as e:
            _register_fail(model)
            errors.append(f"{model}: {e}")
            continue
    raise AICardError(f"rich-контент не сгенерирован: {'; '.join(errors[-2:])}")


# ---------------------------------------------------------------------------
# Авто-обновление ТОПа free-моделей (раз в 10 дней, models.dev + зонд)
# ---------------------------------------------------------------------------

# Известные надёжные семейства — приоритет при сортировке кандидатов
_PREFERRED_FAMILIES = ("z-ai", "google", "meta", "deepseek", "qwen", "mistral", "moonshot")


async def refresh_top_models(max_probe: int = 6) -> dict:
    """Обновляет порядок AI_MODELS: каталог models.dev (:free, cost=0) + живой зонд.

    Возвращает {"checked": N, "alive": [...], "order": [...]}.
    """
    global AI_MODELS
    import asyncio as _asyncio

    candidates: list[str] = []
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get("https://models.dev/api.json")
            if resp.status_code == 200:
                catalog = resp.json()
                models = (catalog.get("openrouter") or {}).get("models") or {}
                for mid, m in models.items():
                    cost = m.get("cost") or {}
                    if mid.endswith(":free") and cost.get("input") == 0 and cost.get("output") == 0:
                        candidates.append(f"openrouter/{mid}")
    except Exception as e:
        logger.warning("models.dev недоступен: %s", e)

    # Приоритет: текущие модели (не теряем рабочую) → предпочитаемые семейства → остальные
    current = [m for m in AI_MODELS if m in candidates or m.startswith("openrouter/")]
    preferred = [m for m in candidates
                 if any(m.split("/")[1].startswith(f) for f in _PREFERRED_FAMILIES if "/" in m)]
    rest = [m for m in candidates if m not in preferred]
    ordered = list(dict.fromkeys(current + preferred + rest))

    # Зонд: короткий запрос к топ-N кандидатам (кроме известных мёртвых)
    probe_list = [m for m in ordered if not _is_on_cooldown(m)][:max_probe]
    alive: list[str] = []
    for model in probe_list:
        try:
            payload = {
                "model": model,
                "messages": [{"role": "user", "content": "ок"}],
                "max_tokens": 5,
            }
            headers = {"Authorization": f"Bearer {AI_API_KEY}",
                       "Content-Type": "application/json"}
            async with httpx.AsyncClient(timeout=45.0) as client:
                resp = await client.post(f"{AI_API_URL}/chat/completions",
                                         headers=headers, json=payload)
            if resp.status_code == 404 or "is not a valid model" in resp.text:
                _register_fail(model, permanent=True)
            elif resp.status_code == 200 and (resp.json().get("choices") or []):
                alive.append(model)
                _register_success(model)
            else:
                _register_fail(model)
        except Exception:
            _register_fail(model)

    # Новый порядок: живые (в порядке зонда) → остальные кандидаты
    new_order = alive + [m for m in ordered if m not in alive]
    if new_order:
        AI_MODELS = new_order[:10]
    logger.info("AI-модели обновлены: каталог=%d, зонд живых=%d, порядок=%s",
                len(candidates), len(alive), AI_MODELS[:5])
    return {"checked": len(probe_list), "alive": alive, "order": AI_MODELS[:5]}
