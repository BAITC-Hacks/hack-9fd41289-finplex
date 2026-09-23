"""Validated API: calculate, review a draft, explicitly approve, then export."""

from __future__ import annotations

import hmac
import logging
from typing import Annotated

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from .config import get_settings
from .core.alerts import AlertEngine, AlertSettings
from .core.planner import apply_budget, compute_orders, finite
from .datasets import DATASETS, available, load_dataset
from .llm import generate_summary, is_configured
from .orders import Store, export_csv

settings = get_settings()
app = FastAPI(title=settings.app_name, version=settings.app_version)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list(),
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "X-API-Key"],
)
store = Store(settings.database_path)
ALERTS = AlertEngine()
ALERT_SETTINGS = AlertSettings(email_enabled=False)
logger = logging.getLogger("planner")


@app.middleware("http")
async def authenticate(request: Request, call_next):
    if request.url.path != "/health" and request.method != "OPTIONS" and settings.api_key:
        if not hmac.compare_digest(request.headers.get("X-API-Key", ""), settings.api_key):
            return JSONResponse(status_code=401, content={"detail": "Требуется API-ключ"})
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@app.exception_handler(ValueError)
async def validation_error(request, exc):
    return JSONResponse(status_code=422, content={"detail": str(exc)})


@app.exception_handler(KeyError)
async def not_found(request, exc):
    return JSONResponse(status_code=404, content={"detail": "Запись не найдена"})


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class PlanInput(StrictModel):
    supplier: str = "systeme"
    lead_time: float | None = Field(None, gt=0, le=12)
    safety: float = Field(0.5, ge=0, le=12)
    budget: float = Field(0, ge=0, le=1e15)
    category: str | None = None
    warehouse: str | None = None


class ItemInput(StrictModel):
    code: str
    quantity: float = Field(gt=0, le=1e12)
    unit_cost: float | None = Field(None, ge=1, le=1e12)
    article: str | None = Field(None, min_length=1, max_length=150)
    unit: str | None = Field(None, min_length=1, max_length=40)


class DraftInput(StrictModel):
    plan_id: str
    lines: list[ItemInput] = Field(min_length=1, max_length=20000)
    note: str = Field("", max_length=2000)


class Approval(StrictModel):
    reviewer: str = Field(min_length=1, max_length=150)
    verified_inputs: bool


class AlertInput(StrictModel):
    days_threshold: float = Field(5, ge=0, le=365)
    push_enabled: bool = True
    email_enabled: bool = False
    email_to: str = Field("", max_length=200)
    muted_categories: list[str] = Field(default_factory=list)
    muted_warehouses: list[str] = Field(default_factory=list)


def dataset(key):
    if key not in DATASETS:
        raise HTTPException(404, "Неизвестный поставщик")
    try:
        return load_dataset(key)
    except (OSError, ValueError) as exc:
        logger.exception("Ошибка входных данных")
        raise HTTPException(422, f"Набор данных не загружен: {exc}") from exc


def calculate(body, multiplier=1, delay=0):
    ds = dataset(body.supplier)
    frame = ds.showcase.copy()
    for field in ("category", "warehouse"):
        selected = getattr(body, field)
        if selected:
            if selected not in set(frame[field]):
                raise HTTPException(422, f"Недоступный фильтр {field}: {selected}")
            frame = frame[frame[field] == selected].copy()
    # Fit category profiles on the full dataset so filtering doesn't change forecasts.
    full = ds.showcase
    lead = body.lead_time
    if delay:
        if lead is not None:
            lead += delay
        else:
            full = full.copy()
            full["lead_time"] = full.apply(
                lambda row: finite(row.get("lead_time"), "lead_time", 0.01,
                                   default=full.attrs.get("lead_time", 1.5)) + delay,
                axis=1,
            )
    result = compute_orders(
        full,
        ds.history,
        ds.moq,
        DATASETS[body.supplier]["name"],
        lead_time=lead,
        safety=body.safety,
        demand_multiplier=multiplier,
    )
    codes = set(frame.code)
    result.states = [x for x in result.states if x.code in codes]
    result.lines = [x for x in result.lines if x.code in codes]
    result.excess_count = sum(
        x.recommended_qty == 0 and x.seasonal_demand > 0 and x.free_stock + x.eligible_transit > x.target_stock * 1.5
        for x in result.states
    )
    result.spike_items = sum(x.spike_count > 0 for x in result.states)
    result.spike_units_total = sum(x.spike_removed for x in result.states)
    result.summary = (
        f"{result.supplier}: {len(result.lines)} позиций. Проверьте исходные данные и утвердите заказ вручную."
    )
    apply_budget(result, body.budget)
    return result, ds


@app.get("/health")
def health():
    return {"status": "ok", "version": settings.app_version}


@app.get("/api/suppliers")
def suppliers():
    return {"suppliers": available()}


