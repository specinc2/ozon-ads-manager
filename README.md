# Ozon Ads Manager

Веб-приложение для автоматизации управления рекламными кампаниями Ozon через **Performance API 2.0** и анализа цен конкурентов (Wildberries, Яндекс.Маркет, Ozon через Bright Data).

## Возможности

### Управление рекламой
- Подключение API-ключей Ozon Performance API 2.0 (OAuth2 client_credentials)
- Список кампаний с фильтрами, старт/стоп, бюджеты, ставки
- Статистика с графиками Chart.js, экспорт CSV
- Товары кампаний, массовое обновление ставок, конкурентные ставки
- **Бидер** — 3 стратегии: целевая ДРР, поддержание позиции, ИИ-подбор

### Автоматизация (APScheduler, каждые 8 мин)
- Синхронизация кампаний и статистики из Ozon API
- Авто-правила: уведомление/остановка при превышении бюджета
- Расписания: старт/стоп по дням недели и часам
- Авто-ставки по метрикам (CTR, конверсия, CPA)
- Синхронизация экономики товаров через Seller API

### Анализатор цен
- **Загрузка нескольких фото** → поиск по фото в Яндекс.Картинках → цены похожих товаров из выдачи
- **Текстовый поиск** — Wildberries (API) + Яндекс.Маркет (HTML/JSON)
- **Ozon** — точные цены по URL карточек через Bright Data
- **Рекомендация:** цена старта, маржа, ДРР, вход в рекламу
- **История анализатора** — все запросы с фото, ссылками и ценами
- **Страна поставки** — через плагин браузера (устарело, удалён)

## Стек

Python 3.11 · FastAPI (async) · SQLAlchemy 2.0 (async) · SQLite/PostgreSQL ·
Jinja2 + Bootstrap 5 + Chart.js · APScheduler · httpx · cryptography (Fernet).

## Быстрый старт

```bash
cp .env.example .env
# Отредактируйте SECRET_KEY, ENCRYPTION_KEY, ключи Ozon
pip install -r requirements.txt
python run.py
# Откройте http://127.0.0.1:8000
```

## Docker

```bash
docker compose build app
docker compose up -d app
```

## Документация для разработчиков

- **[docs/AGENTS.md](docs/AGENTS.md)** — карта проекта для AI-ассистентов: архитектура, потоки, модели, зависимости
- **[docs/codegraph.html](docs/codegraph.html)** — граф зависимостей кода (D3.js, открыть в браузере)

## Ключевые настройки (.env)

| Переменная | Описание | По умолчанию |
|---|---|---|
| `SECRET_KEY` | Ключ подписи сессий | `dev-secret-key-change-me` |
| `ENCRYPTION_KEY` | Fernet-ключ шифрования API-ключей | авто-генерация |
| `DATABASE_URL` | SQLite или PostgreSQL | `sqlite+aiosqlite:///./data/ozon_ads.db` |
| `SCHEDULER_INTERVAL_MINUTES` | Интервал планировщика | `8` |
| `SCRAPINGANT_API_KEY` | ScrapingAnt (не работает для Ozon) | — |
| `SCRAPERAPI_API_KEY` | ScraperAPI (не работает для Ozon без premium) | — |

## Настройка после запуска

1. Зарегистрируйтесь — первый пользователь = admin.
2. **Настройки → Ключи API** — Client-Id + Client-Secret Ozon Performance API.
3. **Настройки → Bright Data** — API-ключ + Dataset ID (для точных цен Ozon).
4. **Настройки → Ключи Seller API** — Client-Id + Api-Key для синхронизации экономики товаров.

## Тесты

```bash
python -m pytest tests/ -v
```

## Структура проекта

```
app/
├── main.py              # FastAPI, middleware, запуск планировщика
├── config.py            # Настройки из .env
├── database.py          # Engine + session + миграции колонок
├── models.py            # ORM-модели
├── security.py          # Пароли (PBKDF2), шифрование (Fernet), сессии
├── jobs.py              # APScheduler: синхронизация, правила, бидер
├── routers/
│   ├── pages.py         # HTML-страницы (дашборд, кампании, настройки, анализатор…)
│   ├── api.py           # JSON API для AJAX-действий
│   └── analyzer.py      # Анализатор цен: страница + API + история
├── services/
│   ├── ozon_client.py   # Ozon Performance API 2.0 (OAuth2)
│   ├── market_search.py # Поиск цен: WB, Ozon, Яндекс.Маркет, AliExpress
│   ├── photo_search.py  # Поиск по фото в Яндекс.Картинках → ссылки + цены
│   ├── bright_data.py   # Bright Data API: цены Ozon по URL товара
│   ├── seller_client.py # Ozon Seller API (официальный)
│   ├── seller_sync.py   # Синхронизация экономики товаров
│   ├── campaigns.py     # CRUD кампаний в кэше БД
│   ├── statistics.py    # Агрегация статистики
│   ├── products.py      # Товары и ставки
│   ├── economics.py     # Экономика: себестоимость, комиссии
│   ├── rules_engine.py  # Авто-правила
│   ├── schedule_service.py # Расписания
│   ├── bidder.py        # Бидер: подбор ставок
│   ├── recommender.py   # Рекомендатор цен
│   └── logger.py        # Логирование
└── templates/           # Jinja2
```

## Деплой на VDS

1. Установите Docker и `docker compose`.
2. Скопируйте проект, настройте `.env`.
3. `docker compose build app && docker compose up -d app`.
4. Настройте nginx reverse-proxy с HTTPS.
5. Увеличьте `proxy_read_timeout` до 300s для анализатора.