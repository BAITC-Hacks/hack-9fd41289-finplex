"""Тесты алертов по Definition of Done из задачи."""

from app.core.alerts import AlertEngine, AlertSettings


def _line(code, urgency, coverage_months, name="Товар", category="1", stock=10):
    return {
        "code": code,
        "name": name,
        "supplier": "S",
        "urgency": urgency,
        "category": category,
        "detail": {"coverage_months": coverage_months, "free_stock": stock},
    }


# DoD 1: риск средний -> высокий => ровно одно уведомление
def test_dod1_risk_up_fires_once():
    eng = AlertEngine()
    s = AlertSettings()
    # первый прогон — средний риск
    eng.evaluate([_line("A", "Средняя", 1.0)], s)
    # второй прогон — стал высокий
    alerts = eng.evaluate([_line("A", "Высокая", 0.5)], s)
    assert len(alerts) == 1
    assert alerts[0].kind in ("risk_up", "threshold")
    assert alerts[0].level == "высокий"
    assert alerts[0].deep_link.endswith("A")


# DoD 2: повторный прогон с тем же уровнем -> нет повторного уведомления
def test_dod2_dedup_no_repeat():
    eng = AlertEngine()
    s = AlertSettings()
    eng.evaluate([_line("A", "Средняя", 1.0)], s)
    eng.evaluate([_line("A", "Высокая", 0.5)], s)  # алерт
    alerts2 = eng.evaluate([_line("A", "Высокая", 0.5)], s)  # тот же уровень
    assert alerts2 == []  # дедупликация


# DoD 3: отключение категории -> нет алертов по ней, по остальным есть
def test_dod3_mute_category():
    eng = AlertEngine()
    s = AlertSettings(muted_categories={"2"})
    eng.evaluate([_line("A", "Средняя", 1.0, category="1"), _line("B", "Средняя", 1.0, category="2")], s)
    alerts = eng.evaluate([_line("A", "Высокая", 0.5, category="1"), _line("B", "Высокая", 0.5, category="2")], s)
    codes = {a.code for a in alerts}
    assert "A" in codes  # категория 1 — алерт есть
    assert "B" not in codes  # категория 2 замьючена


# DoD 4: порог чувствительности влияет на срабатывание по дням до исчерпания
def test_dod4_threshold_days():
    # coverage 0.1 мес = 3 дня. Порог 5 дней -> сработает; порог 2 дня -> нет.
    eng1 = AlertEngine()
    a1 = eng1.evaluate([_line("A", "Средняя", 0.1)], AlertSettings(days_threshold=5))
    # первый прогон уже ниже порога + риск не менялся -> нужен предыдущий уровень
    eng1.evaluate([_line("A", "Плановая", 1.0)], AlertSettings(days_threshold=5))
    a1 = eng1.evaluate([_line("A", "Средняя", 0.1)], AlertSettings(days_threshold=5))
    assert any(x.kind == "threshold" for x in a1)

    eng2 = AlertEngine()
    eng2.evaluate([_line("A", "Плановая", 1.0)], AlertSettings(days_threshold=2))
    a2 = eng2.evaluate([_line("A", "Средняя", 0.1)], AlertSettings(days_threshold=2))
    # 3 дня > порог 2 -> по порогу не срабатывает (и риск не «высокий»)
    assert all(x.kind != "threshold" for x in a2)


# Возврат к норме -> уведомление «риск снят»
def test_risk_cleared():
    eng = AlertEngine()
    s = AlertSettings()
    eng.evaluate([_line("A", "Средняя", 1.0)], s)
    eng.evaluate([_line("A", "Высокая", 0.5)], s)  # алерт
    cleared = eng.evaluate([_line("A", "Плановая", 3.0)], s)  # вернулось в норму
    assert any(x.kind == "cleared" for x in cleared)


# Каналы: push кладёт в ленту, email в демо-режиме не падает
def test_channels_push_feed():
    eng = AlertEngine()
    s = AlertSettings(push_enabled=True, email_enabled=True)
    eng.evaluate([_line("A", "Средняя", 1.0)], s)
    eng.evaluate([_line("A", "Высокая", 0.5)], s)
    assert len(eng.feed) == 1  # push попал в ленту


def test_push_off_no_feed():
    eng = AlertEngine()
    s = AlertSettings(push_enabled=False, email_enabled=True)
    eng.evaluate([_line("A", "Средняя", 1.0)], s)
    eng.evaluate([_line("A", "Высокая", 0.5)], s)
    assert len(eng.feed) == 0  # push выключен -> лента пуста
