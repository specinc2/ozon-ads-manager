"""Тесты экономики товаров и бидера."""
import asyncio

import pytest

from app.services.economics import ProductEconomics, calculate, from_info, recommend_bid
from app.models import ProductInfo


def test_calculate_basic_margin():
    """Товар с ценой 1000, себестоимостью 300, комиссией 15%, логистикой 50, эквайрингом 1.5%, выкупом 80%."""
    e = ProductEconomics(
        sku="100", price=1000.0, cost_price=300.0,
        commission_pct=15.0, logistics_cost=50.0, acquiring_pct=1.5, buyout_pct=80.0,
        monthly_revenue=50000.0,
    )
    calculate(e)

    # Цена с учётом скидки (без акции)
    assert e.effective_price == 1000.0
    # Комиссия 15% = 150
    assert e.commission == 150.0
    # Эквайринг 1.5% = 15
    assert e.acquiring == 15.0
    # Все расходы = 150 + 15 + 50 + 300 = 515
    assert e.total_costs == 515.0
    # Маржа на проданную единицу = 1000 - 515 = 485
    assert e.margin_per_unit == 485.0
    # Маржинальность = 485/1000 = 48.5%
    assert e.margin_pct == pytest.approx(48.5)
    # Маржа с выкупом: 0.8*1000 - 515 = 285
    assert e.margin_per_ordered == pytest.approx(285.0)
    assert e.margin_pct_of_ordered == pytest.approx(28.5)


def test_calculate_with_promotion():
    """Участие в акции со скидкой 20%."""
    e = ProductEconomics(
        sku="101", price=1000.0, cost_price=300.0,
        commission_pct=15.0, logistics_cost=50.0, acquiring_pct=1.5, buyout_pct=80.0,
        in_promotion=True, promotion_discount_pct=20.0,
    )
    calculate(e)
    # Цена со скидкой: 1000 * 0.8 = 800
    assert e.effective_price == 800.0
    # Комиссия от 800 = 120
    assert e.commission == 120.0
    # Эквайринг от 800 = 12
    assert e.acquiring == 12.0
    # Все расходы = 120 + 12 + 50 + 300 = 482
    assert e.total_costs == 482.0
    # Маржа с выкупом = 0.8*800 - 482 = 158
    assert e.margin_per_ordered == pytest.approx(158.0)


def test_breakeven_spend():
    """Лимит рекламы на 1 продажу при минимальной марже 10%."""
    e = ProductEconomics(
        sku="102", price=1000.0, cost_price=300.0,
        commission_pct=15.0, logistics_cost=50.0, acquiring_pct=1.5, buyout_pct=80.0,
        min_margin_pct=10.0,
    )
    calculate(e)
    # Минимальная маржа в рублях = 1000 * 10% = 100
    # Маржа с выкупом = 285
    # Лимит = 285 - 100 = 185
    assert e.breakeven_spend_per_unit == pytest.approx(185.0)


def test_drr_of_total():
    """ДРР от всего оборота."""
    e = ProductEconomics(
        sku="103", price=1000.0, cost_price=300.0, commission_pct=15.0,
        logistics_cost=50.0, acquiring_pct=1.5, buyout_pct=80.0,
        monthly_revenue=50000.0,  # весь оборот
        ad_spend=2500.0,
    )
    calculate(e)
    # ДРР = 2500 / 50000 * 100 = 5%
    assert e.drr_of_total == pytest.approx(5.0)


def test_low_traffic_flag():
    e = ProductEconomics(sku="104", price=100.0)
    # По умолчанию low_traffic = False
    assert e.low_traffic is False


def test_calculate_from_info():
    """Создание экономики из ProductInfo."""
    info = ProductInfo(
        sku="200", name="Тестовый товар", price=500.0, cost_price=200.0,
        commission_pct=10.0, logistics_cost=30.0, acquiring_pct=1.0, buyout_pct=85.0,
    )
    e = from_info(info)
    calculate(e)
    assert e.effective_price == 500.0
    assert e.margin_per_ordered == pytest.approx(0.85 * 500 - (0.1 * 500 + 0.01 * 500 + 30 + 200))


def test_from_info_none():
    """Пустая экономика если info = None."""
    e = from_info(None, sku="300", name="Новый товар")
    assert e.sku == "300"
    assert e.name == "Новый товар"
    assert e.price == 0.0


def test_recommend_bid():
    """Рекомендация ставки на основе маржи и ДРР."""
    # Прибыльный товар, ДРР низкий — ставку можно поднять
    e = ProductEconomics(sku="400", price=1000.0, cost_price=300.0,
                         commission_pct=15.0, logistics_cost=50.0, acquiring_pct=1.5, buyout_pct=80.0,
                         monthly_revenue=50000.0, ad_spend=2000.0, min_margin_pct=10.0)
    calculate(e)
    assert recommend_bid(e) == "raise"

    # Высокая ДРР — снижаем
    e2 = ProductEconomics(sku="401", price=1000.0, cost_price=300.0,
                          commission_pct=15.0, logistics_cost=50.0, acquiring_pct=1.5, buyout_pct=80.0,
                          monthly_revenue=50000.0, ad_spend=25000.0, min_margin_pct=10.0)
    calculate(e2)
    assert recommend_bid(e2) == "lower"


def test_bidder_engine_imports():
    """Проверяем, что движок бидера импортируется без ошибок."""
    from app.services.bidder import BidderEngine
    assert BidderEngine is not None