# AGENTS.md — Ozon Ads Manager

> Полная карта проекта: **[docs/AGENTS.md](docs/AGENTS.md)**
> Граф зависимостей кода: **[docs/codegraph.html](docs/codegraph.html)**

## Что это

Веб-приложение (FastAPI + SQLAlchemy async + Jinja2) для управления рекламой Ozon
через Performance API 2.0 и анализа цен конкурентов (WB, Яндекс.Маркет, Ozon через Bright Data).

- **Запуск:** `python run.py` (uvicorn на :8000), прод — Docker на `127.0.0.1:8002`
  за nginx `searx.dungeonverse.ru` (таймаут 300s).
- **БД:** SQLite `data/ozon_ads.db` (авто-миграции колонок в `app/database.py`).
- **Планировщик:** APScheduler в `app/jobs.py`, каждые 8 минут.

## Ключевые файлы для быстрого старта

| Файл | Зачем |
|---|---|
| `app/routers/analyzer.py` | Анализатор цен: фото → Яндекс.Картинки → цены → рекомендация → история |
| `app/services/photo_search.py` | Поиск по фото (ссылки + цены + миниатюры из выдачи Яндекса) |
| `app/services/market_search.py` | Текстовый поиск цен (WB, Ozon, Яндекс.Маркет, AliExpress) |
| `app/services/bright_data.py` | Цены Ozon по URL карточки (Bright Data API) |
| `app/services/ozon_client.py` | Ozon Performance API 2.0 (OAuth2 client_credentials) |
| `app/routers/pages.py` | Все HTML-страницы и настройки |
| `app/models.py` | Все ORM-модели |
| `app/services/seller_client.py` | Ozon Seller API (экономика своих товаров) |

## Главный поток анализатора

```
POST /analyzer/api
  1. Сохранить фото (data/uploads/) → публичный URL через _absolute() (учитывает X-Forwarded-*)
  2. Для каждого фото: Яндекс.Картинки (photo_search.py) → ссылки /product/ + цены + миниатюры
  3. Текстовый поиск (market_search.py): WB (API), Яндекс.Маркет (HTML/JSON), Ozon (антибот — не работает)
  4. По Ozon-ссылкам из фото: Bright Data (bright_data.py) → точные цены
  5. recommender.py: вилка цен, медиана, рекомендация (цена, маржа, ДРР)
  6. Сохранение в AnalyzerHistory (история)
```

## Важно знать

- **Фото для Яндекса** должно быть по публичному URL — `_absolute()` берёт домен
  из заголовков `X-Forwarded-Proto` / `Host` (иначе Яндекс не найдёт товары).
- **Bright Data** (`gd_lutq85sl13rlndbzai`): если аккаунт неактивен —
  "Customer is not active" → пополнить счёт. Формат ответа: JSON lines / dict / snapshot.
- **Антибот Ozon** не решён для дата-центр IP: `/abt/result` → `{"ok":false}`.
  Рабочие обходы: Bright Data по URL, Яндекс.Маркет.
- **Бюджеты Ozon** в микрорублях (÷ 1 000 000).
- Сессии/пароли: PBKDF2; API-ключи шифруются Fernet (`ENCRYPTION_KEY`).
