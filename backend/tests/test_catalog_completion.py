import pandas as pd
import pytest

from app.core.planner import compute_orders
from app.orders import Store, export_csv


@pytest.fixture
def catalog(tmp_path, monkeypatch):
    import app.datasets as datasets

    monthly = pd.DataFrame([dict(code="A", name="Known", article="", m_2025_01=10)]).rename(
        columns={"m_2025_01": "m_2025-01"}
    )
    stock = pd.DataFrame([{"code": "A", "name": "Known", "article": "ART-A", "unit": "шт", "s_2025-01": 0}])
    history = pd.DataFrame(
        [
            dict(code="H", name="History only", article="ART-H", unit="шт", qty=50, date="2025-02-10"),
            dict(code="A", name="Known", article="ART-A", unit="шт", qty=999, date="2023-01-10"),
            dict(code="C", name="Conflict", article="ART-C", unit="шт", qty=10, date="2025-02-10"),
            dict(code="C", name="Conflict", article="ART-C", unit="уп", qty=10, date="2025-02-11"),
            dict(code="FUTURE", name="Future", article="ART-F", unit="шт", qty=999, date="2027-01-10"),
        ]
    )
    history["date"] = pd.to_datetime(history.date)
    monkeypatch.setattr(datasets, "DATA_DIR", tmp_path)
    monkeypatch.setattr(datasets, "load_sales_history", lambda path: history.copy())
    monkeypatch.setattr(
        datasets, "load_monthly", lambda path, prefix="m_": (monthly if prefix == "m_" else stock).copy()
    )
    monkeypatch.setattr(
        datasets, "load_showcase", lambda path: pd.DataFrame([dict(code="A", name="Known", article="")])
    )
    monkeypatch.setattr(datasets, "load_moq", lambda path: {})
    monkeypatch.setattr(datasets, "load_seasonality", lambda *args: [1.0] * 12)
    return datasets._load("systeme", (str(tmp_path),))


def test_history_only_sku_and_month_are_included(catalog):
    rows = catalog.showcase.set_index("code")
    assert "H" in rows.index and "FUTURE" not in rows.index
    assert rows.loc["H", "m_2025-02"] == 50
    assert rows.loc["H", "m_2025-01"] == 0
    assert "m_2023-01" not in rows
    assert pd.isna(rows.loc["H", "free_stock"])
    assert rows.loc["H", "stock_as_of"] == "неизвестно"
    plan = compute_orders(catalog.showcase, catalog.history, catalog.moq, "S")
    item = next(x for x in plan.lines if x.code == "H")
    assert item.recommended_qty > 0 and item.article == "ART-H"
    assert any("Остаток неизвестен" in warning for warning in item.warnings)


def test_requisites_use_exact_code_and_only_unambiguous_fallback(catalog):
    rows = catalog.showcase.set_index("code")
    assert rows.loc["A", "article"] == "ART-A"
    assert rows.loc["A", "unit"] == "шт"
    assert rows.loc["H", "unit"] == "шт"
    assert rows.loc["C", "unit"] == "не указана"
    assert any("unit: неоднозначные" in w for w in catalog.metadata["warnings"])


def test_confirmed_missing_requisites_survive_approval_export_and_reload(tmp_path):
    store = Store(tmp_path / "orders.db")
    row = dict(code="A", name="Product", article="", unit="не указана", cost=10, free_stock=0, category="C")
    row.update({f"m_2025-{month:02d}": 100 for month in range(1, 13)})
    computed = compute_orders(pd.DataFrame([row]), None, {"A": 1}, "S")
    snapshot = dict(
        lines=[computed.lines[0].as_dict()],
        supplier_key="systeme",
        metadata={"version": "v1"},
        parameters={"budget": 10000},
    )
    plan = store.save_plan(snapshot)
    with pytest.raises(ValueError):
        store.create_order(plan, [dict(code="A", quantity=12, unit_cost=10)], "")
    order = store.create_order(
        plan,
        [dict(code="A", quantity=12, unit_cost=10, article="CONFIRMED-A", unit="шт")],
        "Checked against supplier catalog",
    )
    assert order["lines"][0]["manual_requisites"]["article"] == {"source": "", "confirmed": "CONFIRMED-A"}
    assert store.get("plans", plan["plan_id"])["lines"][0]["article"] == ""
    approved = store.approve(order["id"], "Reviewer", True)
    csv = export_csv(approved).decode("utf-8-sig")
    assert "CONFIRMED-A" in csv
    assert Store(tmp_path / "orders.db").get("orders", order["id"])["lines"][0]["unit"] == "шт"
    with pytest.raises(ValueError):
        store.create_order(plan, [dict(code="A", quantity=12, unit_cost=10, article=" ", unit="шт")], "")
