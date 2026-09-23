"""Regression tests for the code review: outcomes, not implementation mirrors."""

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app.core.alerts import AlertEngine, AlertSettings
from app.core.loader import load_moq, load_sales_history
from app.core.planner import apply_budget, compute_orders
from app.orders import Store, export_csv


def frame(values=None, **kwargs):
    values = values or [100] * 24
    dates = pd.period_range("2024-01", periods=len(values), freq="M").astype(str)
    row = dict(
        code="A",
        name="Product",
        article="SUP-A",
        unit="шт",
        category="C",
        free_stock=0,
        cost=10,
        **{"m_" + p: v for p, v in zip(dates, values, strict=False)},
    )
    row.update(kwargs)
    return pd.DataFrame([row])


def result(f=None, history=None, rules=None, **params):
    return compute_orders(frame() if f is None else f, history, rules or {"A": 1}, "S", **params)


def line(f=None, **params):
    return result(f, **params).states[0]


def test_constant_spike_cannot_inflate_order():
    base = line(frame([100] * 12))
    spike = line(frame([100] * 11 + [5000]))
    assert spike.recommended_qty <= base.recommended_qty * 1.1
    assert spike.spike_removed > 4800


def test_recurring_season_is_not_removed_and_forecast_has_calendar():
    winter = line(frame(([300] * 3 + [100] * 9) * 2), as_of="2025-12-31")
    summer = line(frame(([100] * 6 + [300] * 3 + [100] * 3) * 2), as_of="2025-12-31")
    assert winter.spike_removed == summer.spike_removed == 0
    assert winter.forecast[1] > summer.forecast[1] * 1.5
    quarter = line(frame([10] * 9 + [100] * 3))
    assert quarter.spike_removed == 0


def test_transaction_removal_changes_regular_demand_and_keeps_date():
    # A large deal sits inside a non-extreme monthly total, so monthly filtering alone cannot find it.
    f = frame([100] * 24)
    tx = pd.DataFrame(
        {
            "code": ["A"] * 7,
            "date": pd.to_datetime(["2025-06-01"] * 7),
            "qty": [1, 1, 1, 1, 1, 1, 80],
            "document": [str(i) for i in range(7)],
        }
    )
    cleaned = result(f, tx).states[0]
    assert cleaned.cleaned_monthly[17] < cleaned.monthly[17]
    assert cleaned.base_demand < line(f).base_demand
    assert cleaned.spike_removed > 0


def test_split_customer_purchase_detected():
    tx = pd.DataFrame(
        {
            "code": ["A"] * 20,
            "date": pd.to_datetime(["2025-06-01"] * 20),
            "qty": [1] * 10 + [4] * 10,
            "document": [str(i) for i in range(20)],
            "customer_id": [f"anon-{i}" for i in range(10)] + ["anon-big"] * 10,
        }
    )
    by_customer = result(frame(), tx).states[0]
    by_document = result(frame(), tx.drop(columns="customer_id")).states[0]
    assert by_customer.spike_removed > by_document.spike_removed


def test_zero_sales_not_stockout():
    f = frame([100] * 12 + [100] * 2 + [0] * 2 + [100] * 8)
    assert line(f).stockout_adj == 0
    f["stockout_days"] = [{"2025-03": 31, "2025-04": 30}]
    assert line(f).stockout_adj > 0


def test_partial_confirmed_stockout_increases_need():
    f = frame([100] * 23 + [50])
    baseline = line(f)
    f["stockout_days"] = [{"2025-12": 15}]
    adjusted = line(f)
    assert adjusted.stockout_adj > 0
    assert adjusted.recommended_qty > baseline.recommended_qty


def test_partial_current_month_excluded_from_training():
    a = line(frame([100] * 24 + [10]), as_of="2026-01-10")
    b = line(frame([100] * 24 + [90]), as_of="2026-01-10")
    assert a.recommended_qty == b.recommended_qty


