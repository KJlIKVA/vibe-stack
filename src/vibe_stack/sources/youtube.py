"""Новые видео каналов YouTube через YouTube Data API (решение 48).

RSS YouTube и страницы видео из GitHub Actions закрыты (404 и «подтвердите, что вы не бот»), поэтому — только
официальный API с ключом YOUTUBE_API_KEY. Без ключа источник молча ничего не возвращает. Ключ передаётся
заголовком, а не в адресе: адреса запросов могут попасть в логи.

Квота: список загрузок канала — 1 единица, длительности видео канала — 1 единица, данные видео при проверке —
1 единица; бесплатный лимит — 10 000 в день. Видео короче min_minutes (Shorts, тизеры) отсекаются до модели.
Субтитры через API без OAuth не скачать, поэтому пост о видео — пересказ по описанию (так и пишется в посте).
"""

from __future__ import annotations

import logging
import re
from typing import Any

import httpx

from ..config import SourceConfig, env
from ..models import Candidate
from ..timeutil import parse_dt

log = logging.getLogger(__name__)
API = "https://www.googleapis.com/youtube/v3"
_ISO_DURATION = re.compile(r"^PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?$")


def api_get(client: httpx.Client, method: str, params: dict[str, Any]) -> dict[str, Any]:
    key = env("YOUTUBE_API_KEY")
    if not key:
        raise RuntimeError("нет YOUTUBE_API_KEY")
    r = client.get(f"{API}/{method}", params=params, headers={"X-Goog-Api-Key": key})
    r.raise_for_status()
    data = r.json()
    return data if isinstance(data, dict) else {}


def duration_minutes(iso: str) -> int | None:
    m = _ISO_DURATION.match(iso or "")
    if not m:
        return None
    h, mi, s = (int(x or 0) for x in m.groups())
    return h * 60 + mi + (1 if s >= 30 else 0)


def video_id(url: str) -> str | None:
    m = re.search(r"(?:youtube\.com/watch\?(?:.*&)?v=|youtu\.be/)([A-Za-z0-9_-]{11})", url)
    return m.group(1) if m else None


class YouTubeSource:
    def __init__(self, cfg: SourceConfig, client: httpx.Client) -> None:
        self.cfg = cfg
        self.name = cfg.name
        self.client = client

    def collect(self) -> list[Candidate]:
        if not env("YOUTUBE_API_KEY"):
            log.info("источник %s: нет YOUTUBE_API_KEY — пропускаю", self.name)
            return []
        out: list[Candidate] = []
        skip = [re.compile(p, re.IGNORECASE) for p in self.cfg.skip_title_regex]
        for channel in self.cfg.channels:
            uploads = "UU" + channel[2:]  # плейлист «все загрузки» канала UC… — UU…
            data = api_get(self.client, "playlistItems", {"part": "snippet,contentDetails", "playlistId": uploads,
                                                          "maxResults": 8})
            items = []
            for item in data.get("items") or []:
                sn, cd = item.get("snippet") or {}, item.get("contentDetails") or {}
                vid, title = cd.get("videoId"), (sn.get("title") or "").strip()
                if vid and title and not any(p.search(title) for p in skip):
                    items.append((vid, title, sn, cd))
            minutes = self._durations([vid for vid, *_ in items])
            for vid, title, sn, cd in items:
                mins = minutes.get(vid)
                if mins is None or mins < self.cfg.min_minutes:
                    continue
                out.append(Candidate(
                    source=self.name, source_type="youtube", url=f"https://www.youtube.com/watch?v={vid}",
                    title=title, summary=f"Длительность: {mins} мин\n{(sn.get('description') or '')[:1500]}",
                    published_at=parse_dt(cd.get("videoPublishedAt") or sn.get("publishedAt")),
                    signal=0.8, extra={"channel": sn.get("channelTitle") or "", "minutes": mins},
                ))
        return out

    def _durations(self, ids: list[str]) -> dict[str, int]:
        """Длительность в минутах; видео без длительности (трансляция идёт, видео удалено) — нет в ответе."""
        if not ids:
            return {}
        data = api_get(self.client, "videos", {"part": "contentDetails", "id": ",".join(ids[:50])})
        out = {}
        for item in data.get("items") or []:
            mins = duration_minutes((item.get("contentDetails") or {}).get("duration") or "")
            if item.get("id") and mins:
                out[item["id"]] = mins
        return out
