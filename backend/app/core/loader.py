"""Загрузчик данных партнёра (ekt.kz): витрина продаж/остатков, история, MOQ.

Данные — выгрузки 1С в Excel с многоуровневыми шапками. Загрузчик приводит их
к чистым таблицам для движка расчёта. Работает с брендом Systeme Electric
(богатая витрина TDSheet), структура обобщаема на ИЭК.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

MONTHS_2026 = [
    "Январь 2026 г.", "Февраль 2026 г.", "Март 2026 г.", "Апрель 2026 г.",
    "Май 2026 г.", "Июнь 2026 г.", "Июль 2026 г.", "Август 2026 г.", "Сентябрь 2026 г.",
]
MONTHS_2025 = [
    "Январь 2025 г.", "Февраль 2025 г.", "Март 2025 г.", "Апрель 2025 г.",
    "Май 2025 г.", "Июнь 2025 г.", "Июль 2025 г.", "Август 2025 г.",
    "Сентябрь 2025 г.", "Октябрь 2025 г.", "Ноябрь 2025 г.", "Декабрь 2025 г.",
]


def _num(v, default=0.0) -> float:
    """Безопасное число из ячейки 1С (строки, пробелы, запятые)."""
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return default
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace("\xa0", "").replace(" ", "").replace(",", ".")
    if not s or s in ("-", "nan"):
        return default
    try:
        return float(s)
    except ValueError:
        return default


def load_showcase(path: Path) -> pd.DataFrame:
    """Витрина TDSheet: по артикулу — помесячные продажи, ср.мес, рост, сезонность,
    свободный остаток, в пути. Заголовки во 2-й строке (header=1).
    Возвращает нормализованный DataFrame по товарам.
    """
    df = pd.read_excel(path, sheet_name="TDSheet", header=1)
    df = df[df["Код 1с"].notna() & (df["Код 1с"].astype(str).str.strip() != "")]

    out = pd.DataFrame()
    out["code"] = df["Код 1с"].astype(str).str.strip()
    out["article"] = df.get("Артикул поставщика", "").astype(str).str.strip()
    out["name"] = df.get("Наименование", "").astype(str).str.strip()
    out["category"] = df.get("Категория 2026", "").astype(str).str.strip()
    out["cost"] = df.get("СС реал", 0).map(_num)

    # Готовые метрики партнёра (используем как эталон/фолбэк)
    out["avg_month_12"] = df.get("   Ср мес за последние 12 мес", df.get("Ср мес за последние 12 мес", 0)).map(_num)
    out["growth_coef"] = df.get("Кэф. Роста", 1).map(_num)
    out["season_coef"] = df.get("Кэф. Сез-ти", 1).map(_num)
    out["free_stock"] = df.get("Свободный остаток", 0).map(_num)
    out["in_transit"] = df.get("СЭ в пути 24.09", 0).map(_num)

    # Помесячные продажи 2025-2026 для собственного расчёта сезонности
    for m in MONTHS_2025 + MONTHS_2026:
        if m in df.columns:
            out[f"m_{m}"] = df[m].map(_num)

    return out.reset_index(drop=True)


def load_sales_history(path: Path) -> pd.DataFrame:
    """Построчная история продаж (Динамика): для детекта разовых всплесков.
    Количество отрицательное = расход (продажа). Возвращает: date, code, qty (положит.).
    """
    df = pd.read_excel(path)
    df = df[["Дата", "Код", "Количество"]].copy()
    df.columns = ["date", "code", "qty"]
    df["date"] = pd.to_datetime(df["date"], dayfirst=True, errors="coerce")
    df["code"] = df["code"].astype(str).str.strip()
    df["qty"] = df["qty"].map(_num)
    # продажи = отрицательные строки (расход), берём модуль
    df = df[df["qty"] < 0].copy()
    df["qty"] = df["qty"].abs()
    df = df[df["date"].notna()]
    return df.reset_index(drop=True)


def load_moq(path: Path) -> dict[str, float]:
    """MOQ (минимальная партия) по коду 1С. header=1."""
    try:
        df = pd.read_excel(path, header=1)
    except Exception:
        return {}
    # ищем колонку с кодом и с кратностью/мин.партией
    code_col = next((c for c in df.columns if "Код" in str(c)), None)
    qty_col = next((c for c in df.columns if "ратн" in str(c) or "Мин" in str(c)), None)
    if not code_col or not qty_col:
        return {}
    moq = {}
    for _, r in df.iterrows():
        code = str(r[code_col]).strip()
        val = _num(r[qty_col], 1)
        if code and code != "nan" and val > 0:
            moq[code] = val
    return moq
