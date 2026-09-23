"""Реестр встроенных наборов данных партнёра (для демо без загрузки файлов)."""
from __future__ import annotations

from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"

# supplier key -> файлы
DATASETS = {
    "systeme": {
        "name": "Systeme Electric",
        "showcase": DATA_DIR / "systeme" / "Товар в пути_SystemElectric на 22.09.2026.xlsx",
        "history": DATA_DIR / "systeme" / "Динамика продаж_Syseme Electric_2025-2026.xlsx",
        "moq": DATA_DIR / "systeme" / "MOQ SystemElectric.xlsx",
    },
}


def available() -> list[dict]:
    return [{"key": k, "name": v["name"]} for k, v in DATASETS.items()
            if v["showcase"].exists()]
