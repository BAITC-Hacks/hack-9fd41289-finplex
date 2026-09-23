import copy
import json

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app.core.planner import compute_orders
from app.datasets import Dataset
from app.orders import Store, export_exchange
from app.warehouse_data import WarehouseBundle, load_warehouses


def bundle():
    item = dict(
        code="001",
        stock=10,
        reserved=5,
        cost=12.5,
        transit_lots=[],
        monthly_sales={f"2025-{m:02d}": 100 for m in range(1, 13)},
    )
    return dict(
        schema_version=1,
        as_of="2026-01-01",
        warehouses=[
            dict(name="Астана", items=[item]),
            dict(name="Алматы", items=[dict(copy.deepcopy(item), stock=1000)]),
        ],
    )


def load(tmp_path, data):
    path = tmp_path / "warehouses.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    catalog = pd.DataFrame(
        [
            dict(
                code="001",
                name="Товар",
                article="001-A",
                unit="шт",
                category="C",
                warehouse="Все склады (сводно)",
                free_stock=99999,
                cost=999,
                **{"m_2025-01": 99999},
            )
        ]
    )
    history = pd.DataFrame(columns=["code", "warehouse", "date", "qty"])
    history["date"] = pd.to_datetime(history.date)
    metadata = dict(version="v1", sources=[], warnings=[], products=1)
    scopes = load_warehouses(path, catalog, history, {"001": 1}, metadata)
    return Dataset(catalog, history, {"001": 1}, metadata, scopes)


def state(ds, name="Астана"):
    s = ds.warehouses[name]
    return compute_orders(s.showcase, s.history, s.moq, "S").states[0]


def test_scopes_do_not_share_stock_sales_or_prices(tmp_path):
    ds = load(tmp_path, bundle())
    a, b = state(ds), state(ds, "Алматы")
    assert a.free_stock == 5 and a.cost == 12.5
    assert a.base_demand == pytest.approx(100)
    assert a.recommended_qty > 0 and b.recommended_qty == 0
    assert a.warehouse == "Астана"
    data = bundle()
    data["warehouses"][0]["items"][0]["transit_lots"] = [{"qty": 80, "eta": "2026-01-02"}]
    assert state(load(tmp_path, data)).recommended_qty < a.recommended_qty
    assert state(load(tmp_path, data), "Алматы").recommended_qty == b.recommended_qty


def test_stockout_and_reservation_affect_warehouse_order(tmp_path):
    base = state(load(tmp_path, bundle()))
    data = bundle()
    data["warehouses"][0]["items"][0]["stockout_days"] = {"2025-12": 15}
    adjusted = state(load(tmp_path, data))
    assert adjusted.stockout_adj > 0 and adjusted.recommended_qty > base.recommended_qty
    data = bundle()
    data["warehouses"][0]["items"][0]["reserved"] = 0
    assert state(load(tmp_path, data)).recommended_qty < base.recommended_qty


@pytest.mark.parametrize(
    "mutation",
    [
        lambda d: d["warehouses"][0]["items"].append(copy.deepcopy(d["warehouses"][0]["items"][0])),
        lambda d: d["warehouses"][0]["items"][0].update(reserved=11),
        lambda d: d["warehouses"][0]["items"][0]["monthly_sales"].pop("2025-06"),
        lambda d: d["warehouses"][0]["items"][0].update(stockout_days={"2025-02": 29}),
        lambda d: d["warehouses"][0]["items"][0].update(cost=float("nan")),
        lambda d: d["warehouses"][0]["items"][0].update(customer_name="Private"),
        lambda d: d["warehouses"][0]["items"][0]["monthly_sales"].update({"2026-01": 1}),
    ],
)
def test_invalid_warehouse_data_is_rejected(mutation):
    data = bundle()
    mutation(data)
    with pytest.raises(ValueError):
        WarehouseBundle.model_validate(data)


def test_unknown_code_not_silently_dropped(tmp_path):
    data = bundle()
    data["warehouses"][0]["items"][0]["code"] = "unknown"
    with pytest.raises(ValueError, match="отсутствует"):
        load(tmp_path, data)


def test_http_scope_draft_approval_and_machine_export(tmp_path, monkeypatch):
    import app.main as main

    ds = load(tmp_path, bundle())
    monkeypatch.setattr(main, "dataset", lambda key: ds)
    monkeypatch.setattr(main, "store", Store(tmp_path / "orders.db"))
    monkeypatch.setattr(main.settings, "api_key", "")
    monkeypatch.setattr(main.settings, "llm_enabled", False)
    client = TestClient(main.app)
    assert "Астана" in client.get("/api/options").json()["warehouses"]
    response = client.post("/api/plans", json={"warehouse": "Астана"})
    assert response.status_code == 200, response.text
    plan = response.json()
    assert plan["lines"][0]["free_stock"] == 5
    draft = client.post(
        "/api/orders", json={"plan_id": plan["plan_id"], "lines": [{"code": "001", "quantity": 10}]}
    ).json()
    endpoint = f"/api/orders/{draft['id']}/exchange"
    assert client.get(endpoint).status_code == 422
    assert (
        client.post(
            f"/api/orders/{draft['id']}/approve", json={"reviewer": "Менеджер", "verified_inputs": True}
        ).status_code
        == 200
    )
    exported = client.get(endpoint).json()
    assert exported["external_id"] == draft["id"]
    assert exported["total"] == "125.00" and exported["currency"] == "KZT"
    assert exported["lines"][0]["code_1c"] == "001"
    assert exported["lines"][0]["warehouse"] == "Астана"
    assert exported == client.get(endpoint).json()
    ds.metadata["version"] = "v2"
    assert (
        client.post(
            "/api/orders", json={"plan_id": plan["plan_id"], "lines": [{"code": "001", "quantity": 10}]}
        ).status_code
        == 409
    )


def test_machine_export_requires_approval():
    with pytest.raises(ValueError):
        export_exchange({"status": "draft"})