def test_source_parameters_and_category_policy_have_effect():
    f = frame()
    baseline = line(f)
    assert line(frame(growth_coef=0.2)).recommended_qty > baseline.recommended_qty
    assert line(frame(in_transit=100)).recommended_qty < baseline.recommended_qty
    assert line(frame(free_stock=100)).recommended_qty < baseline.recommended_qty
    assert line(f, safety=2).recommended_qty > line(f, safety=0).recommended_qty
    f.attrs["category_policies"] = {"C": {"safety_multiplier": 2}}
    assert line(f).recommended_qty > baseline.recommended_qty
    f.attrs["supplier_seasonality"] = [2] * 3 + [0.66666667] * 9
    assert line(f).forecast[1] > baseline.forecast[1]


def test_stable_growth_is_estimated_when_forecast_not_supplied():
    assert line(frame([100] * 12 + [130] * 12)).growth > 0


def test_late_or_unknown_transit_does_not_mask_shortage():
    late = line(frame(transit_lots=[{"qty": 10000, "eta": "2027-01-01"}]))
    unknown = line(frame(in_transit=10000))
    assert late.eligible_transit == 0 and late.recommended_qty > 0
    assert unknown.recommended_qty == 0 and unknown.urgency == "Высокая"


def test_minimum_and_pack_are_distinct():
    ln = result(frame([10] * 24), rules={"A": {"pack_size": 6, "min_qty": 100}}).lines[0]
    assert ln.recommended_qty >= 100 and ln.recommended_qty % 6 == 0


@pytest.mark.parametrize(
    "params", [{"lead_time": -1}, {"lead_time": float("nan")}, {"safety": float("inf")}, {"demand_multiplier": -1}]
)
def test_invalid_planner_parameters(params):
    with pytest.raises(ValueError):
        result(**params)


def test_unknown_price_does_not_fit_budget():
    r = result(frame(cost=None))
    apply_budget(r, 100000)
    assert r.as_dict()["unpriced_count"] == 1 and not r.lines[0].in_budget


def test_alert_recovery_rearms_real_planner():
    engine = AlertEngine()
    settings = AlertSettings(email_enabled=False)
    assert len(engine.evaluate([x.as_dict() for x in result().states], settings)) == 1
    recovered = result(frame(free_stock=10000))
    assert len(engine.evaluate([x.as_dict() for x in recovered.states], settings)) == 1
    assert len(engine.evaluate([x.as_dict() for x in result().states], settings)) == 1
    muted = AlertEngine().evaluate([x.as_dict() for x in result().states], AlertSettings(muted_categories={"C"}))
    assert muted == []


def test_threshold_crossing_without_risk_level_change():
    engine = AlertEngine()
    settings = AlertSettings(days_threshold=5, email_enabled=False)
    ln = dict(code="A", name="P", urgency="Средняя", detail={"coverage_months": 0.3, "free_stock": 1})
    assert engine.evaluate([ln], settings) == []
    ln["detail"]["coverage_months"] = 0.1
    assert engine.evaluate([ln], settings)[0].kind == "threshold"
    assert engine.evaluate([ln], settings) == []


def test_moq_headers_first_row(tmp_path):
    for key, label in [("Номенклатура.Код", "Кратность"), ("Код 1с", "Мин. разр. к отгр.")]:
        path = tmp_path / "moq.xlsx"
        pd.DataFrame({key: ["A"], label: [12]}).to_excel(path, index=False)
        rules = load_moq(path)
        assert rules["A"]["min_qty"] == 12
        assert rules["A"]["pack_size"] == (12 if label == "Кратность" else 1)


def test_sales_signs_returns_and_rejected_rows(tmp_path):
    path = tmp_path / "sales.xlsx"
    pd.DataFrame(
        {
            "Дата": ["01.01.2025"] * 4,
            "Код": ["A"] * 4,
            "Количество": [10, -2, 3, 999],
            "Документ": ["Расходная накладная 1", "Расходная накладная 2", "Возврат от клиента", "Неизвестный"],
            "Склад": ["S"] * 4,
        }
    ).to_excel(path, index=False)
    h = load_sales_history(path)
    assert h.qty.tolist() == [10, -2, -3] and h.attrs["quality"]["rejected_rows"] == 1


