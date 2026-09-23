"""Rolling forecast diagnostic, no current stock/growth or future sales in training.
Usage: python -m app.backtest --supplier systeme --products 30 --months 3
This compares against observed sales, not unobserved latent demand/service level.
"""

import argparse
import json

import pandas as pd

from .core.loader import load_seasonality
from .core.planner import compute_orders
from .datasets import DATA_DIR, DATASETS, load_dataset


def backtest(key, products=30, months=3):
    ds = load_dataset(key)
    all_months = sorted(c for c in ds.showcase if c.startswith("m_") and c[2:] < ds.metadata["as_of"][:7])
    # Fixed universe by early training volume: never rank using future holdouts.
    ranking = ds.showcase[all_months[:-months]].sum(axis=1).nlargest(products).index
    original = ds.showcase.loc[ranking].copy()
    stats = {
        "model": {"absolute_error": 0.0, "actual": 0.0, "shortfall_units": 0.0, "excess_units": 0.0},
        "seasonal_naive": {"absolute_error": 0.0, "actual": 0.0, "shortfall_units": 0.0, "excess_units": 0.0},
    }
    folds = []
    for target in all_months[-months:]:
        start = pd.Period(target[2:], "M")
        cutoff = (start - 1).end_time.date().isoformat()
        columns = [c for c in all_months if c < target]
        frame = original[["code", "name", "category", "unit", "article"] + columns].copy()
        frame["free_stock"] = 0
        frame.attrs = {
            "as_of": cutoff,
            "supplier_seasonality": load_seasonality(DATA_DIR / key / DATASETS[key]["files"][5], cutoff),
        }
        history = ds.history[ds.history.date < pd.Timestamp(start.start_time)]
        plan = compute_orders(frame, history, ds.moq, key, safety=0)
        by_code = {x.code: x for x in plan.states}
        naive_col = "m_" + str(start - 12)
        for _, row in original.iterrows():
            item = by_code[row.code]
            index = item.forecast_periods.index(str(start))
            actual = max(0, float(row[target]))
            for name, predicted in [("model", item.forecast[index]), ("seasonal_naive", max(0, float(row[naive_col])))]:
                diff = predicted - actual
                stats[name]["absolute_error"] += abs(diff)
                stats[name]["actual"] += actual
                stats[name]["shortfall_units"] += max(0, -diff)
                stats[name]["excess_units"] += max(0, diff)
        folds.append({"train_through": cutoff, "predict_month": str(start)})
    for data in stats.values():
        data["wape"] = data["absolute_error"] / data["actual"] if data["actual"] else None
        for k, v in data.items():
            if v is not None:
                data[k] = round(v, 4)
    return {
        "supplier": key,
        "products": len(original),
        "folds": folds,
        "metrics": stats,
        "limitations": "Observed monthly sales only; stockout intervals/customer IDs missing. Not a stock service-level simulation.",
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--supplier", choices=DATASETS, default="systeme")
    parser.add_argument("--products", type=int, default=30)
    parser.add_argument("--months", type=int, default=3)
    args = parser.parse_args()
    if not 1 <= args.products <= 1000 or not 1 <= args.months <= 12:
        parser.error("products: 1..1000; months: 1..12")
    print(json.dumps(backtest(args.supplier, args.products, args.months), ensure_ascii=False, indent=2))
