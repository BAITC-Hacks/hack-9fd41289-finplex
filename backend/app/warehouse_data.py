"""Explicit warehouse snapshots; never distribute aggregate stocks between warehouses."""

from __future__ import annotations

import json
from datetime import date
from typing import Annotated, Literal

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, model_validator

Nonnegative = Annotated[float, Field(ge=0)]
Identifier = Annotated[str, Field(min_length=1, max_length=150)]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, str_strip_whitespace=True)


class Lot(Strict):
    qty: Nonnegative
    eta: date


class StockItem(Strict):
    code: Identifier
    stock: Nonnegative
    reserved: Nonnegative
    monthly_sales: dict[str, Nonnegative] = Field(min_length=1)
    transit_lots: list[Lot]
    stockout_days: dict[str, Nonnegative] = Field(default_factory=dict)
    cost: Annotated[float, Field(ge=1)] | None = None
    lead_time_days: Annotated[float, Field(gt=0, le=365)] | None = None
    growth_coef: Annotated[float, Field(gt=0, le=10)] = 1

    @model_validator(mode="after")
    def validate_periods(self):
        if self.reserved > self.stock:
            raise ValueError("Резерв превышает физический остаток")
        periods = sorted(self.monthly_sales)
        for period in periods:
            if len(period) != 7 or str(pd.Period(period, freq="M")) != period:
                raise ValueError("Период продаж должен быть YYYY-MM")
        if list(pd.period_range(periods[0], periods[-1], freq="M").astype(str)) != periods:
            raise ValueError("Укажите каждый месяц периода, включая месяцы с нулевыми продажами")
        for period, days in self.stockout_days.items():
            if period not in self.monthly_sales or days > pd.Period(period, freq="M").days_in_month:
                raise ValueError("stockout_days: месяц вне истории или число дней больше календарного")
        return self


class Warehouse(Strict):
    name: Identifier
    items: list[StockItem] = Field(min_length=1, max_length=20000)


class WarehouseBundle(Strict):
    schema_version: Literal[1]
    as_of: date
    warehouses: list[Warehouse] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def unique_and_current(self):
        names = [w.name for w in self.warehouses]
        if len(set(names)) != len(names) or "Все склады (сводно)" in names:
            raise ValueError("Названия складов должны быть уникальными и не обозначать сводный набор")
        for warehouse in self.warehouses:
            codes = [item.code for item in warehouse.items]
            if len(codes) != len(set(codes)):
                raise ValueError(f"{warehouse.name}: повторяющийся код товара")
            windows = {tuple(sorted(item.monthly_sales)) for item in warehouse.items}
            if len(windows) != 1:
                raise ValueError(f"{warehouse.name}: все товары должны иметь одинаковый период наблюдения")
            for item in warehouse.items:
                if max(item.monthly_sales) >= self.as_of.strftime("%Y-%m"):
                    raise ValueError("Передавайте только полные месяцы продаж до месяца as_of")
                if any(lot.eta <= self.as_of for lot in item.transit_lots):
                    raise ValueError("Просроченные поступления нужно сверить до загрузки")
        return self


def load_warehouses(path, catalog, history, moq, metadata):
    if not path.exists():
        return {}
    from .datasets import Dataset

    bundle = WarehouseBundle.model_validate(json.loads(path.read_text(encoding="utf-8-sig")))
    indexed = catalog.set_index("code")
    scopes = {}
    for warehouse in bundle.warehouses:
        rows = []
        for item in warehouse.items:
            if item.code not in indexed.index:
                raise ValueError(f"{warehouse.name}: код {item.code} отсутствует в справочнике поставщика")
            source = indexed.loc[item.code]
            # Only product-level master data is shared. No aggregate demand, stock,
            # stockout, transit, or manually assumed growth leaks into this scope.
            row = {k: source[k] for k in ("name", "article", "unit", "category")}
            row.update(
                code=item.code,
                warehouse=warehouse.name,
                free_stock=item.stock - item.reserved,
                stock_as_of=bundle.as_of.isoformat(),
                stock_estimated=False,
                cost=item.cost,
                growth_coef=item.growth_coef,
                stockout_days=item.stockout_days,
                in_transit=sum(lot.qty for lot in item.transit_lots),
                transit_lots=[lot.model_dump(mode="json") for lot in item.transit_lots],
            )
            if item.lead_time_days is not None:
                row["lead_time"] = item.lead_time_days / 30.4375
            row.update({"m_" + period: qty for period, qty in item.monthly_sales.items()})
            rows.append(row)
        frame = pd.DataFrame(rows)
        frame.attrs.update(
            as_of=bundle.as_of.isoformat(),
            lead_time=catalog.attrs.get("lead_time", 1.5),
            category_policies=catalog.attrs.get("category_policies", {}),
        )
        tx = history.iloc[:0].copy()
        if "warehouse" in history:
            tx = history[history.warehouse.eq(warehouse.name) & history.code.isin(frame.code)].copy()
            tx = tx[tx.date < pd.Timestamp(bundle.as_of) + pd.Timedelta(days=1)]
        warnings = [
            "Складской расчёт включает только товары, перечисленные в warehouses.json; это не сумма всех складов.",
            "Свободный остаток = физический остаток − резерв. Межскладские перемещения не выполняются.",
        ]
        if tx.empty:
            warnings.append("Нет накладных этого склада: аномалии проверяются только по месячным продажам.")
        elif "customer_id" not in tx or not tx.customer_id.fillna("").astype(str).str.strip().ne("").any():
            warnings.append("Нет обезличенных ID клиентов склада: аномалии сделок группируются по документам.")
        if any(item.lead_time_days is None for item in warehouse.items):
            warnings.append("Для части товаров использован срок поставщика; проверьте его перед утверждением.")
        scoped_metadata = dict(
            metadata,
            as_of=bundle.as_of.isoformat(),
            products=len(frame),
            warnings=warnings,
            sources=metadata["sources"] + ["warehouses.json"],
            warehouse=warehouse.name,
        )
        scopes[warehouse.name] = Dataset(frame, tx, moq, scoped_metadata)
    return scopes


if __name__ == "__main__":
    import argparse
    from pathlib import Path

    parser = argparse.ArgumentParser(description="Проверка warehouses.json без изменения данных")
    parser.add_argument("path", type=Path)
    args = parser.parse_args()
    validated = WarehouseBundle.model_validate_json(args.path.read_text(encoding="utf-8-sig"))
    print(f"OK: {len(validated.warehouses)} warehouses, snapshot {validated.as_of}")
