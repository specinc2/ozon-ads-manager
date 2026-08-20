"""Тесты анализатора цен и рекомендаций."""
import pytest

from app.services.market_search import analyze_prices
from app.services.recommender import recommend


def test_analyze_prices_buckets():
    """Разбивка цен по вилкам и проценты."""
    prices = [150, 180, 220, 250, 260, 450, 480, 550]
    a = analyze_prices(prices, bucket_size=100.0)

    assert a["total"] == 8
    assert a["median"] == pytest.approx(255.0)  # 4-й и 5-й: 250, 260
    assert a["min"] == 150
    assert a["max"] == 550

    labels = {b.label: b for b in a["buckets"]}
    # 100–200: 150, 180 → 2 шт = 25%
    assert labels["100–200 ₽"].count == 2
    assert labels["100–200 ₽"].percent == 25.0
    # 200–300: 220,250,260 → 3 шт = 37.5%
    assert labels["200–300 ₽"].count == 3
    assert labels["200–300 ₽"].percent == 37.5

    # Рекомендуемая цена — около медианы * 0.95, в населённой вилке
    assert a["recommended_price"] is not None
    assert 200 <= a["recommended_price"] <= 300


def test_analyze_prices_empty():
    a = analyze_prices([])
    assert a["buckets"] == []
    assert a["median"] is None
    assert a["recommended_price"] is None


def test_recommend_enter_ad():
    """При хорошей марже рекомендуем входить в рекламу."""
    r = recommend(
        240.0, cost_price=50.0, commission_pct=20.0,
        logistics_cost=50.0, acquiring_pct=1.5, buyout_pct=80.0, min_margin_pct=10.0,
    )
    assert r.recommended_price == 240.0
    assert r.margin_per_unit > 0
    assert r.margin_pct >= 10.0
    assert r.ad_verdict == "enter"
    assert r.breakeven_drr > 0


def test_recommend_skip_low_margin():
    """При низкой марже рекомендуем не входить в рекламу."""
    r = recommend(
        240.0, cost_price=200.0, commission_pct=20.0,
        logistics_cost=50.0, acquiring_pct=1.5, buyout_pct=80.0, min_margin_pct=10.0,
    )
    assert r.ad_verdict in ("skip", "careful")


def test_recommend_no_price():
    """Без цены рекомендация не строится."""
    r = recommend(None, cost_price=100.0)
    assert r.recommended_price is None
    assert r.ad_verdict == "skip"