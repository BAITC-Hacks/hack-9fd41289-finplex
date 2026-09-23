"""Explainable calendar demand planning; no LLM or unverified stockout inference."""

from __future__ import annotations

import math
import statistics
from dataclasses import asdict, dataclass, field
from decimal import ROUND_CEILING, Decimal

import pandas as pd

DEFAULT_LEAD_TIME_MONTHS = 1.5
DEFAULT_SAFETY_MONTHS = 0.5
IQR_K = 3.0


def finite(value, name, minimum=None, default=None):
    if value is None or (isinstance(value, float) and math.isnan(value)):
        if default is not None:
            value = default
        else:
            raise ValueError(f"{name}: значение отсутствует")
    result = float(value)
    if not math.isfinite(result) or (minimum is not None and result < minimum):
        raise ValueError(f"{name}: недопустимое значение")
    return result


def _quantile(vals, q):
    return float(pd.Series(vals, dtype=float).quantile(q)) if vals else 0.0


def spike_threshold(qtys, k=IQR_K):
    values = sorted(finite(x, "quantity", 0) for x in qtys if x > 0)
    if len(values) < 4:
        return math.inf
    median = statistics.median(values)
    # Absolute and relative floors prevent IQR=0 failure and over-filtering small noise.
    return max(median + k * (_quantile(values, 0.75) - _quantile(values, 0.25)), median * 5, median + 10)


def detect_spikes(qtys, k=IQR_K):
    threshold = spike_threshold(qtys, k)
    spikes = [float(q) for q in qtys if q > threshold]
    return sum(spikes), len(spikes)


def safety_stock(monthly, lead_time, z=1.65):
    lead_time = finite(lead_time, "lead_time", 0)
    values = [finite(v, "monthly", 0) for v in monthly]
    return z * statistics.pstdev(values) * math.sqrt(lead_time) if len(values) > 1 else 0.0


def round_order(need, pack_size=1, min_qty=1):
    if need <= 0:
        return 0.0
    step = Decimal(str(finite(pack_size, "pack_size", 0.000001)))
    value = Decimal(str(max(need, finite(min_qty, "min_qty", 0.000001))))
    return float((value / step).to_integral_value(rounding=ROUND_CEILING) * step)


@dataclass
class OrderLine:
    code: str
    name: str
    supplier: str
    recommended_qty: float
    urgency: str
    reason: str
    reason_tag: str = ""
    article: str = ""
    unit: str = "шт"
    category: str = ""
    warehouse: str = "Все склады (сводно)"
    base_demand: float = 0
    seasonal_demand: float = 0
    free_stock: float = 0
    in_transit: float = 0
    target_stock: float = 0
    spike_removed: float = 0
    spike_count: int = 0
    stockout_adj: float = 0
    moq: float = 1
    min_qty: float = 1
    cost: float | None = None
    coverage_months: float = 99
    monthly: list = field(default_factory=list)
    periods: list = field(default_factory=list)
    cleaned_monthly: list = field(default_factory=list)
    forecast: list = field(default_factory=list)
    forecast_periods: list = field(default_factory=list)
    confirmed_stockout: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    in_budget: bool = True
    lead_time: float = 1.5
    safety_units: float = 0
    growth: float = 0
    stock_as_of: str = ""
    eligible_transit: float = 0
    transfer_from: str = ""
    transfer_qty: float = 0

    def order_cost(self):
        return None if self.cost is None else self.recommended_qty * self.cost

    def as_dict(self):
        result = asdict(self)
        result["order_cost"] = round(self.order_cost(), 2) if self.order_cost() is not None else None
        result["detail"] = {
            "base_demand_month": round(self.base_demand, 3),
            "seasonal_demand_month": round(self.seasonal_demand, 3),
            "free_stock": self.free_stock,
            "in_transit": self.in_transit,
            "eligible_transit": self.eligible_transit,
            "target_stock": round(self.target_stock, 3),
            "spike_removed_units": round(self.spike_removed, 3),
            "spike_count": self.spike_count,
            "stockout_adj_units": round(self.stockout_adj, 3),
            "coverage_months": round(self.coverage_months, 3),
            "moq": self.moq,
            "min_qty": self.min_qty,
            "safety_units": round(self.safety_units, 3),
            "growth": self.growth,
            "lead_time": self.lead_time,
        }
        return result


