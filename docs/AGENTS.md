# Ozon Ads Manager — Карта проекта для AI-ассистентов

## Кратко

Веб-приложение для управления рекламными кампаниями Ozon (Performance API 2.0) с автоматизацией: авто-бюджет, расписания, бидер ставок + анализатор цен конкурентов (WB, Яндекс.Маркет, Ozon через Bright Data).

- **Стек:** Python 3.11, FastAPI (async), SQLAlchemy 2.0 (async), Pydantic v2, Jinja2, Bootstrap 5, Chart.js, APScheduler.
- **БД:** SQLite (дефолт), PostgreSQL (docker-compose).
- **Запуск:** `run.py` (uvicorn) или Docker.
- **Домен:** `searx.dungeonverse.ru` (nginx → 127.0.0.1:8002).

---

## Структура проекта

```
ozon-ads/
├── app/
│   ├── main.py                   # FastAPI приложение, монтирование роутеров, статика
│   ├── config.py                 # Настройки из .env (pydantic-settings)
│   ├── database.py               # SQLAlchemy engine + async session + миграции колонок
│   ├── models.py                 # ORM-модели: User, ApiKey, Campaign, ProxySetting, AnalyzerHistory…
│   ├── security.py               # Хеширование паролей (PBKDF2), шифрование ключей (Fernet)
│   │
│   ├── routers/
│   │   ├── pages.py              # HTML-страницы: дашборд, кампании, настройки, статистика…
│   │   ├── api.py                # JSON-API: AJAX-действия (старт/стоп, бюджеты, ставки, экспорт)
│   │   └── analyzer.py           # Анализатор цен: страница + API (поиск по фото, цены, рекомендации)
│   │
│   ├── services/
│   │   ├── ozon_client.py        # Клиент Ozon Performance API 2.0 (OAuth2 client_credentials)
│   │   ├── market_search.py      # Поиск цен: WB, Ozon, Яндекс.Маркет, AliExpress + антибот-цепочка
│   │   ├── photo_search.py       # Поиск по фото в Яндекс.Картинках → ссылки + цены из выдачи
│   │   ├── bright_data.py        # Bright Data API: цены Ozon по URL товара
│   │   ├── seller_client.py      # Клиент Ozon Seller API (официальный API продавца)
│   │   ├── seller_sync.py        # Синхронизация экономики товаров (остатки, цены, комиссии)
│   │   ├── campaigns.py          # CRUD кампаний в кэше БД
│   │   ├── statistics.py         # Агрегация статистики, кэширование по дням
│   │   ├── products.py           # Товары кампаний, ставки, массовое обновление
│   │   ├── economics.py          # Экономика: себестоимость, комиссии, логистика
│   │   ├── rules_engine.py       # Движок авто-правил (авто-бюджет, авто-ставки)
│   │   ├── schedule_service.py   # Расписания работы кампаний (время/дни недели)
│   │   ├── bidder.py             # Бидер: ИИ-подбор ставок по стратегиям
│   │   ├── recommender.py        # Рекомендатор цен (вилка, маржа, ДРР, вход в рекламу)
│   │   ├── logger.py             # Логирование действий и API-запросов
│   │   └── challenge/            # Решатель JS-challenge Ozon (Node+jsdom, незавершён)
│   │
│   ├── templates/                # Jinja2-шаблоны: base.html, dashboard, campaigns, analyzer…
│   ├── static/                   # CSS, JS, Chart.js
│   └── jobs.py                   # APScheduler: периодические задачи (синхронизация, правила, бидер)
│
├── docs/                         # Документация
│   ├── AGENTS.md                 # Этот файл — карта для AI
│   └── codegraph.html            # Граф зависимостей (D3.js)
│
├── data/                         # SQLite БД, загруженные фото, ключ шифрования
├── tests/                        # pytest-тесты
├── Dockerfile, docker-compose.yml
├── run.py, requirements.txt
└── .env                          # SECRET_KEY, ENCRYPTION_KEY, ключи API
```

---

## Ключевые потоки

### 1. Анализатор цен (главная фича)
```
Пользователь → /analyzer [GET] → форма
  → загружает фото(и) → /analyzer/api [POST]
    → 1. Сохраняет фото на сервер (data/uploads/)
    → 2. Яндекс.Картинки (photo_search.py) — поиск по фото
          → получает ссылки на товары + цены из выдачи
    → 3. Текстовый поиск (market_search.py) — WB, Яндекс.Маркет
          → WB через API, Яндекс.Маркет через HTML-парсинг
    → 4. Bright Data (bright_data.py) — по Ozon-ссылкам из фото → точные цены
    → 5. Анализ (recommender.py) — вилка цен, медиана, рекомендация
    → 6. Сохранение в AnalyzerHistory (история)
    → 7. Ответ: {photo_prices, photo_links, sources, stats, recommendation}
```

### 2. Управление рекламой Ozon
```
Авторизация: OAuth2 client_credentials (client_id + client_secret → Bearer token)
  → Список кампаний, старт/стоп, бюджеты, ставки
  → Статистика (показы, клики, CTR, заказы, ДРР)
  → Товары кампаний, конкурентные ставки
```