def plan_snapshot():
    ln = line().as_dict()
    return dict(
        lines=[ln, dict(ln, code="B", article="SUP-B")],
        supplier_key="systeme",
        metadata={"version": "v1"},
        parameters={"budget": 10000},
    )


def test_persistent_approval_export_selected_only_and_budget(tmp_path):
    store = Store(tmp_path / "orders.db")
    p = store.save_plan(plan_snapshot())
    order = store.create_order(p, [{"code": "B", "quantity": 12, "unit_cost": 20}], "reviewed")
    with pytest.raises(ValueError):
        export_csv(order)
    reopened = Store(tmp_path / "orders.db").get("orders", order["id"])
    assert reopened["status"] == "draft" and len(reopened["lines"]) == 1
    approved = store.approve(order["id"], "Manager", True)
    csv = export_csv(approved).decode("utf-8-sig")
    assert "SUP-B" in csv and "SUP-A" not in csv and "12.0" in csv
    assert store.approve(order["id"], "Someone else", True)["reviewer"] == "Manager"
    with pytest.raises(ValueError):
        store.create_order(p, [{"code": "B", "quantity": 2000, "unit_cost": 20}], "")
    with pytest.raises(ValueError):
        store.approve(order["id"], "Manager", False)


def test_csv_formula_injection_and_quoting(tmp_path):
    store = Store(tmp_path / "orders.db")
    p = plan_snapshot()
    p["lines"][0]["name"] = '=HYPERLINK("evil");x'
    saved = store.save_plan(p)
    o = store.create_order(saved, [{"code": "A", "quantity": 1, "unit_cost": 1}], "")
    csv = export_csv(store.approve(o["id"], "Manager", True)).decode("utf-8-sig")
    assert "'=HYPERLINK" in csv and '""evil""' in csv


@pytest.fixture
def api_client(tmp_path, monkeypatch):
    import app.main as main
    from app.datasets import Dataset

    f = frame()
    f.attrs["as_of"] = "2025-12-31"
    ds = Dataset(
        f,
        None,
        {"A": {"pack_size": 6, "min_qty": 6}},
        {"version": "v1", "as_of": "2025-12-31", "warnings": [], "sources": [], "products": 1},
    )
    monkeypatch.setattr(main, "dataset", lambda key: ds)
    monkeypatch.setattr(main, "store", Store(tmp_path / "api.db"))
    monkeypatch.setattr(main.settings, "llm_enabled", False)
    monkeypatch.setattr(main.settings, "api_key", "")
    return TestClient(main.app), ds


def test_http_selected_draft_approval_export_and_stale_version(api_client):
    client, ds = api_client
    p = client.post("/api/plans", json={"supplier": "systeme"}).json()
    o = client.post(
        "/api/orders", json={"plan_id": p["plan_id"], "lines": [{"code": "A", "quantity": 12, "unit_cost": 100}]}
    ).json()
    assert client.get(f"/api/orders/{o['id']}/export").status_code == 422
    assert (
        client.post(f"/api/orders/{o['id']}/approve", json={"reviewer": "M", "verified_inputs": True}).status_code
        == 200
    )
    assert client.get(f"/api/orders/{o['id']}/export").status_code == 200
    ds.metadata["version"] = "v2"
    assert (
        client.post("/api/orders", json={"plan_id": p["plan_id"], "lines": [{"code": "A", "quantity": 12}]}).status_code
        == 409
    )


@pytest.mark.parametrize("value", [-1, 0, 13, "NaN", "Infinity"])
def test_http_rejects_invalid_lead(api_client, value):
    client, _ = api_client
    assert client.get("/api/plan", params={"lead_time": value}).status_code == 422