@dataclass
class PlanResult:
    supplier: str
    lines: list = field(default_factory=list)
    states: list = field(default_factory=list)
    summary: str = ""
    excess_count: int = 0
    spike_items: int = 0
    spike_units_total: float = 0

    def as_dict(self):
        return {
            "supplier": self.supplier,
            "orders_count": len(self.lines),
            "deficit_count": sum(x.urgency == "Высокая" for x in self.states),
            "excess_count": self.excess_count,
            "total_units": round(sum(x.recommended_qty for x in self.lines), 3),
            "total_cost": round(sum(x.order_cost() or 0 for x in self.lines), 2),
            "unpriced_count": sum(x.cost is None for x in self.lines),
            "spike_items": self.spike_items,
            "spike_units_total": round(self.spike_units_total, 3),
            "summary": self.summary,
            "lines": [x.as_dict() for x in self.lines],
        }


def clean_months(values, periods, transactions):
    cleaned = list(values)
    count = 0
    warnings = []
    if transactions is not None and len(transactions) and "date" in transactions:
        tx = transactions.copy()
        tx["period"] = pd.to_datetime(tx.date).dt.to_period("M").astype(str)
        tx = tx[tx.period.isin(periods) & (tx.qty > 0)].copy()
        tx["day"] = pd.to_datetime(tx.date).dt.date.astype(str)
        # Customer-day joins split documents for a single large purchase. Unknown IDs
        # remain grouped by document, never all collapsed into one anonymous customer.
        docs = tx["document"] if "document" in tx else pd.Series(tx.index.astype(str), index=tx.index)
        customer = tx.get("customer_id", pd.Series("", index=tx.index)).fillna("").astype(str)
        tx["group"] = [("customer:" + c if c else "doc:" + str(d)) for c, d in zip(customer, docs, strict=False)]
        deals = tx.groupby(["period", "day", "group"], as_index=False).qty.sum()
        threshold = spike_threshold(deals.qty.tolist())
        big = deals[deals.qty > threshold]
        if big.period.nunique() >= 3:
            # Mixed retail/wholesale sizes are not a single homogeneous distribution.
            # Recurrent large invoices form their own regular segment. Only outliers
            # within that segment are one-offs; missing customer IDs do not change this.
            big = big[big.qty > spike_threshold(big.qty.tolist())]
        if len(big):
            seasonal = []
            for idx, deal in big.iterrows():
                peers = big[(big.period != deal.period) & (big.period.str[-2:] == deal.period[-2:])]
                if (peers.qty >= deal.qty * 0.5).any():
                    seasonal.append(idx)
            big = big.drop(index=seasonal)
        # Repeated large orders across >=3 months are not one-off purchases.
        recurring = big.groupby("group").period.nunique()
        big = big[~big["group"].isin(recurring[recurring >= 3].index)]
        totals = tx.groupby("period").qty.sum()
        for period, group in big.groupby("period"):
            index = periods.index(period)
            qty = float(group.qty.sum())
            total = float(totals[period])
            if abs(total - values[index]) > max(1, values[index] * 0.05):
                # Summaries and invoices have different scopes in the supplied extracts.
                # Apply the anomalous share, not an unrelated absolute quantity.
                qty = values[index] * min(1, qty / total)
                warnings.append("Аномалии сделок перенесены пропорционально: итог истории отличается от сводки.")
            cleaned[index] = max(0, cleaned[index] - qty)
            count += len(group)
    # Conservative monthly fallback: only isolated peaks; protect recurring calendar
    # peaks and runs of high months. Never interpret an entire seasonal quarter as noise.
    threshold = spike_threshold(cleaned)
    for index, value in enumerate(list(cleaned)):
        if value <= threshold:
            continue
        peers = [v for j, v in enumerate(cleaned) if j != index and periods[j][5:7] == periods[index][5:7]]
        adjacent = [cleaned[j] for j in [index - 1, index + 1] if 0 <= j < len(cleaned)]
        if any(v >= value * 0.5 for v in peers + adjacent):
            continue
        baseline = statistics.median([v for v in cleaned if v <= threshold])
        cleaned[index] = baseline
        count += 1
    return cleaned, sum(values) - sum(cleaned), count, sorted(set(warnings))


