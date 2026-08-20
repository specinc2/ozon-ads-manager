"""Тесты чистой логики движка авто-правил (без БД и API)."""
import pytest

from app.models import Product
from app.services.rules_engine import RuleEngine


# _compare — проверка условий
def test_compare_operators():
    engine = RuleEngine.__new__(RuleEngine)  # без инициализации, тестируем только staticmethod
    assert engine._compare(0.5, "<", 1.0) is True
    assert engine._compare(1.5, ">", 1.0) is True
    assert engine._compare(1.0, "<=", 1.0) is True
    assert engine._compare(2.0, ">=", 1.0) is True
    assert engine._compare(5.0, "==", 5.0) is True
    assert engine._compare(2.0, "<", 1.0) is False
    assert engine._compare(0.5, ">", 1.0) is False


# _new_bid — расчёт новой ставки
def test_new_bid_calculations():
    engine = RuleEngine.__new__(RuleEngine)
    # Уменьшение на 10%
    assert engine._new_bid(100.0, "decrease_by_percent", 10) == pytest.approx(90.0)
    # Увеличение на 10%
    assert engine._new_bid(100.0, "increase_by_percent", 10) == pytest.approx(110.0)
    # Уменьшение на фиксированную сумму
    assert engine._new_bid(100.0, "decrease_by_amount", 15) == pytest.approx(85.0)
    # Увеличение на фиксированную сумму
    assert engine._new_bid(100.0, "increase_by_amount", 15) == pytest.approx(115.0)


# _metric_value — извлечение метрик из товара
def test_metric_value():
    engine = RuleEngine.__new__(RuleEngine)
    product = Product(impressions=1000, clicks=50, orders=5, spend=200.0)
    # CTR = 50/1000 * 100 = 5%
    assert engine._metric_value(product, "ctr") == 5.0
    # Конверсия = 5/50 * 100 = 10%
    assert engine._metric_value(product, "conversion") == 10.0
    # CPA = 200/5 = 40
    assert engine._metric_value(product, "cpa") == 40.0
    assert engine._metric_value(product, "orders") == 5.0
    assert engine._metric_value(product, "impressions") == 1000.0
    # Нулевые клики — конверсия 0, CPA отсутствует
    no_clicks = Product(impressions=100, clicks=0, orders=0, spend=0.0)
    assert engine._metric_value(no_clicks, "conversion") == 0.0
    assert engine._metric_value(no_clicks, "cpa") is None
