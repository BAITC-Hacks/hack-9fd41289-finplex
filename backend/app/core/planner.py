"""Движок расчёта заказов поставщикам (ядро кейса Электрокомплект).

Закрывает 5 must-have ТЗ:
  1. Базовая потребность (спрос, остаток, в пути, категория, рост)
  2. Сезонность
  3. Компенсация упущенного спроса при stockout
  4. Исключение разовых крупных заказов (детект выбросов) ← ключевая фишка
  5. Итоговый список по поставщикам + обоснование по каждой позиции

Всё детерминированно и объяснимо (требование ТЗ). LLM только формулирует
итоговое резюме, числа берутся из расчёта.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

# Параметры расчёта (настраиваемы; дефолты разумны для дистрибуции электрики)
DEFAULT_LEAD_TIME_MONTHS = 1.5   # срок поставки в месяцах (цель запаса)
DEFAULT_SAFETY_MONTHS = 0.5      # страховой запас в месяцах
SPIKE_Z = 2.5                    # порог z-score для разового всплеска


@dataclass
class OrderLine:
    code: str
    name: str
    supplier: str
    recommended_qty: float
    urgency: str
    reason: str
    reason_tag: str = ""       # короткий тег обоснования для колонки таблицы
    # диагностика (для прозрачности/защиты)
    base_demand: float = 0.0
    seasonal_demand: float = 0.0
    free_stock: float = 0.0
    in_transit: float = 0.0
    target_stock: float = 0.0
    spike_removed: float = 0.0
    spike_count: int = 0
    stockout_adj: float = 0.0
    moq: float = 1.0
    cost: float = 0.0          # себестоимость единицы (для суммы заказа в тенге)
    coverage_months: float = 0.0
    monthly: list[float] = field(default_factory=list)  # ряд продаж для графика

    def order_cost(self) -> float:
        return self.recommended_qty * self.cost

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code, "name": self.name, "supplier": self.supplier,
            "recommended_qty": round(self.recommended_qty, 1), "urgency": self.urgency,
            "reason": self.reason, "reason_tag": self.reason_tag,
            "order_cost": round(self.order_cost()),
            "monthly": [round(m, 1) for m in self.monthly],
            "detail": {
                "base_demand_month": round(self.base_demand, 2),
                "seasonal_demand_month": round(self.seasonal_demand, 2),
                "free_stock": round(self.free_stock, 1),
                "in_transit": round(self.in_transit, 1),
                "target_stock": round(self.target_stock, 1),
                "spike_removed_units": round(self.spike_removed, 1),
                "spike_count": self.spike_count,
                "stockout_adj_units": round(self.stockout_adj, 1),
                "coverage_months": round(self.coverage_months, 2),
                "moq": self.moq,
            },
        }


@dataclass
class PlanResult:
    supplier: str
    lines: list[OrderLine] = field(default_factory=list)
    summary: str = ""
    excess_count: int = 0            # позиций с избыточным запасом
    spike_items: int = 0             # позиций, где исключены разовые заказы
    spike_units_total: float = 0.0   # сколько единиц-выбросов отфильтровано

    def as_dict(self) -> dict[str, Any]:
        deficit = sum(1 for l in self.lines if l.urgency == "Высокая")
        return {
            "supplier": self.supplier,
            "orders_count": len(self.lines),
            "deficit_count": deficit,
            "excess_count": self.excess_count,
            "total_units": round(sum(l.recommended_qty for l in self.lines), 1),
            "total_cost": round(sum(l.order_cost() for l in self.lines)),
            "spike_items": self.spike_items,
            "spike_units_total": round(self.spike_units_total, 1),
            "summary": self.summary,
            "lines": [l.as_dict() for l in self.lines],
        }


def detect_spikes(qtys: list[float], z: float = SPIKE_Z) -> tuple[float, int]:
    """Найти разовые крупные продажи (выбросы) в списке продаж по месяцам/сделкам.
    Возвращает (сумма_выбросов, количество). Выбросы исключаются из регулярного спроса.
    """
    clean = [q for q in qtys if q > 0]
    if len(clean) < 4:
        return 0.0, 0
    mean = statistics.mean(clean)
    stdev = statistics.pstdev(clean)
    if stdev == 0:
        return 0.0, 0
    spike_sum = 0.0
    spike_cnt = 0
    for q in clean:
        if (q - mean) / stdev >= z:
            spike_sum += q
            spike_cnt += 1
    return spike_sum, spike_cnt


def _monthly_sales(row: pd.Series) -> list[float]:
    """Помесячные продажи из витрины (колонки m_...)."""
    return [float(row[c]) for c in row.index if str(c).startswith("m_")]


def compute_orders(
    showcase: pd.DataFrame,
    history: pd.DataFrame | None,
    moq: dict[str, float],
    supplier: str,
    lead_time: float = DEFAULT_LEAD_TIME_MONTHS,
    safety: float = DEFAULT_SAFETY_MONTHS,
) -> PlanResult:
    """Основной расчёт: по каждому товару считает рекомендуемый заказ."""
    # История продаж по коду (для детекта всплесков на уровне сделок)
    hist_by_code: dict[str, list[float]] = {}
    if history is not None and len(history):
        for code, grp in history.groupby("code"):
            hist_by_code[code] = list(grp["qty"].values)

    lines: list[OrderLine] = []
    excess_count = 0
    spike_items = 0
    spike_units_total = 0.0

    for _, row in showcase.iterrows():
        code = row["code"]
        monthly = _monthly_sales(row)
        months_active = [m for m in monthly if m > 0]

        # --- must-have 4: исключение разовых всплесков (детект выбросов) ---
        # По сделкам (точнее) если есть история, иначе по месячным продажам.
        deal_qtys = hist_by_code.get(code, [])
        spike_sum, spike_cnt = detect_spikes(deal_qtys if deal_qtys else monthly)

        # --- must-have 1: базовый месячный спрос ---
        # За основу берём готовый "Ср мес за 12 мес" партнёра (надёжный ориентир),
        # но если детектировали всплеск — пересчитываем среднее БЕЗ выбросов.
        partner_avg = float(row.get("avg_month_12", 0.0))
        if months_active:
            recent = monthly[-12:] if len(monthly) >= 12 else monthly
            recent_active = [m for m in recent if m > 0]
            if recent_active and len(recent_active) >= 4:
                mean = statistics.mean(recent_active)
                std = statistics.pstdev(recent_active) or 1
                clean = [m for m in recent_active if (m - mean) / std < SPIKE_Z]
                own_avg = sum(clean) / len(clean) if clean else mean
            else:
                own_avg = statistics.mean(recent_active) if recent_active else 0.0
            # берём более консервативную из двух оценок, чтобы не перезаказать
            base_demand = own_avg if own_avg > 0 else partner_avg
        else:
            base_demand = partner_avg

        # --- must-have 2: сезонность ---
        # Кэф. Сез-ти партнёра — относительный (может быть <0), НЕ множитель.
        # Считаем сезонный множитель сами: спрос последних 3 мес / средний по году.
        season_mult = 1.0
        if len(monthly) >= 6 and base_demand > 0:
            last3 = [m for m in monthly[-3:] if m >= 0]
            if last3:
                recent_mean = statistics.mean(last3)
                season_mult = max(0.6, min(recent_mean / base_demand if base_demand else 1.0, 1.8))

        # --- рост: Кэф. Роста партнёра как доля (клиппинг ±40%) ---
        growth_raw = float(row.get("growth_coef", 0.0))
        growth_mult = 1.0 + max(-0.4, min(growth_raw, 0.4)) if -3 < growth_raw < 3 else 1.0

        seasonal_demand = base_demand * season_mult * growth_mult

        # --- must-have 3: компенсация упущенного спроса (stockout) ---
        # Месяцы с 0 продаж среди активного периода — вероятный дефицит, поднимаем спрос.
        stockout_adj = 0.0
        if months_active:
            first = next((i for i, m in enumerate(monthly) if m > 0), 0)
            active_span = monthly[first:]
            zero_in_span = sum(1 for m in active_span if m == 0)
            if zero_in_span >= 2 and base_demand > 0:
                stockout_adj = base_demand * 0.10 * min(zero_in_span, 4)
                seasonal_demand += stockout_adj

        # --- must-have 1: целевой запас и итоговый заказ ---
        target_stock = seasonal_demand * (lead_time + safety)
        free_stock = float(row.get("free_stock", 0.0))
        in_transit = float(row.get("in_transit", 0.0))
        need = target_stock - free_stock - in_transit
        season = season_mult
        growth = growth_mult
        cost = float(row.get("cost", 0.0))
        coverage_months = (free_stock + in_transit) / seasonal_demand if seasonal_demand > 0 else 99.0

        # --- избыточный запас: остатка хватает надолго (> 3× срок поставки) ---
        if need <= 0:
            if seasonal_demand > 0 and coverage_months > (lead_time + safety) * 3:
                excess_count += 1
            if spike_cnt:
                spike_items += 1
                spike_units_total += spike_sum
            continue  # заказ не нужен

        item_moq = moq.get(code, 1.0)
        qty = item_moq * (int((need - 1e-9) / item_moq) + 1) if item_moq > 0 else need

        # срочность по покрытию остатком
        if coverage_months < lead_time:
            urgency = "Высокая"
        elif coverage_months < lead_time + safety:
            urgency = "Средняя"
        else:
            urgency = "Плановая"

        # --- must-have 5: обоснование + короткий тег для колонки таблицы ---
        tags = []
        if stockout_adj:
            tags.append("компенсация stockout")
        if season > 1.15:
            tags.append("сезонный рост")
        elif season < 0.85:
            tags.append("сезонный спад")
        if spike_cnt:
            tags.append("исключён разовый всплеск")
        if in_transit > 0:
            tags.append("товар в пути учтён")
        if coverage_months < lead_time:
            tags.append("остаток ниже точки заказа")
        reason_tag = ", ".join(tags[:2]) if tags else "плановое пополнение"

        reason_parts = [
            f"спрос ~{seasonal_demand:.0f} шт/мес (база {base_demand:.0f}, сезон ×{season:.2f}, рост ×{growth:.2f})",
            f"цель на {lead_time + safety:.1f} мес = {target_stock:.0f}",
            f"минус свободный {free_stock:.0f} и в пути {in_transit:.0f}",
        ]
        if spike_cnt:
            reason_parts.append(f"исключено {spike_cnt} разов. всплеск(ов) {spike_sum:.0f} шт")
        if stockout_adj:
            reason_parts.append(f"учтён дефицит (+{stockout_adj:.0f})")
        if item_moq > 1:
            reason_parts.append(f"округление до MOQ {item_moq:.0f}")
        reason = "; ".join(reason_parts) + "."

        if spike_cnt:
            spike_items += 1
            spike_units_total += spike_sum

        lines.append(OrderLine(
            code=code, name=row["name"], supplier=supplier,
            recommended_qty=qty, urgency=urgency, reason=reason, reason_tag=reason_tag,
            base_demand=base_demand, seasonal_demand=seasonal_demand,
            free_stock=free_stock, in_transit=in_transit, target_stock=target_stock,
            spike_removed=spike_sum, spike_count=spike_cnt, stockout_adj=stockout_adj,
            moq=item_moq, cost=cost, coverage_months=coverage_months, monthly=monthly,
        ))

    # сортировка: сначала срочные, потом по объёму
    order = {"Высокая": 0, "Средняя": 1, "Плановая": 2}
    lines.sort(key=lambda l: (order.get(l.urgency, 3), -l.recommended_qty))

    urgent = sum(1 for l in lines if l.urgency == "Высокая")
    summary = (
        f"{supplier}: рекомендовано заказать {len(lines)} позиций "
        f"(срочных: {urgent}). Разовые всплески исключены из регулярного спроса."
    )

    return PlanResult(supplier=supplier, lines=lines, summary=summary,
                      excess_count=excess_count, spike_items=spike_items,
                      spike_units_total=spike_units_total)
