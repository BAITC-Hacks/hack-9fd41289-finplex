"""SQLite snapshots and orders. Approval is atomic and never sends supplier mail."""

from __future__ import annotations

import csv
import io
import json
import sqlite3
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path


def now():
    return datetime.now(UTC).isoformat()


class Store:
    def __init__(self, path):
        self.path = Path(path)

    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=20)
        conn.execute("CREATE TABLE IF NOT EXISTS plans (id TEXT PRIMARY KEY, data TEXT NOT NULL)")
        conn.execute("CREATE TABLE IF NOT EXISTS orders (id TEXT PRIMARY KEY, data TEXT NOT NULL)")
        return conn

    def save_plan(self, data):
        plan = dict(data, plan_id=uuid.uuid4().hex, created_at=now())
        with self.connect() as db:
            db.execute(
                "INSERT INTO plans VALUES (?,?)",
                (plan["plan_id"], json.dumps(plan, ensure_ascii=False, allow_nan=False)),
            )
        return plan

    def get(self, kind, ident):
        if kind not in ("plans", "orders"):
            raise ValueError("Неверный тип записи")
        with self.connect() as db:
            found = db.execute(f"SELECT data FROM {kind} WHERE id=?", (ident,)).fetchone()
        if not found:
            raise KeyError(ident)
        return json.loads(found[0])

    def create_order(self, plan, requested, note):
        if not requested:
            raise ValueError("Выберите позиции")
        catalog = {line["code"]: line for line in plan["lines"]}
        lines, seen = [], set()
        total = Decimal(0)
        for item in requested:
            code = item["code"]
            if code in seen or code not in catalog:
                raise ValueError("Повторяющаяся позиция или товар отсутствует в плане")
            seen.add(code)
            original = catalog[code]
            article = (item.get("article") if item.get("article") is not None else original["article"]).strip()
            unit = (item.get("unit") if item.get("unit") is not None else original["unit"]).strip()
            if unit in ("", "не указана") or not article:
                raise ValueError(f"{code}: укажите подтверждённые артикул и единицу измерения")
            qty = Decimal(str(item["quantity"]))
            step = Decimal(str(original["moq"]))
            minimum = Decimal(str(original["min_qty"]))
            price_value = item.get("unit_cost") if item.get("unit_cost") is not None else original["cost"]
            if price_value is None:
                raise ValueError(f"{code}: укажите цену")
            price = Decimal(str(price_value))
            if not qty.is_finite() or qty <= 0 or qty < minimum or qty % step != 0:
                raise ValueError(f"{code}: минимум {minimum}, кратность {step}")
            if not price.is_finite() or price < 1:
                raise ValueError(f"{code}: цена должна быть не ниже 1 ₸")
            cost = (qty * price).quantize(Decimal(".01"))
            total += cost
            corrections = {
                field: {"source": original[field], "confirmed": value}
                for field, value in [("article", article), ("unit", unit)]
                if value != original[field]
            }
            lines.append(
                dict(
                    original,
                    article=article,
                    unit=unit,
                    manual_requisites=corrections,
                    quantity=float(qty),
                    cost=float(price),
                    order_cost=float(cost),
                )
            )
        if plan["parameters"]["budget"] > 0 and total > Decimal(str(plan["parameters"]["budget"])):
            raise ValueError("Выбранные позиции превышают бюджет; измените черновик или пересчитайте план")
        data = {
            "id": uuid.uuid4().hex,
            "plan_id": plan["plan_id"],
            "supplier_key": plan["supplier_key"],
            "source_version": plan["metadata"]["version"],
            "status": "draft",
            "created_at": now(),
            "note": note,
            "total_cost": float(total),
            "lines": lines,
            "parameters": plan["parameters"],
        }
        with self.connect() as db:
            db.execute(
                "INSERT INTO orders VALUES (?,?)", (data["id"], json.dumps(data, ensure_ascii=False, allow_nan=False))
            )
        return data

    def approve(self, ident, reviewer, verified_inputs):
        if not reviewer.strip() or not verified_inputs:
            raise ValueError("Требуется имя ответственного и подтверждение проверки исходных данных")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            record = db.execute("SELECT data FROM orders WHERE id=?", (ident,)).fetchone()
            if not record:
                raise KeyError(ident)
            data = json.loads(record[0])
            if data["status"] == "approved":
                return data  # retry-safe, keep original audit record
            data.update(status="approved", approved_at=now(), reviewer=reviewer.strip(), verified_inputs=True)
            db.execute("UPDATE orders SET data=? WHERE id=?", (json.dumps(data, ensure_ascii=False), ident))
        return data

    def list_orders(self):
        with self.connect() as db:
            rows = db.execute("SELECT data FROM orders ORDER BY rowid DESC LIMIT 100").fetchall()
        return [{k: v for k, v in json.loads(r[0]).items() if k != "lines"} for r in rows]


def export_csv(order):
    if order["status"] != "approved":
        raise ValueError("Экспорт доступен после утверждения")
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, delimiter=";", lineterminator="\r\n")
    writer.writerow(
        [
            "Код 1С",
            "Артикул поставщика",
            "Поставщик",
            "Наименование",
            "Ед.",
            "Количество",
            "Цена",
            "Сумма",
            "Обоснование",
            "Срочность",
            "Склад",
        ]
    )

    def safe(value):
        text = str(value)
        return "'" + text if text.lstrip().startswith(("=", "+", "-", "@", "\t", "\r", "\n")) else text

    for line in order["lines"]:
        writer.writerow(
            [
                safe(line["code"]),
                safe(line["article"]),
                safe(line["supplier"]),
                safe(line["name"]),
                safe(line["unit"]),
                line["quantity"],
                line["cost"],
                line["order_cost"],
                safe(line["reason"]),
                line["urgency"],
                safe(line.get("warehouse", "Все склады (сводно)")),
            ]
        )
    return ("\ufeff" + stream.getvalue()).encode("utf-8")


def export_exchange(order):
    """Versioned exchange contract, not a configuration-specific 1C document."""
    if order["status"] != "approved":
        raise ValueError("Экспорт доступен после утверждения")
    return {
        "schema": "finplex.purchase-order",
        "schema_version": 1,
        "external_id": order["id"],
        "supplier_key": order["supplier_key"],
        "currency": "KZT",
        "status": "approved",
        "approved_at": order["approved_at"],
        "reviewer": order["reviewer"],
        "source_version": order["source_version"],
        "total": format(Decimal(str(order["total_cost"])), ".2f"),
        "tax_treatment": "unspecified",
        "lines": [
            {
                "code_1c": line["code"],
                "supplier_article": line["article"],
                "name": line["name"],
                "unit": line["unit"],
                "warehouse": line.get("warehouse", "Все склады (сводно)"),
                "quantity": str(Decimal(str(line["quantity"]))),
                "unit_price": str(Decimal(str(line["cost"]))),
                "amount": format(Decimal(str(line["order_cost"])), ".2f"),
                "reason": line["reason"],
                "manual_requisites": line.get("manual_requisites", {}),
            }
            for line in order["lines"]
        ],
    }
