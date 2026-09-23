"""Order Planner API — автоматический расчёт заказов поставщикам (кейс Электрокомплект).

Сценарий: менеджер запускает расчёт по поставщику -> получает список
рекомендованных заказов с обоснованием -> проверяет -> утверждает (экспорт).

LLM-резюме опционально (fallback без ключа). Расчёт детерминирован и объясним.
"""
from __future__ import annotations

import logging
from functools import lru_cache
from typing import Any

from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .config import get_settings
from .core.loader import load_moq, load_sales_history, load_showcase
from .core.planner import apply_budget, compute_orders
from .datasets import DATASETS, available
from .llm import generate_summary, is_configured

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("planner")

settings = get_settings()
app = FastAPI(title=settings.app_name, version=settings.app_version)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list(),
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


@lru_cache(maxsize=8)
def _load_dataset(key: str):
    """Загрузить и закэшировать набор данных поставщика."""
    ds = DATASETS[key]
    showcase = load_showcase(ds["showcase"])
    history = load_sales_history(ds["history"]) if ds["history"].exists() else None
    moq = load_moq(ds["moq"]) if ds["moq"].exists() else {}
    return showcase, history, moq, ds["name"]


@app.get("/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "version": settings.app_version,
            "llm_configured": is_configured(), "datasets": available()}


@app.get("/api/suppliers")
def suppliers() -> dict[str, Any]:
    return {"suppliers": available()}


@app.get("/api/plan")
def plan(supplier: str = Query(default="systeme"),
         lead_time: float = Query(default=None),
         safety: float = Query(default=None),
         budget: float = Query(default=0.0, description="Бюджетный лимит, ₸ (0 = без лимита)"),
         limit: int = Query(default=100, ge=1, le=1000)) -> JSONResponse:
    """Рассчитать рекомендованные заказы по поставщику."""
    if supplier not in DATASETS:
        return JSONResponse(status_code=400,
                            content={"error": {"code": "UNKNOWN_SUPPLIER", "message": f"Нет данных: {supplier}"}})
    try:
        showcase, history, moq, name = _load_dataset(supplier)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Ошибка загрузки данных")
        return JSONResponse(status_code=500,
                            content={"error": {"code": "LOAD_ERROR", "message": str(exc)}})

    result = compute_orders(
        showcase, history, moq, supplier=name,
        lead_time=lead_time if lead_time else settings.lead_time_months,
        safety=safety if safety else settings.safety_months,
    )
    # доп. функция 2: бюджетный лимит (PRD 6.3.2)
    if budget and budget > 0:
        apply_budget(result, budget)

    payload = result.as_dict()
    # агрегаты по бюджету и перераспределению для дашборда
    payload["budget"] = budget
    payload["within_budget"] = sum(1 for ln in result.lines if ln.in_budget)
    payload["transfers"] = sum(1 for ln in result.lines if ln.transfer_from)
    payload["lines"] = payload["lines"][:limit]

    payload["ai_summary"], payload["ai_source"] = _summary(result)
    return JSONResponse(content=payload, headers={"Cache-Control": "no-store"})


@app.get("/api/whatif")
def whatif(supplier: str = Query(default="systeme")) -> JSONResponse:
    """Доп. функция 3: what-if сценарии (PRD 6.3.3).
    Заранее просчитанные сценарии тем же модулем с изменёнными параметрами.
    """
    if supplier not in DATASETS:
        return JSONResponse(status_code=400,
                            content={"error": {"code": "UNKNOWN_SUPPLIER", "message": f"Нет данных: {supplier}"}})
    showcase, history, moq, name = _load_dataset(supplier)
    base_lead = settings.lead_time_months

    scenarios = []
    # 1. Базовый
    r0 = compute_orders(showcase, history, moq, supplier=name, lead_time=base_lead)
    scenarios.append({"key": "base", "title": "Базовый сценарий",
                      "orders": r0.as_dict()["orders_count"], "cost": r0.as_dict()["total_cost"],
                      "deficit": r0.as_dict()["deficit_count"]})
    # 2. Поставщик задерживает на 2 недели (lead +0.5 мес) -> больше страховой запас/заказ
    r1 = compute_orders(showcase, history, moq, supplier=name, lead_time=base_lead + 0.5)
    scenarios.append({"key": "delay", "title": "Поставщик задерживает +2 недели",
                      "orders": r1.as_dict()["orders_count"], "cost": r1.as_dict()["total_cost"],
                      "deficit": r1.as_dict()["deficit_count"]})
    # 3. Спрос +20% (симулируем через удлинение срока покрытия — больше заказ)
    r2 = compute_orders(showcase, history, moq, supplier=name, lead_time=base_lead * 1.2)
    scenarios.append({"key": "demand_up", "title": "Спрос +20%",
                      "orders": r2.as_dict()["orders_count"], "cost": r2.as_dict()["total_cost"],
                      "deficit": r2.as_dict()["deficit_count"]})

    return JSONResponse(content={"supplier": name, "scenarios": scenarios},
                        headers={"Cache-Control": "no-store"})


def _summary(result) -> tuple[str, str]:
    """Резюме плана для менеджера: LLM или шаблон."""
    urgent = sum(1 for line in result.lines if line.urgency == "Высокая")
    total_units = sum(line.recommended_qty for line in result.lines)
    top = result.lines[:5]
    if is_configured():
        top_text = "\n".join(f"- {line.name[:40]}: {line.recommended_qty:.0f} шт ({line.urgency})" for line in top)
        prompt = (
            f"План закупки для «{result.supplier}». Позиций к заказу: {len(result.lines)}, "
            f"из них срочных: {urgent}, суммарно {total_units:.0f} шт. Топ позиций:\n{top_text}\n\n"
            "Дай менеджеру по закупкам краткое резюме (3-4 предложения): на что обратить внимание "
            "в первую очередь, есть ли риск дефицита, что проверить перед утверждением заказа."
        )
        text = generate_summary(prompt)
        if text:
            return text, "llm"
    # fallback
    txt = (f"По поставщику «{result.supplier}» рекомендовано {len(result.lines)} позиций к заказу, "
           f"из них {urgent} срочных (общий объём {total_units:.0f} шт). "
           "Начните с позиций высокой срочности — по ним остаток ниже потребности на срок поставки. "
           "Разовые крупные продажи исключены из регулярного спроса.")
    return txt, "template"
