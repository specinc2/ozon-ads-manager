"""Тесты: шифрование, пароли, нормализация ответов, Ozon-клиент на mock-транспорте."""
import asyncio
import json

import httpx
import pytest

from app.security import decrypt_value, encrypt_value, hash_password, verify_password
from app.services.ozon_client import (
    OzonAuthError,
    OzonClient,
    normalize_campaign,
    normalize_daily_stat_row,
    rubles_from_micro,
    parse_decimal,
)


# ---------------------------------------------------------------------------
# Шифрование и пароли
# ---------------------------------------------------------------------------

def test_password_roundtrip():
    h = hash_password("secret123")
    assert h != "secret123"
    assert verify_password("secret123", h)
    assert not verify_password("wrong", h)
    assert not verify_password("secret123", "garbage-string")


def test_encrypt_decrypt_roundtrip():
    token = encrypt_value("my-client-secret")
    assert token != "my-client-secret"
    assert decrypt_value(token) == "my-client-secret"
    assert decrypt_value("") == ""
    assert decrypt_value("broken-token") == ""


# ---------------------------------------------------------------------------
# Денежные преобразования (микрорубли)
# ---------------------------------------------------------------------------

def test_rubles_from_micro():
    assert rubles_from_micro("1000000") == 1.0
    assert rubles_from_micro("2000000000") == 2000.0
    assert rubles_from_micro(500000) == 0.5
    assert rubles_from_micro(None) is None
    assert rubles_from_micro("") is None
    assert rubles_from_micro("0") == 0.0


def test_parse_decimal_comma():
    assert parse_decimal("3,31") == 3.31
    assert parse_decimal("0,00") == 0.0
    assert parse_decimal(123) == 123.0
    assert parse_decimal("1 234,5") == 1234.5
    assert parse_decimal(None) == 0.0


# ---------------------------------------------------------------------------
# Нормализация ответов Ozon
# ---------------------------------------------------------------------------

def test_normalize_campaign():
    raw = {
        "id": "12345",
        "title": "Моя кампания",
        "state": "CAMPAIGN_STATE_RUNNING",
        "advObjectType": "SKU",
        "PaymentType": "CPC",
        "dailyBudget": "500000000",
        "weeklyBudget": "2000000000",
        "budget": "10000000000",
        "fromDate": "2026-07-16",
        "toDate": "2026-08-16",
    }
    norm = normalize_campaign(raw)
    assert norm["campaign_id"] == "12345"
    assert norm["title"] == "Моя кампания"
    assert norm["status"] == "RUNNING"
    assert norm["campaign_type"] == "Оплата за клик"
    assert norm["daily_budget"] == 500.0
    assert norm["weekly_budget"] == 2000.0
    assert norm["total_budget"] == 10000.0
    assert str(norm["start_date"]) == "2026-07-16"


def test_normalize_daily_stat_row():
    raw = {
        "id": "32547986",
        "title": "jack",
        "date": "2026-08-02",
        "views": "156",
        "clicks": "6",
        "moneySpent": "6,35",
        "orders": "1",
        "ordersMoney": "300,00",
    }
    norm = normalize_daily_stat_row(raw)
    assert norm["campaign_id"] == "32547986"
    assert norm["impressions"] == 156
    assert norm["clicks"] == 6
    assert norm["ctr"] == pytest.approx(3.85)
    assert norm["orders"] == 1
    assert norm["spend"] == pytest.approx(6.35)
    assert norm["revenue"] == pytest.approx(300.0)
    assert norm["cpa"] == pytest.approx(6.35)


# ---------------------------------------------------------------------------
# Ozon-клиент на mock-транспорте
# ---------------------------------------------------------------------------

def run_sync(coro):
    return asyncio.run(coro)


def make_client(handler) -> OzonClient:
    transport = httpx.MockTransport(handler)
    return OzonClient("client-id", "client-secret", transport=transport)


def test_token_and_list_campaigns():
    """Получение токена (OAuth2) и списка кампаний."""
    seen_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_paths.append(request.url.path)
        if request.url.path == "/api/client/token":
            return httpx.Response(200, json={"access_token": "test-token", "expires_in": 1800, "token_type": "Bearer"})
        if request.url.path == "/api/client/campaign":
            return httpx.Response(200, json={"list": [
                {"id": "1", "title": "Кампания А", "state": "CAMPAIGN_STATE_RUNNING", "advObjectType": "SKU"},
            ]})
        return httpx.Response(404, json={"error": "not found"})

    client = make_client(handler)
    campaigns = run_sync(client.list_campaigns())

    assert seen_paths == ["/api/client/token", "/api/client/campaign"]
    assert len(campaigns) == 1
    assert campaigns[0]["title"] == "Кампания А"


