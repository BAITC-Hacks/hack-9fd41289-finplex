"""Тесты приёмки по разделу 7 PRD (must-have критерии).

Каждый тест соответствует критерию приёмки из ТЗ. Именно эти проверки
рекомендовано прогнать на демо (PRD 6.1).
"""
import pandas as pd

from app.core.planner import compute_orders, detect_spikes, safety_stock

MONTHS = [f"m_2026-{i:02d}" for i in range(1, 13)]


def _row(code, monthly, free_stock=0, in_transit=0, cost=100, avg=None,
         growth=0.0, name=None):
    """Собрать строку витрины для теста."""
    r = {
        "code": code, "article": code, "name": name or f"Товар {code}",
        "category": "1", "cost": cost,
        "avg_month_12": avg if avg is not None else (sum(monthly) / len(monthly)),
        "growth_coef": growth, "season_coef": 1.0,
        "free_stock": free_stock, "in_transit": in_transit,
    }
    for i, m in enumerate(MONTHS):
        r[m] = monthly[i] if i < len(monthly) else 0
    return r


def _showcase(rows):
    return pd.DataFrame(rows)


# ---- Критерий 7.1: базовая потребность, реагирует на все источники ----
def test_p71_reacts_to_in_transit():
    monthly = [100] * 12
    base = _showcase([_row("A1", monthly, free_stock=50, in_transit=0)])
    more = _showcase([_row("A1", monthly, free_stock=50, in_transit=500)])
    r1 = compute_orders(base, None, {}, "S")
    r2 = compute_orders(more, None, {}, "S")
    q1 = r1.lines[0].recommended_qty if r1.lines else 0
    q2 = r2.lines[0].recommended_qty if r2.lines else 0
    # больше товара в пути -> меньше (или ноль) рекомендованный заказ
    assert q2 < q1 or q2 == 0


def test_p71_reacts_to_free_stock():
    monthly = [100] * 12
    low = _showcase([_row("A1", monthly, free_stock=0)])
    high = _showcase([_row("A1", monthly, free_stock=1000)])
    q_low = compute_orders(low, None, {}, "S").lines
    q_high = compute_orders(high, None, {}, "S").lines
    # большой остаток -> заказ не нужен
    assert (q_low[0].recommended_qty if q_low else 0) > (q_high[0].recommended_qty if q_high else 0)


# ---- Критерий 7.3: компенсация stockout повышает потребность ----
def test_p73_stockout_increases_need():
    # ряд с двумя нулевыми месяцами внутри активного периода (stockout)
    with_stockout = [100, 100, 0, 0, 100, 100, 100, 100, 100, 100, 100, 100]
    r_so = compute_orders(_showcase([_row("A1", with_stockout, free_stock=0)]), None, {}, "S")
    # у товара со stockout база занижена нулями, но компенсация должна поднять потребность
    so_line = next((ln for ln in r_so.lines if ln.code == "A1"), None)
    assert so_line is not None
    assert so_line.stockout_adj > 0  # компенсация применена


# ---- Критерий 7.4: разовый крупный заказ НЕ раздувает рекомендацию ----
def test_p74_spike_excluded():
    normal = [100, 110, 95, 105, 100, 98, 102, 100, 100, 105, 95, 100]
    spiked = normal.copy()
    spiked[5] = 5000  # искусственный разовый крупный заказ
    r_norm = compute_orders(_showcase([_row("A1", normal, free_stock=0)]), None, {}, "S")
    r_spk = compute_orders(_showcase([_row("A2", spiked, free_stock=0)]), None, {}, "S")
    q_norm = r_norm.lines[0].recommended_qty
    q_spk = r_spk.lines[0].recommended_qty
    # выброс не должен существенно (>50%) раздуть рекомендацию
    assert q_spk < q_norm * 1.5


def test_detect_spikes_iqr():
    # ряд с явным выбросом
    vals = [100, 110, 95, 105, 100, 98, 102, 5000]
    spike_sum, cnt = detect_spikes(vals)
    assert cnt == 1 and spike_sum == 5000
    # ровный ряд — без выбросов
    assert detect_spikes([100, 101, 99, 100, 102, 98])[1] == 0


def test_safety_stock_formula():
    # стабильный спрос -> малый страховой запас; волатильный -> больше
    stable = safety_stock([100, 100, 100, 100], lead_time=1.5)
    volatile = safety_stock([20, 180, 40, 160, 60, 140], lead_time=1.5)
    assert volatile > stable


# ---- Критерий 7.5: обоснование по каждой позиции + группировка ----
def test_p75_every_line_has_reason():
    monthly = [100] * 12
    r = compute_orders(_showcase([_row("A1", monthly, free_stock=0),
                                  _row("A2", monthly, free_stock=0)]), None, {}, "S")
    assert r.lines
    for line in r.lines:
        assert line.reason and len(line.reason) > 10
        assert line.reason_tag


def test_deterministic():
    monthly = [100, 120, 90, 110, 100, 95, 105, 100, 100, 110, 90, 100]
    sc = _showcase([_row("A1", monthly, free_stock=50)])
    r1 = compute_orders(sc, None, {}, "S")
    r2 = compute_orders(sc, None, {}, "S")
    assert r1.as_dict() == r2.as_dict()


def test_moq_rounding():
    monthly = [100] * 12
    r = compute_orders(_showcase([_row("A1", monthly, free_stock=0)]), None, {"A1": 50}, "S")
    if r.lines:
        # заказ кратен MOQ=50
        assert r.lines[0].recommended_qty % 50 == 0