def test_http_enforces_api_key(api_client, monkeypatch):
    import app.main as main

    client, _ = api_client
    monkeypatch.setattr(main.settings, "api_key", "secret-test-key")
    assert client.get("/api/orders").status_code == 401
    assert client.get("/api/orders", headers={"X-API-Key": "secret-test-key"}).status_code == 200
    assert client.get("/health").status_code == 200


def test_whatif_demand_changes_demand_not_lead(api_client, monkeypatch):
    import app.main as main

    seen = []
    original = main.compute_orders

    def capture(*args, **kwargs):
        seen.append(kwargs)
        return original(*args, **kwargs)

    monkeypatch.setattr(main, "compute_orders", capture)
    response = api_client[0].post(
        "/api/whatif", json={"supplier": "systeme", "lead_time": 2, "safety": 0.2, "budget": 500}
    )
    assert response.status_code == 200
    assert [p["lead_time"] for p in seen] == [2, 2.5, 2]
    assert seen[2]["demand_multiplier"] == 1.2 and all(p["safety"] == 0.2 for p in seen)


def test_source_version_changes_with_file(tmp_path, monkeypatch):
    import app.datasets as ds

    monkeypatch.setattr(ds, "DATA_DIR", tmp_path)
    monkeypatch.setitem(ds.DATASETS, "test", {"files": ["source.xlsx"]})
    (tmp_path / "test").mkdir()
    p = tmp_path / "test/source.xlsx"
    p.write_bytes(b"one")
    first = ds.signature("test")
    p.write_bytes(b"longer")
    assert first != ds.signature("test")


def test_http_does_not_truncate_at_400(api_client):
    client, ds = api_client
    ds.showcase = pd.concat([frame(code=f"P{i}") for i in range(405)], ignore_index=True)
    ds.showcase.attrs["as_of"] = "2025-12-31"
    response = client.post("/api/plans", json={"supplier": "systeme"})
    assert response.status_code == 200
    plan = response.json()
    assert len(plan["lines"]) == plan["orders_count"] == 405


def test_conflicting_moq_rejected_identical_rule_deduplicated(tmp_path):
    p = tmp_path / "moq.xlsx"
    pd.DataFrame({"Код 1с": ["A", "A"], "Наименование": ["old", "new"], "Мин. разр. к отгр.": [6, 6]}).to_excel(
        p, index=False
    )
    assert len(load_moq(p)) == 1
    pd.DataFrame({"Код 1с": ["A", "A"], "Мин. разр. к отгр.": [6, 12]}).to_excel(p, index=False)
    with pytest.raises(ValueError):
        load_moq(p)


def test_approval_rechecks_source_version(api_client):
    client, ds = api_client
    p = client.post("/api/plans", json={}).json()
    order = client.post("/api/orders", json={"plan_id": p["plan_id"], "lines": [{"code": "A", "quantity": 12}]}).json()
    ds.metadata["version"] = "changed"
    assert (
        client.post(f"/api/orders/{order['id']}/approve", json={"reviewer": "M", "verified_inputs": True}).status_code
        == 409
    )


def test_http_rejects_changed_qty_breaking_pack(api_client):
    client, _ = api_client
    p = client.post("/api/plans", json={}).json()
    assert (
        client.post("/api/orders", json={"plan_id": p["plan_id"], "lines": [{"code": "A", "quantity": 7}]}).status_code
        == 422
    )


def test_recurring_wholesale_not_deleted_without_customer_id():
    rows = []
    for month in range(1, 13):
        for i, qty in enumerate([1] * 20 + [100, 110]):
            rows.append({"code": "A", "date": pd.Timestamp(2025, month, 1), "qty": qty, "document": f"{month}-{i}"})
    history = pd.DataFrame(rows)
    f = frame([230] * 24)
    ordinary = result(f, history).states[0]
    assert ordinary.spike_removed == 0
    huge = pd.DataFrame([{"code": "A", "date": pd.Timestamp(2025, 6, 2), "qty": 10000, "document": "unique"}])
    assert result(f, pd.concat([history, huge], ignore_index=True)).states[0].spike_removed > 0
