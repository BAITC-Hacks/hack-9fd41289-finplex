"""Validated adapters for partner Excel exports; missing inventory is not zero."""

from __future__ import annotations

import math
import re
from pathlib import Path

import pandas as pd

MONTH_NAMES = ["янв", "фев", "мар", "апр", "май", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"]


def _num(value, default=0.0):
    if pd.isna(value) or str(value).strip() in ("", "-"):
        return default
    try:
        result = float(str(value).replace("\xa0", "").replace(" ", "").replace(",", "."))
    except (ValueError, TypeError) as exc:
        raise ValueError(f"Некорректное число: {value!r}") from exc
    if not math.isfinite(result):
        raise ValueError("Число должно быть конечным")
    return result


def month_key(value):
    text = str(value).lower().strip()
    match = re.search(r"(20\d{2})", text)
    if not match:
        return None
    for index, name in enumerate(MONTH_NAMES, 1):
        if text.startswith(name) or (index == 5 and text.startswith("мая")):
            return f"{match[1]}-{index:02d}"
    return None


def table(path: Path, required: str, sheet=0):
    raw = pd.read_excel(path, sheet_name=sheet, header=None)
    for index, row in raw.head(8).iterrows():
        names = [str(x).strip() for x in row]
        if required in names:
            result = raw.iloc[index + 1 :].copy()
            result.columns = names
            return result
    raise ValueError(f"{path.name}: не найден заголовок {required}")


def unique_codes(df, column, path):
    df = df.drop_duplicates()
    df = df[df[column].notna()].copy()
    df["code"] = df[column].astype(str).str.strip()
    df = df[~df.code.isin(["", "Итого", "nan"])]
    if df.code.duplicated().any():
        raise ValueError(f"{path.name}: повторяющиеся коды товара")
    return df


def load_showcase(path: Path):
    df = unique_codes(table(path, "Код 1с", "TDSheet"), "Код 1с", path)
    out = pd.DataFrame({"code": df.code})
    for target, source in [("article", "Артикул поставщика"), ("name", "Наименование"), ("category", "Категория 2026")]:
        out[target] = df[source].fillna("").astype(str).str.strip()
    for target, source in [("cost", "СС реал"), ("growth_coef", "Кэф. Роста"), ("free_stock", "Свободный остаток")]:
        if source not in df:
            raise ValueError(f"{path.name}: отсутствует {source}")
        out[target] = df[source].map(lambda x: _num(x, None))
    transit = [c for c in df if "в пути" in c.lower()]
    if len(transit) != 1:
        raise ValueError(f"{path.name}: неоднозначная колонка товара в пути")
    out["in_transit"] = df[transit[0]].map(_num)
    out["transit_lots"] = out.in_transit.map(lambda q: [{"qty": q, "eta": None}] if q else [])
    for col in df:
        period = month_key(col)
        if period:
            out[f"m_{period}"] = df[col].map(_num)
    return out.reset_index(drop=True)


def load_monthly(path: Path, prefix="m_"):
    df = unique_codes(table(path, "Номенклатура.Код"), "Номенклатура.Код", path)
    out = pd.DataFrame({"code": df.code, "name": df["Номенклатура"].astype(str).str.strip()})
    for source, target in [("Артикул", "article"), ("Ед.", "unit"), ("Ед.изм", "unit")]:
        if source in df:
            out[target] = df[source].fillna("").astype(str).str.strip()
    for col in df:
        period = month_key(col)
        if period:
            out[prefix + period] = df[col].map(lambda v: _num(v, None if prefix == "s_" else 0))
    if not any(c.startswith(prefix) for c in out):
        raise ValueError(f"{path.name}: отсутствуют периоды")
    return out.reset_index(drop=True)


def load_sales_history(path: Path):
    df = pd.read_excel(path)
    required = {"Дата", "Код", "Количество", "Документ", "Склад"}
    if not required.issubset(df):
        raise ValueError(f"{path.name}: отсутствуют {sorted(required - set(df))}")
    result = pd.DataFrame(
        {
            "date": pd.to_datetime(df["Дата"], dayfirst=True, format="mixed", errors="coerce"),
            "code": df["Код"].fillna("").astype(str).str.strip(),
            "qty": df["Количество"].map(_num),
            "document": df["Документ"].fillna("").astype(str),
            "warehouse": df["Склад"].fillna("").astype(str),
            "unit": df.get("Ед.", pd.Series("", index=df.index)).fillna("").astype(str),
        }
    )
    expense = result.document.str.startswith("Расходная накладная")
    returns = result.document.str.lower().str.contains("возврат")
    result.loc[returns, "qty"] = -result.loc[returns, "qty"].abs()
    valid = (expense | returns) & result.date.notna() & result.code.ne("")
    result["operation"] = "sale"
    result.loc[result.qty < 0, "operation"] = "return_or_correction"
    if "customer_id" in df:
        result["customer_id"] = df["customer_id"].fillna("").astype(str)
    result = result[valid].copy()
    result.attrs["quality"] = {
        "input_rows": len(df),
        "accepted_rows": len(result),
        "rejected_rows": int((~valid).sum()),
        "correction_rows": int((result.qty < 0).sum()),
        "customer_ids_available": "customer_id" in result,
    }
    return result.reset_index(drop=True)


def load_moq(path: Path):
    raw = pd.read_excel(path, header=None, nrows=8)
    key = "Номенклатура.Код" if raw.isin(["Номенклатура.Код"]).any().any() else "Код 1с"
    df = table(path, key)
    pack = next((c for c in df if "ратность" in c), None)
    minimum = next((c for c in df if "Мин" in c), None)
    if not pack and not minimum:
        raise ValueError(f"{path.name}: нет кратности или минимальной партии")
    # Duplicate display names for the same SKU are harmless only when rules agree.
    df = df[[key] + [c for c in (pack, minimum) if c]].drop_duplicates()
    df = unique_codes(df, key, path)
    rules = {}
    for _, row in df.iterrows():
        step = _num(row[pack], 1) if pack else 1
        qty = _num(row[minimum], 1) if minimum else step
        if step <= 0 or qty <= 0:
            raise ValueError(f"{path.name}: неположительная партия для {row.code}")
        rules[row.code] = {"pack_size": step, "min_qty": qty}
    return rules


def load_transit_iek(path: Path):
    df = table(path, "Код 1с")
    df = df[df["Код 1с"].notna()].copy()
    df["code"] = df["Код 1с"].astype(str).str.strip()
    df["Артикул ИЭК"] = df["Артикул ИЭК"].fillna("").astype(str).str.strip()
    # Repeated catalogue rows may differ in display name/whitespace. Coalesce each
    # shipment column, never sum duplicate copies of the same shipment.
    merged = []
    for code, group in df.groupby("code", sort=False):
        row = group.iloc[0].copy()
        if group["Артикул ИЭК"].nunique() > 1:
            raise ValueError(f"{path.name}: конфликт артикулов {code}")
        for column in df:
            if "поступление до" in column:
                values = group[column].map(_num)
                if values[values != 0].nunique() > 1:
                    raise ValueError(f"{path.name}: конфликт количества в пути {code}")
                row[column] = values.max()
        merged.append(row)
    df = pd.DataFrame(merged)
    rows = []
    for _, row in df.iterrows():
        lots = []
        for column in df:
            match = re.search(r"поступление до (\d{2}\.\d{2}\.\d{4})", column)
            if match:
                qty = _num(row[column])
                if qty < 0:
                    raise ValueError("Отрицательное количество в пути")
                if qty:
                    lots.append({"qty": qty, "eta": pd.to_datetime(match[1], dayfirst=True).date().isoformat()})
        rows.append(
            {
                "code": row.code,
                "article": str(row["Артикул ИЭК"]),
                "name": str(row["Наименование"]),
                "transit_lots": lots,
                "in_transit": sum(x["qty"] for x in lots),
            }
        )
    return pd.DataFrame(rows)


def load_seasonality(path: Path, as_of):
    raw = pd.read_excel(path, header=None)
    profiles = []
    for _, row in raw.iterrows():
        try:
            year = int(row.iloc[0])
        except (ValueError, TypeError):
            continue
        if not 2000 <= year < pd.Timestamp(as_of).year:
            continue
        values = [_num(v) for v in row.iloc[1:13]]
        mean = sum(values) / 12
        if mean > 0:
            profiles.append([v / mean for v in values])
    if not profiles:
        raise ValueError(f"{path.name}: нет полного исторического года сезонности")
    return [sum(p[i] for p in profiles) / len(profiles) for i in range(12)]
