"""LLM-обёртка: OpenAI-совместимый провайдер + graceful fallback.

LLM только формулирует резюме по УЖЕ посчитанному плану заказов. Числа берутся
из расчёта, LLM их не меняет. Без ключа/при ошибке -> None (используется шаблон).
"""
from __future__ import annotations

import logging

import httpx

from .config import get_settings

logger = logging.getLogger("planner.llm")


def is_configured() -> bool:
    s = get_settings()
    return bool(s.llm_enabled and s.llm_api_key)


def generate_summary(prompt: str) -> str | None:
    if not is_configured():
        return None
    s = get_settings()
    url = s.llm_base_url.rstrip("/") + "/chat/completions"
    headers = {"Authorization": f"Bearer {s.llm_api_key}", "Content-Type": "application/json"}
    body = {
        "model": s.llm_model,
        "messages": [
            {"role": "system", "content": "Ты помощник менеджера по закупкам. Отвечай кратко, по-русски, "
                                          "только по данным из запроса. Не выдумывай и не меняй числа."},
            {"role": "user", "content": prompt},
        ],
        "temperature": s.llm_temperature,
        "max_tokens": s.llm_max_output_tokens,
    }
    try:
        with httpx.Client(timeout=s.llm_timeout_seconds) as client:
            resp = client.post(url, headers=headers, json=body)
        if resp.status_code != 200:
            logger.warning("LLM %s: %s", resp.status_code, resp.text[:200])
            return None
        return (resp.json()["choices"][0]["message"]["content"] or "").strip() or None
    except (httpx.HTTPError, KeyError, IndexError, ValueError) as exc:
        logger.warning("LLM недоступен, fallback: %s", exc)
        return None