def test_list_campaigns_pagination():
    """Пагинация списка кампаний: повторяет запросы, пока страница не полная."""
    pages = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/client/token":
            return httpx.Response(200, json={"access_token": "test-token", "expires_in": 1800})
        if request.url.path == "/api/client/campaign":
            pages["n"] += 1
            page = int(request.url.params.get("page", 1))
            # страницы 1 и 2 — полные (по 100), третья — неполная (10)
            if page <= 2:
                items = [{"id": str(i), "title": f"C{i}", "state": "CAMPAIGN_STATE_RUNNING"} for i in range(100)]
            else:
                items = [{"id": str(i), "title": f"C{i}", "state": "CAMPAIGN_STATE_RUNNING"} for i in range(10)]
            return httpx.Response(200, json={"list": items})
        return httpx.Response(404, json={"error": "not found"})

    client = make_client(handler)
    campaigns = run_sync(client.list_campaigns())

    assert len(campaigns) == 210
    assert pages["n"] == 3


def test_retry_on_429():
    """Ретрай при 429 и успех на второй попытке."""
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/client/token":
            return httpx.Response(200, json={"access_token": "test-token", "expires_in": 1800})
        attempts["n"] += 1
        if attempts["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0"}, json={})
        return httpx.Response(200, json={"list": []})

    client = make_client(handler)
    result = run_sync(client.list_campaigns())

    assert result == []
    assert attempts["n"] == 2


def test_401_raises_auth_error():
    """401 → OzonAuthError с понятным сообщением."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "unauthorized"})

    client = make_client(handler)
    with pytest.raises(OzonAuthError):
        run_sync(client.list_campaigns())


def test_5xx_raises_after_retries():
    """5xx → ошибка API после исчерпания ретраев."""
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/client/token":
            return httpx.Response(200, json={"access_token": "test-token"})
        attempts["n"] += 1
        return httpx.Response(500, json={"error": "internal"})

    client = make_client(handler)
    from app.services.ozon_client import OzonAPIError
    with pytest.raises(OzonAPIError, match="Ozon вернул 500"):
        run_sync(client.list_campaigns())
    assert attempts["n"] > 1  # были ретраи


def test_update_bids_builds_body():
    """PUT /api/client/campaign/{id}/products — тело содержит bids."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/client/token":
            return httpx.Response(200, json={"access_token": "test-token"})
        if request.url.path == "/api/client/campaign/42/products" and request.method == "PUT":
            captured["body"] = json.loads(request.content)
            return httpx.Response(200, json=None)
        return httpx.Response(404, json={"error": "not found"})

    client = make_client(handler)
    run_sync(client.update_products_bids(42, [{"sku": "100", "bid": 55.5}]))

    assert captured["body"] == {"bids": [{"sku": "100", "bid": "55.5"}]}


def test_daily_statistics_query():
    """GET /api/client/statistics/daily/json — параметры дат и кампаний."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/client/token":
            return httpx.Response(200, json={"access_token": "test-token"})
        if request.url.path == "/api/client/statistics/daily/json":
            captured["params"] = list(request.url.params.multi_items())
            return httpx.Response(200, json={"rows": [
                {"id": "1", "date": "2026-08-01", "views": "10", "clicks": "2", "moneySpent": "3,31", "orders": "0", "ordersMoney": "0,00"},
            ]})
        return httpx.Response(404, json={"error": "not found"})

    client = make_client(handler)
    rows = run_sync(client.get_daily_statistics(["1", "2"], "2026-08-01", "2026-08-10"))

    params = dict(captured["params"])
    assert params.get("dateFrom") == "2026-08-01"
    assert params.get("dateTo") == "2026-08-10"
    # campaignIds передаются повторяющимися параметрами: ?campaignIds=1&campaignIds=2
    campaign_ids = [v for k, v in captured["params"] if k == "campaignIds"]
    assert campaign_ids == ["1", "2"]
    assert len(rows) == 1


def test_campaign_summary_report():
    """GET /api/client/statistics/campaign/product/json — сводный отчёт с ДРР и корзиной."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/client/token":
            return httpx.Response(200, json={"access_token": "test-token"})
        if request.url.path == "/api/client/statistics/campaign/product/json":
            captured["params"] = list(request.url.params.multi_items())
            return httpx.Response(200, json={"rows": [
                {"id": "32547986", "title": "jack", "objectType": "SKU", "status": "running",
                 "moneySpent": "140,50", "views": "3479", "clicks": "100", "ctr": "0,03",
                 "clickPrice": "1,41", "orders": "15", "ordersMoney": "3380,00",
                 "drr": "4,2", "toCart": "18", "weeklyBudget": "2000,00"},
            ]})
        return httpx.Response(404, json={"error": "not found"})

    client = make_client(handler)
    rows = run_sync(client.get_campaign_summary_report("2026-08-01", "2026-08-20", ["32547986"]))

    assert len(rows) == 1
    row = rows[0]
    assert row["drr"] == "4,2"
    assert row["toCart"] == "18"
    params = dict(captured["params"])
    assert params.get("dateFrom") == "2026-08-01"
    assert params.get("dateTo") == "2026-08-20"
    assert params.get("campaignIds") == "32547986"
