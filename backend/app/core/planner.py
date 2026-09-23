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
    transfer_from: str = ""    # перераспределение: склад-донор (PRD 6.3.1)
    transfer_qty: float = 0.0  # сколько переместить со склада вместо заказа
    in_budget: bool = True     # прошла ли позиция бюджетный лимит (PRD 6.3.2)

    def order_cost(self) -> float:
        return self.recommended_qty * self.cost

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code, "name": self.name, "supplier": self.supplier,
            "recommended_qty": round(self.recommended_qty, 1), "urgency": self.urgency,
            "reason": self.reason, "reason_tag": self.reason_tag,
            "order_cost": round(self.order_cost()),
            "monthly": [round(m, 1) for m in self.monthly],
            "transfer_from": self.transfer_from,
            "transfer_qty": round(self.transfer_qty, 1),
            "in_budget": self.in_budget,
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
        deficit = sum(1 for ln in self.lines if ln.urgency == "Высокая")
        return {
            "supplier": self.supplier,
            "orders_count": len(self.lines),
            "deficit_count": deficit,
            "excess_count": self.excess_count,
            "total_units": round(sum(ln.recommended_qty for ln in self.lines), 1),
            "total_cost": round(sum(ln.order_cost() for ln in self.lines)),
            "spike_items": self.spike_items,
            "spike_units_total": round(self.spike_units_total, 1),
            "summary": self.summary,
            "lines": [ln.as_dict() for ln in self.lines],
        }


IQR_K = 3.0   # порог выброса: медиана + k*IQR (PRD 6.2.4)


def _quantile(sorted_vals: list[float], q: float) -> float:
    """Линейная интерполяция квантиля (без numpy)."""
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    pos = q * (len(sorted_vals) - 1)
    lo = int(pos)
    frac = pos - lo
    if lo + 1 < len(sorted_vals):
        return sorted_vals[lo] + frac * (sorted_vals[lo + 1] - sorted_vals[lo])
    return sorted_vals[lo]


def detect_spikes(qtys: list[float], k: float = IQR_K) -> tuple[float, int]:
    """Найти разовые крупные продажи (выбросы) по правилу медиана + k*IQR (PRD 6.2.4).

    Возвращает (сумма_выбросов, количество). Выбросы исключаются из регулярного спроса,
    но логируются отдельно (прозрачность + критерий приёмки п.7.4).
    """
    clean = sorted(q for q in qtys if q > 0)
    if len(clean) < 4:
        return 0.0, 0
    median = _quantile(clean, 0.5)
    q1 = _quantile(clean, 0.25)
    q3 = _quantile(clean, 0.75)
    iqr = q3 - q1
    if iqr <= 0:
        return 0.0, 0
    threshold = median + k * iqr
    spikes = [q for q in clean if q > threshold]
    return sum(spikes), len(spikes)


