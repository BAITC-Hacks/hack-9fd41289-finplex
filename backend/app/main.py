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
from .core.planner import compute_orders
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
    payload = result.as_dict()
    payload["lines"] = payload["lines"][:limit]

    # LLM-резюме (или fallback-шаблон)
    payload["ai_summary"], payload["ai_source"] = _summary(result)
    return JSONResponse(content=payload, headers={"Cache-Control": "no-store"})


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