@app.get("/api/options")
def options(supplier: str = "systeme"):
    ds = dataset(supplier)
    return {
        "categories": sorted(set(ds.showcase.category)),
        "warehouses": sorted(set(ds.showcase.warehouse)),
        "metadata": ds.metadata,
        "lead_time": ds.showcase.attrs.get("lead_time", 1.5),
    }


def make_plan(body):
    result, ds = calculate(body)
    payload = result.as_dict()
    payload.update(
        supplier_key=body.supplier,
        parameters=body.model_dump(),
        metadata=ds.metadata,
        within_budget=sum(x.in_budget for x in result.lines),
    )
    # Only aggregate counts leave the service, and only with LLM_ENABLED=true.
    payload["ai_summary"] = result.summary
    if is_configured():
        prompt = f"Позиций {len(result.lines)}; срочных {payload['deficit_count']}. Кратко напомни менеджеру проверить данные перед утверждением. Не добавляй числа."
        payload["ai_summary"] = generate_summary(prompt) or result.summary
    return store.save_plan(payload)


@app.post("/api/plans")
def create_plan(body: PlanInput):
    return make_plan(body)


@app.get("/api/plan")
def plan(
    supplier: str = "systeme",
    lead_time: Annotated[float | None, Query(gt=0, le=12, allow_inf_nan=False)] = None,
    safety: Annotated[float, Query(ge=0, le=12, allow_inf_nan=False)] = 0.5,
    budget: Annotated[float, Query(ge=0, le=1e15, allow_inf_nan=False)] = 0,
    category: str | None = None,
    warehouse: str | None = None,
):
    # Full snapshot: no silent slicing. Frontend search/export use the same full plan.
    return make_plan(
        PlanInput(
            supplier=supplier, lead_time=lead_time, safety=safety, budget=budget, category=category, warehouse=warehouse
        )
    )


@app.post("/api/whatif")
def whatif(body: PlanInput):
    scenarios = []
    for key, title, multiplier, delay in [
        ("base", "Базовый", 1, 0),
        ("delay", "Задержка +2 недели", 1, 0.5),
        ("demand_up", "Спрос +20%", 1.2, 0),
    ]:
        result, _ = calculate(body, multiplier, delay)
        stats = result.as_dict()
        scenarios.append(
            {
                "key": key,
                "title": title,
                "orders": stats["orders_count"],
                "cost": stats["total_cost"],
                "unpriced_count": stats["unpriced_count"],
                "deficit": stats["deficit_count"],
                "within_budget": sum(x.in_budget for x in result.lines),
            }
        )
    return {"scenarios": scenarios}


def current_version(record):
    if dataset(record["supplier_key"]).metadata["version"] != record["source_version"]:
        raise HTTPException(409, "Данные изменились. Пересчитайте план и создайте новый черновик.")


@app.post("/api/orders", status_code=201)
def create_draft(body: DraftInput):
    snapshot = store.get("plans", body.plan_id)
    current_version({"supplier_key": snapshot["supplier_key"], "source_version": snapshot["metadata"]["version"]})
    return store.create_order(snapshot, [x.model_dump() for x in body.lines], body.note)


@app.get("/api/orders")
def orders():
    return {"orders": store.list_orders()}


@app.get("/api/orders/{ident}")
def get_order(ident: str):
    return store.get("orders", ident)


@app.post("/api/orders/{ident}/approve")
def approve(ident: str, body: Approval):
    record = store.get("orders", ident)
    if record["status"] != "approved":
        current_version(record)
    return store.approve(ident, body.reviewer, body.verified_inputs)


@app.get("/api/orders/{ident}/export")
def export(ident: str):
    data = export_csv(store.get("orders", ident))
    return Response(
        data,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="order-{ident}.csv"'},
    )


@app.get("/api/alerts")
def alerts():
    return {"alerts": ALERTS.feed, "delivery": "push feed; email is log-only demo"}


@app.post("/api/alerts/settings")
def alert_settings(body: AlertInput):
    global ALERT_SETTINGS
    ALERT_SETTINGS = AlertSettings(
        **dict(
            body.model_dump(), muted_categories=set(body.muted_categories), muted_warehouses=set(body.muted_warehouses)
        )
    )
    return {"ok": True}


@app.post("/api/alerts/scan")
def scan(body: PlanInput):
    # Always process the complete supplier scope to observe recovery after replenishment.
    result, _ = calculate(body.model_copy(update={"category": None, "warehouse": None}))
    found = ALERTS.evaluate([x.as_dict() for x in result.states], ALERT_SETTINGS)
    return {"new_alerts": [x.as_dict() for x in found], "total_feed": len(ALERTS.feed)}


@app.post("/api/alerts/reset")
def reset():
    ALERTS.reset()
    return {"ok": True}
