from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from vibe_stack.config import Config, load_config

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures"
# среда, 10:30 по Москве — внутри слота 10:00
NOW = datetime(2026, 10, 7, 7, 30, tzinfo=UTC)


@pytest.fixture
def cfg() -> Config:
    cfg = load_config(ROOT / "config.yaml")
    # сценарии тестов написаны по московскому времени; канал с 15.10 работает по японскому (решение 67)
    cfg.channel.tz = "Europe/Moscow"
    return cfg


@pytest.fixture
def now() -> datetime:
    return NOW