### 3. Автоматизация (APScheduler, каждые 8 мин)
```
  → Синхронизация кампаний и статистики из Ozon API
  → Авто-правила: если бюджет > X% → уведомление; 100% → стоп
  → Расписания: кампании старт/стоп по дням/часам
  → Бидер: автоматическая корректировка ставок по стратегиям
  → Синхронизация Seller API (остатки, цены, комиссии)
```

---

## Ключевые модели БД

| Таблица | Назначение |
|---|---|
| `users` | Пользователи (login, пароль PBKDF2) |
| `api_keys` | Ключи Ozon Performance API + Seller API (зашифрованы Fernet) |
| `campaigns` | Кэш кампаний из Ozon |
| `campaign_stats` | Статистика кампаний по дням |
| `products` | Рекламные товары |
| `product_info` | Экономика товаров (остатки, цены, комиссии) |
| `proxy_settings` | Настройки: прокси, куки, Bright Data |
| `automation_rules` | Авто-правила |
| `campaign_schedules` | Расписания |
| `bidder_rules` | Правила бидера |
| `analyzer_history` | История поисков анализатора (фото, товары, цены) |
| `action_log`, `api_log` | Логи |

---

## Зависимости между сервисами

```
pages.py ← analyzer.py ← photo_search.py ← httpx (Яндекс.Картинки)
                        ← market_search.py ← httpx (WB, YM, Ozon)
                        ← bright_data.py ← httpx (Bright Data API)
                        ← recommender.py

api.py ← ozon_client.py ← httpx (Ozon Performance API)
       ← campaigns.py, statistics.py, products.py
       ← seller_client.py ← httpx (Ozon Seller API)

jobs.py ← campaigns.py, statistics.py, products.py
        ← seller_sync.py ← seller_client.py
        ← rules_engine.py, schedule_service.py, bidder.py
```

---

## Важные технические детали

### Ozon Performance API 2.0
- **Аутентификация:** OAuth2 client_credentials (JSON body с `grant_type=client_credentials`), ответ — Bearer token.
- **Бюджеты:** в микрорублях (÷ 1 000 000).
- **Пути:** `/api/client/campaign/…`.

### Яндекс.Картинки (поиск по фото)
- **Условие:** фото должно быть доступно по публичному URL (наш сервер).
- **Формула:** `yandex.ru/images/search?rpt=imageview&url=<URL>&cbir_page=products`.
- **Извлекается:** ссылки на товары (`/product/`), цены (`Цена 262₽`), миниатюры (`avatars.mds.yandex.net`).
- **Ограничение:** ищет только по фото, не по названию.

### Bright Data
- **Датасет:** `gd_lutq85sl13rlndbzai` — Ozon product prices.
- **Формат:** POST `/datasets/v3/scrape` → JSON lines / single dict / snapshot.
- **Нужен:** активный аккаунт (ошибка "Customer is not active" → пополнить счёт).

### Антибот Ozon (не решено)
- JS-challenge: `/abt/result` → `{"ok":false}` для дата-центр IP.
- Все бесплатные сервисы (ScraperAPI, ScrapingAnt, Crawlbase) не проходят.
- Рабочие пути: Bright Data (по URL), Яндекс.Маркет (не Ozon), браузерный плагин (удалён).

---

## Конфигурация (.env)

| Параметр | Назначение |
|---|---|
| `SECRET_KEY` | Сессии FastAPI |
| `ENCRYPTION_KEY` | Шифрование API-ключей (Fernet) |
| `DATABASE_URL` | `sqlite+aiosqlite:///./data/ozon_ads.db` или PostgreSQL |
| `SCHEDULER_INTERVAL_MINUTES` | Интервал APScheduler (мин) |
| `SCRAPINGANT_API_KEY` | ScrapingAnt (не работает для Ozon) |
| `SCRAPERAPI_API_KEY` | ScraperAPI (не работает для Ozon без premium) |
| `OZON_BRIDGE_URL` | (устарело) URL локального сервера на ПК |

---

## Развёртывание

- **nginx:** `searx.dungeonverse.ru` → `proxy_pass 127.0.0.1:8002`. Таймаут 300s для анализатора.
- **Docker:** `docker compose build app && docker compose up -d app`.
- **Миграции:** автоматические (`init_db()` → `_migrate_sqlite()` добавляет колонки).
- **Логи:** `docker logs ozon-ads`.

---

## Для нейросетей: как читать этот проект

1. **Начните с `app/routers/analyzer.py`** — там главный поток анализатора (фото → цены).
2. **`app/services/photo_search.py`** — поиск по фото в Яндексе (ключевой сервис).
3. **`app/services/market_search.py`** — текстовый поиск цен (WB, YM, Ozon).
4. **`app/services/bright_data.py`** — цены Ozon по URL (альтернатива).
5. **`app/routers/pages.py`** — все HTML-страницы и настройки.
6. **`app/services/ozon_client.py`** — интеграция с Ozon Performance API.
7. **`app/jobs.py`** — планировщик, автоматизация.
8. **`app/models.py`** — все модели БД.