def seasonal_profile(values, periods):
    mean = statistics.mean(values) if values else 0
    if mean <= 0:
        return [1.0] * 12
    profile = []
    for month in range(1, 13):
        observed = [v for v, p in zip(values, periods, strict=False) if int(p[5:7]) == month]
        profile.append(statistics.mean(observed) / mean if observed else 1.0)
    norm = statistics.mean(profile)
    return [max(0.05, p / norm) for p in profile]


def compute_orders(
    showcase, history, moq, supplier, lead_time=None, safety=DEFAULT_SAFETY_MONTHS, *, as_of=None, demand_multiplier=1.0
):
    if lead_time is not None:
        lead_time = finite(lead_time, "lead_time", 0.01)
    safety = finite(safety, "safety", 0)
    demand_multiplier = finite(demand_multiplier, "demand_multiplier", 0.000001)
    if safety > 12 or demand_multiplier > 10:
        raise ValueError("Параметр вне допустимого диапазона")
    columns = sorted(c for c in showcase if str(c).startswith("m_"))
    periods = [str(pd.Period(c[2:], freq="M")) for c in columns]
    if not columns:
        raise ValueError("Нет месячной истории продаж")
    as_of = pd.Timestamp(as_of or showcase.attrs.get("as_of") or pd.Period(periods[-1], freq="M").end_time.date())
    complete = [i for i, p in enumerate(periods) if pd.Period(p, "M").end_time.date() <= as_of.date()]
    if not complete:
        raise ValueError("Нет полного месяца до даты расчёта")
    hist = {}
    if history is not None and len(history):
        valid = history
        if "date" in valid:
            valid = valid[pd.to_datetime(valid.date) < as_of.normalize() + pd.Timedelta(days=1)]
        hist = dict(tuple(valid.groupby("code")))
    prepared = []
    for _, row in showcase.iterrows():
        raw = [max(0, finite(row[c], c)) for c in columns]
        clean, removed, count, warnings = clean_months(raw, periods, hist.get(row.code))
        clean = list(clean)
        confirmed = row.get("stockout_days", {})
        confirmed = confirmed if isinstance(confirmed, dict) else {}
        added = 0.0
        for period, days in confirmed.items():
            if period not in periods:
                raise ValueError(f"{row.code}: stockout вне окна истории")
            i = periods.index(period)
            total_days = pd.Period(period, "M").days_in_month
            days = finite(days, "stockout_days", 0)
            if days > total_days:
                raise ValueError("Stockout превышает число дней месяца")
            peers = [clean[j] for j in complete if j != i and periods[j][5:7] == period[5:7]]
            reference = (
                statistics.mean(peers) if peers else statistics.mean([clean[j] for j in complete if j != i] or [0])
            )
            estimate = (
                clean[i] / (total_days - days) * days if 0 < days <= total_days - 7 else reference * days / total_days
            )
            clean[i] += estimate
            added += estimate
        prepared.append((row, raw, clean, removed, count, warnings, added, confirmed))
    category_profiles = {}
    for category in {str(p[0].get("category", "Без категории")) for p in prepared}:
        members = [p[2] for p in prepared if str(p[0].get("category", "Без категории")) == category]
        category_profiles[category] = seasonal_profile(
            [sum(v[i] for v in members) for i in complete], [periods[i] for i in complete]
        )
    supplier_profile = showcase.attrs.get("supplier_seasonality", [1.0] * 12)
    if len(supplier_profile) != 12 or any(not math.isfinite(float(x)) or x <= 0 for x in supplier_profile):
        raise ValueError("Неверный сезонный профиль поставщика")
    result = PlanResult(supplier)
    for row, raw, clean, removed, count, warnings, added, confirmed in prepared:
        category = str(row.get("category", "Без категории"))
        policy = showcase.attrs.get("category_policies", {}).get(category, {})
        lead = finite(
            lead_time if lead_time is not None else row.get("lead_time"),
            "lead_time",
            0.01,
            default=showcase.attrs.get("lead_time", DEFAULT_LEAD_TIME_MONTHS),
        )
        if lead > 12:
            raise ValueError("lead_time должен быть <=12 месяцев")
        training = [clean[i] for i in complete]
        calendar = [periods[i] for i in complete]
        own = seasonal_profile(training, calendar)
        cat = category_profiles[category]
        weight = 0.6 if len(training) >= 24 else 0.2
        profile = [
            weight * a + (1 - weight) * 0.5 * (b + c) for a, b, c in zip(own, cat, supplier_profile, strict=False)
        ]
        norm = statistics.mean(profile)
        profile = [x / norm for x in profile]
        deseasoned = [v / profile[int(p[5:7]) - 1] for v, p in zip(training, calendar, strict=False)]
        base = statistics.mean(deseasoned[-12:])
        ratios = [
            training[i] / training[i - 12] - 1
            for i in range(max(12, len(training) - 6), len(training))
            if training[i - 12] > 0
        ]
        inferred = (
            statistics.median(ratios)
            if len(ratios) >= 4 and max(sum(x > 0 for x in ratios), sum(x < 0 for x in ratios)) >= len(ratios) * 0.75
            else 0
        )
        supplied = row.get("growth_coef")
        growth = finite(supplied, "growth_coef", default=inferred)
        growth = max(-0.4, min(0.4, growth))
        level = base * (1 + growth) * demand_multiplier
        forecast_periods = [str(as_of.to_period("M") + i) for i in range(14)]
        forecast = [level * profile[int(p[5:7]) - 1] for p in forecast_periods]
        # Integrate calendar demand from the day after the snapshot to replenishment.
        days = max(1, math.ceil(lead * 30.4375))
        daily = []
        for day in pd.date_range(as_of + pd.Timedelta(days=1), periods=days):
            daily.append(level * profile[day.month - 1] / day.days_in_month)
        demand_lead = sum(daily)
        avg = demand_lead / lead
        multiplier = finite(policy.get("safety_multiplier", 1), "category safety", 0.01)
        ss = max(avg * safety, safety_stock(deseasoned[-12:], lead) * (1 + growth) * demand_multiplier) * multiplier
        target = demand_lead + ss
        stock = row.get("free_stock")
        if stock is None or pd.isna(stock):
            stock = 0
            warnings.append("Остаток неизвестен; рекомендация требует проверки.")
        stock = finite(stock, "free_stock")
        if row.get("stock_estimated", False):
            warnings.append("Использован начальный месячный остаток; подтвердите актуальность.")
        signals = sum(row.get("s_" + p) == 0 for p in calendar if pd.notna(row.get("s_" + p)))
        if signals:
            warnings.append(f"Нулевой начальный остаток в {signals} мес.; длительность stockout не установлена.")
        lots = row.get("transit_lots")
        if not isinstance(lots, list):
            q = finite(row.get("in_transit", 0), "in_transit", 0)
            lots = [{"qty": q, "eta": None}] if q else []
        transit = eligible = 0.0
        known_arrivals = {}
        for lot in lots:
            qty = finite(lot["qty"], "transit qty", 0)
            transit += qty
            eta = pd.Timestamp(lot["eta"]) if lot.get("eta") else None
            if eta is None:
                eligible += qty
                warnings.append("ETA товара в пути неизвестна; срочность рассчитана по наличному запасу.")
            elif eta.date() <= as_of.date():
                warnings.append("Просроченное поступление не зачтено: подтвердите приёмку.")
            elif eta <= as_of + pd.Timedelta(days=days):
                eligible += qty
                known_arrivals[eta.date()] = known_arrivals.get(eta.date(), 0) + qty
        # Unknown/late arrivals never mask an immediate shortage.
        balance = stock
        shortage = False
        for day, consumption in zip(pd.date_range(as_of + pd.Timedelta(days=1), periods=days), daily, strict=False):
            balance += known_arrivals.get(day.date(), 0)
            balance -= consumption
            shortage |= balance < -1e-8
        need = max(0, target - stock - eligible)
        rule = moq.get(row.code, {"pack_size": 1, "min_qty": 1})
        if not isinstance(rule, dict):
            rule = {"pack_size": rule, "min_qty": rule}
        if row.code not in moq:
            warnings.append("Партия поставщика неизвестна: временно использована 1.")
        qty = round_order(need, rule["pack_size"], rule["min_qty"])
        cost = row.get("cost")
        cost = finite(cost, "cost", 0) if cost is not None and pd.notna(cost) and float(cost) > 0 else None
        if cost is None:
            warnings.append("Нет цены: заполните в черновике до утверждения.")
        coverage = max(0, stock) / avg if avg > 0 else 99
        urgency = "Высокая" if shortage else ("Средняя" if stock < target else "Плановая")
        reason = (
            f"База {base:.2f}/мес; календарный прогноз на срок {lead:g} мес: {demand_lead:.2f}; "
            f"рост {growth:+.1%}; страховой запас {ss:.2f} (категория {category}, ×{multiplier:g}); "
            f"цель {target:.2f} − наличный {stock:g} − зачтённый путь {eligible:g}; "
            f"минимум {rule['min_qty']:g}, кратность {rule['pack_size']:g} → {qty:g}. "
            f"Исключено аномалий {removed:.2f}; восстановлено подтверждённого спроса {added:.2f}."
        )
        ln = OrderLine(
            code=row.code,
            name=str(row["name"]),
            supplier=supplier,
            recommended_qty=qty,
            urgency=urgency,
            reason=reason,
            reason_tag="риск дефицита" if shortage else "плановое пополнение",
            article=str(row.get("article", "")),
            unit=str(row.get("unit", "шт")),
            category=category,
            warehouse=str(row.get("warehouse", "Все склады (сводно)")),
            base_demand=base,
            seasonal_demand=avg,
            free_stock=stock,
            in_transit=transit,
            eligible_transit=eligible,
            target_stock=target,
            spike_removed=removed,
            spike_count=count,
            stockout_adj=added,
            moq=rule["pack_size"],
            min_qty=rule["min_qty"],
            cost=cost,
            coverage_months=coverage,
            monthly=raw,
            periods=periods,
            cleaned_monthly=clean,
            forecast=forecast[:4],
            forecast_periods=forecast_periods[:4],
            confirmed_stockout=list(confirmed),
            warnings=sorted(set(warnings)),
            lead_time=lead,
            safety_units=ss,
            growth=growth,
            stock_as_of=str(row.get("stock_as_of", as_of.date().isoformat())),
        )
        result.states.append(ln)
        if qty > 0:
            result.lines.append(ln)
        elif avg > 0 and stock + eligible > target * 1.5:
            result.excess_count += 1
        result.spike_items += count > 0
        result.spike_units_total += removed
    order = {"Высокая": 0, "Средняя": 1, "Плановая": 2}
    result.lines.sort(key=lambda x: (order[x.urgency], x.coverage_months, x.code))
    result.summary = f"{supplier}: {len(result.lines)} позиций. Проверьте предупреждения и подтвердите заказ вручную."
    return result


def apply_budget(result, budget):
    budget = finite(budget, "budget", 0)
    spent = 0
    for line in result.lines:
        cost = line.order_cost()
        line.in_budget = budget == 0 or (cost is not None and spent + cost <= budget)
        if line.in_budget and cost is not None:
            spent += cost
    return result