def safety_stock(monthly: list[float], lead_time: float, z: float = 1.65) -> float:
    """Страховой запас: z * σ(спроса) * sqrt(lead_time) (PRD 6.2.2).
    σ считаем по месячному спросу (активные месяцы).
    """
    active = [m for m in monthly if m > 0]
    if len(active) < 2:
        return 0.0
    sigma = statistics.pstdev(active)
    return z * sigma * (lead_time ** 0.5)


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

        # Очищенный от выбросов месячный ряд: выбросы заменяем на медиану активных.
        # Используется для σ (страховой запас) и сезонности, чтобы выброс не раздувал их.
        monthly_clean = list(monthly)
        if months_active:
            _sorted = sorted(months_active)
            _median = _quantile(_sorted, 0.5)
            _q1, _q3 = _quantile(_sorted, 0.25), _quantile(_sorted, 0.75)
            _iqr = _q3 - _q1
            if _iqr > 0:
                _thr = _median + IQR_K * _iqr
                monthly_clean = [(_median if m > _thr else m) for m in monthly]

        # --- must-have 1: базовый месячный спрос ---
        # За основу берём готовый "Ср мес за 12 мес" партнёра (надёжный ориентир),
        # но если детектировали всплеск — пересчитываем среднее БЕЗ выбросов.
        partner_avg = float(row.get("avg_month_12", 0.0))
        clean_active = [m for m in monthly_clean if m > 0]
        if clean_active:
            # базовый спрос — среднее по очищенному ряду (выбросы уже заменены)
            base_demand = statistics.mean(clean_active)
        else:
            base_demand = partner_avg

        # --- must-have 2: сезонность (PRD 6.2.3) ---
        # Сезонный множитель = средний спрос последних 3 мес / средний по очищенному году.
        season_mult = 1.0
        if len(monthly_clean) >= 6 and base_demand > 0:
            last3 = [m for m in monthly_clean[-3:] if m >= 0]
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

        # --- must-have 1: целевой запас и итоговый заказ (PRD 6.2.2) ---
        # target = спрос за срок поставки + страховой запас (z*σ*sqrt(lead))
        # σ по ОЧИЩЕННОМУ ряду — выброс не должен раздувать страховой запас.
        ss = safety_stock(monthly_clean, lead_time)
        target_stock = seasonal_demand * lead_time + ss
        free_stock = float(row.get("free_stock", 0.0))
        in_transit = float(row.get("in_transit", 0.0))
        need = target_stock - free_stock - in_transit
        season = season_mult
        growth = growth_mult
        cost = float(row.get("cost", 0.0))
        coverage_months = (free_stock + in_transit) / seasonal_demand if seasonal_demand > 0 else 99.0

        # --- избыточный запас: остатка хватает надолго (> target * коэф. допуска 1.5) ---
        if need <= 0:
            if seasonal_demand > 0 and (free_stock + in_transit) > target_stock * 1.5:
                excess_count += 1
            if spike_cnt:
                spike_items += 1
                spike_units_total += spike_sum
            continue  # заказ не нужен

        item_moq = moq.get(code, 1.0)
        qty = item_moq * (int((need - 1e-9) / item_moq) + 1) if item_moq > 0 else need

        # --- доп. функция 1: перераспределение между складами (PRD 6.3.1) ---
        # Если на других складах есть излишек этого товара — рекомендуем перемещение
        # вместо (или в дополнение к) внешнего заказа.
        transfer_from = ""
        transfer_qty = 0.0
        wh = {
            "Розничный склад": float(row.get("wh_retail", 0.0)),
            "Витрина": float(row.get("wh_vitrina", 0.0)),
        }
        # донор — склад с наибольшим излишком (запас заметно больше месячного спроса)
        for wh_name, wh_stock in sorted(wh.items(), key=lambda kv: -kv[1]):
            surplus = wh_stock - seasonal_demand  # что сверх месячного спроса склада
            if surplus > 0 and seasonal_demand > 0:
                transfer_qty = min(need, surplus)
                if transfer_qty >= 1:
                    transfer_from = wh_name
                    # заказ у поставщика уменьшаем на объём перемещения
                    remaining = max(0.0, need - transfer_qty)
                    qty = item_moq * (int((remaining - 1e-9) / item_moq) + 1) if (item_moq > 0 and remaining > 0) else remaining
                break

        # срочность по покрытию остатком относительно срока поставки
        if coverage_months < lead_time:
            urgency = "Высокая"
        elif coverage_months < lead_time + 0.5:
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
            f"цель {target_stock:.0f} = спрос×{lead_time:.1f}мес + страх.запас {ss:.0f}",
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

        if transfer_from:
            reason_tag = f"переместить со склада «{transfer_from}»"

        lines.append(OrderLine(
            code=code, name=row["name"], supplier=supplier,
            recommended_qty=qty, urgency=urgency, reason=reason, reason_tag=reason_tag,
            base_demand=base_demand, seasonal_demand=seasonal_demand,
            free_stock=free_stock, in_transit=in_transit, target_stock=target_stock,
            spike_removed=spike_sum, spike_count=spike_cnt, stockout_adj=stockout_adj,
            moq=item_moq, cost=cost, coverage_months=coverage_months, monthly=monthly,
            transfer_from=transfer_from, transfer_qty=transfer_qty,
        ))

    # сортировка: сначала срочные, потом по объёму
    order = {"Высокая": 0, "Средняя": 1, "Плановая": 2}
    lines.sort(key=lambda ln: (order.get(ln.urgency, 3), -ln.recommended_qty))

    urgent = sum(1 for ln in lines if ln.urgency == "Высокая")
    summary = (
        f"{supplier}: рекомендовано заказать {len(lines)} позиций "
        f"(срочных: {urgent}). Разовые всплески исключены из регулярного спроса."
    )

    return PlanResult(supplier=supplier, lines=lines, summary=summary,
                      excess_count=excess_count, spike_items=spike_items,
                      spike_units_total=spike_units_total)


def apply_budget(result: PlanResult, budget: float) -> PlanResult:
    """Доп. функция 2: приоритизация по бюджету (PRD 6.3.2).

    Позиции уже отсортированы по срочности. Накопительно набираем заказы, пока
    не упрёмся в бюджет; остальные помечаем in_budget=False (отсечены) с причиной.
    Основной расчёт не меняется — только флаг и порядок.
    """
    if budget <= 0:
        for ln in result.lines:
            ln.in_budget = True
        return result
    spent = 0.0
    for ln in result.lines:
        c = ln.order_cost()
        if spent + c <= budget:
            ln.in_budget = True
            spent += c
        else:
            ln.in_budget = False
    return result

