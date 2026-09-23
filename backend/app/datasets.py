"""Versioned partner bundles. Warehouse scopes are not implicitly interchangeable."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from functools import lru_cache
from pathlib import Path

import pandas as pd

from .core.loader import load_monthly, load_moq, load_sales_history, load_seasonality, load_showcase, load_transit_iek

DATA_DIR = Path(os.environ.get("DATA_DIR", Path(__file__).resolve().parents[2] / "data"))
DATASETS = {
    "systeme": {
        "name": "Systeme Electric",
        "as_of": "2026-09-22",
        "files": [
            "Динамика продаж_Syseme Electric_2025-2026.xlsx",
            "Ежемесячные продажи в кол-м выражении SystemElectric 2024-2026.xlsx",
            "Ежемесячные остатки SystemElectric 2024-2026.xlsx",
            "Товар в пути_SystemElectric на 22.09.2026.xlsx",
            "MOQ SystemElectric.xlsx",
            "Сезонность SystemElectric 2024-2026.xlsx",
        ],
    },
    "iek": {
        "name": "ИЭК",
        "as_of": "2026-09-22",
        "files": [
            "Динамика продаж_2025-2026.xlsx",
            "Ежемесячные продажи в количественном выражении за последние 2 года.xlsx",
            "Ежемесячные остатки продукции за последние 2 года  ИЭК.xlsx",
            "Путь ИЭК 22.09.2026.xlsx",
            "MOQ  ИЭК.xlsx",
            "Сезонность ИЭК.xlsx",
        ],
    },
}


@dataclass
class Dataset:
    showcase: pd.DataFrame
    history: pd.DataFrame
    moq: dict
    metadata: dict
    warehouses: dict = dataclass_field(default_factory=dict)


def available():
    return [
        {"key": key, "name": ds["name"]}
        for key, ds in DATASETS.items()
        if all((DATA_DIR / key / f).exists() for f in ds["files"])
    ]


def signature(key):
    paths = [DATA_DIR / key / name for name in DATASETS[key]["files"]]
    paths += [DATA_DIR / key / "planning.json", DATA_DIR / key / "warehouses.json"]
    return tuple((str(p), p.stat().st_mtime_ns, p.stat().st_size) if p.exists() else (str(p), 0, 0) for p in paths)


def load_dataset(key):
    return _load(key, signature(key))


@lru_cache(maxsize=8)
def _load(key, version):
    ds = DATASETS[key]
    paths = [DATA_DIR / key / name for name in ds["files"]]
    config_path = DATA_DIR / key / "planning.json"
    config = json.loads(config_path.read_text(encoding="utf-8-sig")) if config_path.exists() else {}
    as_of = config.get("as_of", ds["as_of"])
    pd.Timestamp(as_of)
    history = load_sales_history(paths[0])
    history = history[history.date < pd.Timestamp(as_of).normalize() + pd.Timedelta(days=1)].copy()
    monthly = load_monthly(paths[1]).set_index("code")
    stock = load_monthly(paths[2], "s_").set_index("code")
    moq = load_moq(paths[4])
    seasons = load_seasonality(paths[5], as_of)
    transit = (load_showcase(paths[3]) if key == "systeme" else load_transit_iek(paths[3])).set_index("code")
    codes = monthly.index.union(stock.index).union(transit.index).union(pd.Index(history.code.unique()))
    out = monthly.reindex(codes).copy()
    warnings = [
        "Нет точных интервалов stockout: нулевые начальные остатки — только сигнал для проверки.",
        "Автоперемещения отключены: нет согласованных остатков, резервов и спроса каждого склада.",
    ]
    if "customer_id" not in history or not history.customer_id.fillna("").astype(str).str.strip().ne("").any():
        warnings.append("Нет обезличенного ID клиента: группировка аномалий по документу, не по клиенту.")
    for c in stock:
        if c.startswith("s_"):
            out[c] = stock[c].reindex(codes)
    for c in ["name", "article", "category", "cost", "growth_coef", "free_stock", "in_transit", "transit_lots"]:
        values = transit[c].reindex(codes) if c in transit else pd.Series(index=codes, dtype=object)
        if c in {"name", "article", "category"}:
            values = values.map(lambda value: None if isinstance(value, str) and not value.strip() else value)
        out[c] = values.combine_first(out[c]) if c in out else values
    stock_cols = sorted(c for c in out if c.startswith("s_") and c[2:] <= as_of[:7])
    last_stock = stock_cols[-1] if stock_cols else None
    fallback = out.free_stock.isna()
    out.loc[fallback, "free_stock"] = out.loc[fallback, last_stock] if last_stock else None
    out["stock_as_of"] = as_of
    out.loc[fallback, "stock_as_of"] = last_stock[2:] + "-01" if last_stock else "неизвестно"
    out["stock_estimated"] = fallback
    out.loc[out.free_stock.isna(), "stock_as_of"] = "неизвестно"
    out["warehouse"] = "Все склады (сводно)"
    out["category"] = out.category.fillna("Без категории").replace("", "Без категории")
    # Fill absent identifiers only from an unambiguous value for the same 1C code.
    # Never guess an article from a product name or treat a sales price as a purchase price.
    for field in ("name", "article", "unit"):
        candidates = []
        for source in (monthly, stock, transit):
            if field in source:
                candidates.append(source[field])
        if field in history:
            candidates.append(history.set_index("code")[field])
        values = pd.concat(candidates) if candidates else pd.Series(dtype=object)
        values = values.dropna().astype(str).str.strip()
        values = values[~values.isin(["", "не указана", "nan", "None"])]
        unique = values.groupby(level=0).agg(lambda v: sorted(set(v)))
        resolved = unique[unique.map(len) == 1].map(lambda v: v[0])
        current = out.get(field, pd.Series(index=codes, dtype=object)).map(
            lambda value: None if isinstance(value, str) and not value.strip() else value
        )
        missing = current.isna() | current.eq("не указана")
        out[field] = current.mask(missing, resolved.reindex(codes))
        conflicts = int((missing & unique.map(len).reindex(codes).gt(1)).sum())
        if conflicts:
            warnings.append(f"{field}: неоднозначные реквизиты для {conflicts} товаров; требуется проверка.")
    out["unit"] = out.unit.fillna("не указана")
    out["name"] = out.name.fillna(stock.name).fillna(pd.Series(codes, index=codes))
    out["article"] = out.article.fillna("")
    out["in_transit"] = pd.to_numeric(out.in_transit).fillna(0)
    out["transit_lots"] = out.transit_lots.map(lambda v: v if isinstance(v, list) else [])
    missing_summary = ~out.index.isin(monthly.index)
    invoice_months = (
        history.assign(period=history.date.dt.to_period("M").astype(str)).groupby(["code", "period"]).qty.sum()
    )
    # Do not lose history-only months; zeros inside the observed interval remain zeros.
    summary_periods = [c[2:] for c in out if c.startswith("m_")]
    observed = summary_periods + list(invoice_months.index.get_level_values("period"))
    if observed:
        # The summary defines the reliable start of the observation window.
        # Sparse older invoices must not introduce fictitious zero-sales years.
        start = min(summary_periods) if summary_periods else min(observed)
        for period in pd.period_range(start, max(observed), freq="M").astype(str):
            if "m_" + period not in out:
                out["m_" + period] = (
                    invoice_months.xs(period, level="period").reindex(codes)
                    if period in invoice_months.index.get_level_values("period")
                    else 0.0
                )
    for c in [c for c in out if c.startswith("m_")]:
        if c[2:] in invoice_months.index.get_level_values("period"):
            fallback_sales = invoice_months.xs(c[2:], level="period").reindex(out.index).clip(lower=0)
            out.loc[missing_summary, c] = fallback_sales[missing_summary]
        out[c] = out[c].fillna(0)
    if missing_summary.any():
        warnings.append(
            f"{int(missing_summary.sum())} товаров вне месячной сводки: использована доступная история накладных."
        )
    overrides = config.get("items", {})
    allowed = {
        "cost",
        "category",
        "free_stock",
        "stock_as_of",
        "stock_estimated",
        "unit",
        "growth_coef",
        "lead_time",
        "stockout_days",
        "transit_lots",
        "article",
    }
    for code, changes in overrides.items():
        if code not in out.index or set(changes) - allowed:
            raise ValueError(f"planning.json: неизвестный товар или поле для {code}")
        for field, value in changes.items():
            if field not in out:
                out[field] = pd.Series(index=out.index, dtype=object)
            out.at[code, field] = value
    dates = history.date.dt.to_period("M").astype(str)
    hist_monthly = history.assign(period=dates).groupby(["code", "period"]).qty.sum()
    compared = mismatches = 0
    for col in [c for c in out if c.startswith("m_")]:
        if col[2:] in hist_monthly.index.get_level_values("period"):
            actual = hist_monthly.xs(col[2:], level="period").reindex(out.index)
            valid = actual.notna()
            compared += int(valid.sum())
            mismatches += int(((actual - out[col]).abs()[valid] > 0.01).sum())
    matched = sum(c in moq for c in out.index)
    warnings += [
        f"MOQ сопоставлен: {matched}/{len(out)}. Остальные — партия 1 с предупреждением.",
        f"История и сводные продажи: расхождения в {mismatches}/{compared} SKU-месяцев; основа — сводные продажи.",
        "Срок поставки по умолчанию 1,5 месяца — допущение, уточните у поставщика.",
    ]
    if fallback.any():
        warnings.append(
            f"{int(fallback.sum())} позиций без текущего остатка; источник замены: {last_stock[2:] if last_stock else 'отсутствует'}. Обновите до утверждения."
        )
    out = out.reset_index(names="code")
    out.attrs.update(
        {
            "as_of": as_of,
            "supplier_seasonality": seasons,
            "category_policies": config.get("category_policies", {}),
            "lead_time": config.get("lead_time_months", 1.5),
        }
    )
    metadata = {
        "as_of": as_of,
        "version": hashlib.sha256(repr(version).encode()).hexdigest()[:20],
        "sources": [p.name for p in paths] + (["planning.json"] if config else []),
        "warnings": warnings,
        "history_quality": history.attrs.get("quality", {}),
        "reconciliation": {"compared": compared, "mismatches": mismatches},
        "moq_matched": matched,
        "products": len(out),
    }
    from .warehouse_data import load_warehouses

    scopes = load_warehouses(DATA_DIR / key / "warehouses.json", out, history, moq, metadata)
    return Dataset(out, history, moq, metadata, scopes)
