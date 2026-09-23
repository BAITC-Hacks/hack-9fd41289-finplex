"""Проактивные алерты о росте риска дефицита (доп. функция, Roadmap v2 PRD).

Слой уведомлений поверх модуля расчёта (planner). После каждого пересчёта
сравнивает текущий риск по артикулу с предыдущим и формирует алерт при:
  - переходе риска в «высокий» из низкого/среднего, ИЛИ
  - падении «дней до исчерпания» ниже порога пользователя.

Дедупликация: повторный алерт по тому же артикулу не шлётся, пока риск не
изменился. При возврате к норме — уведомление «риск снят».

Каналы: push (в дашборд) и email (демо-режим: письмо формируется и логируется).
Хранилище истории — in-memory (для MVP; в проде — таблица last_alert_level).
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger("alerts")

# Уровни риска и их порядок (для сравнения «стало хуже / лучше»)
RISK_ORDER = {"Плановая": 0, "Средняя": 1, "Высокая": 2}
# соответствие внутренних названий срочности -> уровень риска для пользователя
LEVEL_LABEL = {"Плановая": "низкий", "Средняя": "средний", "Высокая": "высокий"}


@dataclass
class AlertSettings:
    """Настройки пользователя (PRD: порог, отключения, каналы)."""

    days_threshold: float = 5.0  # дней до исчерпания -> алерт (по умолчанию 5)
    push_enabled: bool = True
    email_enabled: bool = True
    email_to: str = "manager@ekt.kz"
    muted_categories: set[str] = field(default_factory=set)
    muted_warehouses: set[str] = field(default_factory=set)


@dataclass
class Alert:
    code: str
    name: str
    supplier: str
    kind: str  # "risk_up" | "threshold" | "cleared"
    level: str  # текущий уровень (низкий/средний/высокий)
    prev_level: str
    days_left: float  # прогноз дней до исчерпания
    current_stock: float
    depletion_date: str  # прогнозируемая дата исчерпания
    deep_link: str  # ссылка на позицию в дашборде
    message: str
    created_at: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _coverage_to_days(coverage_months: float) -> float:
    return round(coverage_months * 30, 1)


def _depletion_date(days_left: float) -> str:
    from datetime import timedelta

    d = datetime.now(UTC) + timedelta(days=days_left)
    return d.strftime("%Y-%m-%d")


class AlertEngine:
    """Хранит снапшот прошлого расчёта и уровень последнего алерта (дедуп)."""

    def __init__(self) -> None:
        # code -> уровень риска на прошлом расчёте
        self._prev_risk: dict[str, str] = {}
        # code -> уровень, по которому уже слали алерт (дедупликация)
        self._last_alerted: dict[str, str] = {}
        self._prev_days: dict[str, float] = {}
        # накопленные push-уведомления (лента в дашборде)
        self.feed: list[dict[str, Any]] = []

    def evaluate(self, lines: list[dict[str, Any]], settings: AlertSettings) -> list[Alert]:
        """Сравнить текущий расчёт с предыдущим, вернуть новые алерты."""
        alerts: list[Alert] = []
        seen_codes = set()

        for ln in lines:
            code = (ln.get("supplier", ""), ln.get("warehouse", "Основной"), ln["code"])
            seen_codes.add(code)
            urgency = ln.get("urgency", "Плановая")
            level = LEVEL_LABEL.get(urgency, "низкий")
            prev_urgency = self._prev_risk.get(code, "Плановая")
            prev_level = LEVEL_LABEL.get(prev_urgency, "низкий")

            detail = ln.get("detail", {})
            days_left = _coverage_to_days(detail.get("coverage_months", 99))
            category = str(ln.get("category", ""))
            # склад определяем по перемещению/по умолчанию (в наших данных один склад)
            warehouse = ln.get("warehouse", "Основной")

            # применяем отключения по категории/складу
            if category in settings.muted_categories or warehouse in settings.muted_warehouses:
                self._prev_risk[code] = urgency
                self._prev_days[code] = days_left
                self._last_alerted.pop(code, None)
                continue

            # --- триггеры ---
            risk_up = RISK_ORDER.get(urgency, 0) == 2 and RISK_ORDER.get(prev_urgency, 0) < 2
            below_threshold = days_left <= settings.days_threshold and days_left < 99
            got_worse = RISK_ORDER.get(urgency, 0) > RISK_ORDER.get(prev_urgency, 0)

            trigger = None
            if risk_up:
                trigger = "risk_up"
            elif below_threshold and (got_worse or self._prev_days.get(code, float("inf")) > settings.days_threshold):
                trigger = "threshold"

            if trigger:
                # дедупликация: не слать повторно, пока не стало ещё хуже
                already = self._last_alerted.get(code)
                if already == urgency and trigger != "threshold":
                    self._prev_risk[code] = urgency
                    self._prev_days[code] = days_left
                    continue
                alert = self._make_alert(ln, trigger, level, prev_level, days_left)
                alerts.append(alert)
                self._last_alerted[code] = urgency
            else:
                # возврат к норме -> «риск снят» (если раньше был алерт)
                if code in self._last_alerted and RISK_ORDER.get(urgency, 0) < 2 and not below_threshold:
                    alert = self._make_alert(ln, "cleared", level, prev_level, days_left)
                    alerts.append(alert)
                    del self._last_alerted[code]

            self._prev_risk[code] = urgency
            self._prev_days[code] = days_left

        # доставка
        for a in alerts:
            self._deliver(a, settings)
        return alerts

    def _make_alert(self, ln, kind, level, prev_level, days_left) -> Alert:
        code = ln["code"]
        detail = ln.get("detail", {})
        stock = detail.get("free_stock", 0)
        if kind == "cleared":
            msg = f"Риск по «{ln['name'][:40]}» снят — остатка снова достаточно."
        elif kind == "threshold":
            msg = (
                f"Товар «{ln['name'][:40]}» заканчивается: осталось ~{days_left:.0f} дн. "
                f"(остаток {stock:.0f} шт). Рекомендуется срочный заказ."
            )
        else:
            msg = (
                f"Резкий рост риска по «{ln['name'][:40]}»: уровень стал «высокий» "
                f"(было «{prev_level}»). Остаток {stock:.0f} шт, хватит ~{days_left:.0f} дн."
            )
        return Alert(
            code=code,
            name=ln["name"],
            supplier=ln.get("supplier", ""),
            kind=kind,
            level=level,
            prev_level=prev_level,
            days_left=days_left,
            current_stock=stock,
            depletion_date=_depletion_date(days_left),
            deep_link=f"/?focus={code}",
            message=msg,
            created_at=datetime.now(UTC).isoformat(timespec="seconds"),
        )

    def _deliver(self, alert: Alert, settings: AlertSettings) -> None:
        """Доставка по каналам. Push -> лента дашборда, email -> демо (лог)."""
        if settings.push_enabled:
            self.feed.insert(0, alert.as_dict())
            self.feed = self.feed[:50]  # держим последние 50
        if settings.email_enabled:
            # ДЕМО-режим: реальный SMTP требует провайдера (SendGrid и т.п.).
            # Здесь формируем и логируем письмо — на демо показываем содержание.
            logger.info(
                "EMAIL -> %s | %s | %s (ссылка: %s)",
                settings.email_to,
                alert.name[:40],
                alert.message,
                alert.deep_link,
            )

    def reset(self) -> None:
        self._prev_risk.clear()
        self._last_alerted.clear()
        self._prev_days.clear()
        self.feed.clear()
