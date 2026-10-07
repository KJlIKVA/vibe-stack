"""Адаптеры рейтингов для закрепа. Подключаются только разрешённые способы доступа (раздел 5).

Каждый адаптер: key, label и fetch() → Snapshot (дата данных по источнику, топ-3, ссылка на страницу рейтинга)
или None. Ранжирование — только из данных источника.
"""

from __future__ import annotations

from typing import Any

from .config import Config


def build_adapters(cfg: Config) -> list[Any]:
    """Адаптеры из config.yaml (раздел leaderboards). Пока условия источников не подтверждены — пусто:
    строки рейтинга в закрепе не показываются."""
    return []
