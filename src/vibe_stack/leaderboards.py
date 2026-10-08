"""Адаптеры рейтингов для закрепа. Подключаются только разрешённые способы доступа (раздел 5).

Каждый адаптер: key, label и fetch() → Snapshot (дата данных по источнику, топ-3, ссылка на данные)
или None. Места берутся только из данных источника — код ничего не ранжирует сам и не «додумывает».

- Arena: сайт arena.ai по их ToS скрапить нельзя. Разрешённый путь — их датасет на Hugging Face
  lmarena-ai/leaderboard-dataset (CC BY 4.0), читаем через официальный datasets-server API.
- Artificial Analysis: бесплатный API с ключом, но условия ограничивают ежедневный топ без письменного
  согласия — адаптер выключен в config.yaml, пока владелец канала не получит разрешение.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import date
from typing import Any

import httpx

from .config import Config, LeaderboardConfig
from .pin import Snapshot
from .timeutil import Clock, local_date

log = logging.getLogger(__name__)

HF_DATASET = "lmarena-ai/leaderboard-dataset"
HF_ROWS = "https://datasets-server.huggingface.co/rows"
HF_PAGE = f"https://huggingface.co/datasets/{HF_DATASET}"
CC_BY = "https://creativecommons.org/licenses/by/4.0/"
ARENA_ATTRIBUTION = (f'Arena: <a href="{HF_PAGE}">leaderboard-dataset</a> '
                     f'(<a href="{CC_BY}">CC BY 4.0</a>), топ-3 извлечён из данных')
AA_API = "https://artificialanalysis.ai/api/v2/language/models/free"
AA_ATTRIBUTION = 'Source: Artificial Analysis (<a href="https://artificialanalysis.ai">artificialanalysis.ai</a>)'


class ArenaAdapter:
    """Топ-3 категории (по умолчанию overall) из среза latest. Места — колонка rank, дата — leaderboard_publish_date.

    Строки среза идут блоками по категориям (overall — первой), внутри блока — по месту. Листаем страницы, пока
    не найдём места 1–3 своей категории или не пройдём её блок: для coding это около десяти страниц.
    """

    def __init__(self, cfg: LeaderboardConfig, client: httpx.Client, max_pages: int = 40) -> None:
        self.key = cfg.key
        self.label = cfg.label
        self.config = cfg.dataset_config or "text_style_control"
        self.category = cfg.category
        self.client = client
        self.max_pages = max_pages

    def _page(self, page: int) -> list[dict[str, Any]]:
        params = {"dataset": HF_DATASET, "config": self.config, "split": "latest", "offset": page * 100,
                  "length": 100}
        r = self.client.get(HF_ROWS, params=params)
        if r.status_code == 429:  # частые запросы подряд datasets-server ограничивает — одна пауза и повтор
            time.sleep(3)
            r = self.client.get(HF_ROWS, params=params)
        r.raise_for_status()
        return [x["row"] for x in r.json().get("rows", [])]

    def fetch(self) -> Snapshot | None:
        mine: list[dict[str, Any]] = []
        for page in range(self.max_pages):
            batch = self._page(page)
            here = [x for x in batch if x.get("category") == self.category]
            if mine and not here:
                break  # блок категории закончился
            mine += here
            if not batch or {1, 2, 3} <= {_rank(x) for x in mine}:
                break
        top = sorted(mine, key=_rank)[:3]
        if [_rank(x) for x in top] != [1, 2, 3]:
            log.warning("Arena %s: в данных не нашлись места 1–3 категории %s", self.config, self.category)
            return None
        dates = {_iso_date(x.get("leaderboard_publish_date")) for x in top}
        if len(dates) != 1 or None in dates:
            log.warning("Arena %s: у топ-3 нет общей даты публикации %s", self.config, dates)
            return None  # дату не выдумываем: без неё строка остаётся с прошлыми данными
        return Snapshot(key=self.key, label=self.label, date=dates.pop(), top=[str(x["model_name"]) for x in top],
                        scores=[_score(x) for x in top], data_url=HF_PAGE, attribution=ARENA_ATTRIBUTION)


def _score(row: dict[str, Any]) -> str:
    """Рейтинг места для навигатора: Arena Score (rating, ~1500) — целым числом, IPS-оценка Agent Arena (score,
    доли единицы) — тремя знаками. Нет числа — пусто, место показывается без рейтинга."""
    for key, fmt in (("rating", "{:.0f}"), ("score", "{:.3f}")):
        v = row.get(key)
        if isinstance(v, int | float):
            return fmt.format(v)
    return ""


def _iso_date(v: Any) -> str | None:
    """YYYY-MM-DD из поля источника или None, если это не дата."""
    try:
        return date.fromisoformat(str(v)[:10]).isoformat()
    except ValueError:
        return None


def _rank(row: dict[str, Any]) -> int:
    try:
        return int(float(row.get("rank")))  # в части конфигов rank приходит как float
    except (TypeError, ValueError):
        return 10**6


class ArtificialAnalysisAdapter:
    """Индекс Artificial Analysis через бесплатный API (x-api-key). Сортировку по индексу делает код.

    Дата — день получения данных: у ответа нет даты публикации рейтинга.
    """

    def __init__(self, cfg: LeaderboardConfig, client: httpx.Client, clock: Clock, tz: str) -> None:
        self.key = cfg.key
        self.label = cfg.label
        self.client = client
        self.clock = clock
        self.tz = tz

    def fetch(self) -> Snapshot | None:
        api_key = os.environ.get("AA_API_KEY")
        if not api_key:
            log.warning("Artificial Analysis: нет AA_API_KEY — строка закрепа не показывается")
            return None
        models: list[dict[str, Any]] = []
        for page in range(1, 11):
            r = self.client.get(AA_API, params={"page": page}, headers={"x-api-key": api_key})
            r.raise_for_status()
            data = r.json().get("data") or []
            models += data
            if len(data) < 200:
                break
        def index(m: dict[str, Any]) -> Any:
            return (m.get("evaluations") or {}).get("artificial_analysis_intelligence_index")

        scored = [m for m in models if isinstance(index(m), int | float)]
        scored.sort(key=index, reverse=True)
        if len(scored) < 3:
            return None
        return Snapshot(key=self.key, label=self.label, date=local_date(self.clock(), self.tz).isoformat(),
                        top=[str(m.get("name") or m.get("slug")) for m in scored[:3]],
                        data_url="https://artificialanalysis.ai", attribution=AA_ATTRIBUTION)


def build_adapters(cfg: Config, clock: Clock | None = None, client: httpx.Client | None = None) -> list[Any]:
    """Только включённые в config.yaml адаптеры (leaderboards[].enabled)."""
    from .timeutil import utc_now

    client = client or httpx.Client(timeout=30, headers={"User-Agent": cfg.fetch.user_agent}, follow_redirects=True)
    out: list[Any] = []
    for lb in cfg.leaderboards:
        if not lb.enabled:
            continue
        if lb.type == "arena_hf":
            out.append(ArenaAdapter(lb, client))
        elif lb.type == "artificial_analysis":
            out.append(ArtificialAnalysisAdapter(lb, client, clock or utc_now, cfg.channel.tz))
    return out
