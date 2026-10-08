"""Контур «Что посмотреть и послушать» (решение 48): воскресная подборка вышедших за неделю постов «Книга/видео».

Текст собирает код, без модели: строка на каждый пост — его первая строка (заголовок «📚 Название — автор, тип»),
ссылка — на наш пост в канале. Факты уже прошли сверку, когда вышли сами посты. Меньше двух постов — подборки нет.
"""

from __future__ import annotations

import html
import logging
import re
from typing import Any

from .board import BoardUnavailable
from .lint import lint_post
from .runtime import Runtime
from .telegram import TelegramError
from .timeutil import week_start

log = logging.getLogger(__name__)
CONTOUR = "watchlist"
MIN_ITEMS = 2
MAX_ITEMS = 12  # до двух постов в день при большой очереди (решение 50)
MAX_CHARS = 2500
_TAG_RE = re.compile(r"<[^>]+>")


def headline(post_html: str) -> str:
    """Первая строка поста без тегов и эмодзи рубрики: «Название — автор/канал, тип, длина»."""
    first = next((ln for ln in post_html.split("\n") if ln.strip()), "")
    text = html.unescape(_TAG_RE.sub("", first)).strip()
    return re.sub(r"^[^\w«\"(]+", "", text)[:160]


def remember_headline(rt: Runtime, message_id: int, post_html: str) -> None:
    rt.state.put(f"watch:{message_id}", headline(post_html))


def build(rt: Runtime) -> tuple[str, int]:
    """(текст подборки, сколько в ней постов) за текущую неделю (с понедельника)."""
    rows = [r for r in rt.state.published_since(week_start(rt.today()))
            if r.rubric == "book_video" and r.tg_message_id and r.tg_message_id > 0]
    items = []
    for r in rows[-MAX_ITEMS:]:
        link = rt.post_link(r.tg_message_id)
        if not link:
            continue
        label = rt.state.get(f"watch:{r.tg_message_id}") or r.title
        items.append(f'• <a href="{html.escape(link, quote=True)}">{html.escape(label)}</a>')
    if len(items) < MIN_ITEMS:
        return "", len(items)
    text = ("🎬 <b>Что посмотреть и послушать: подборка недели</b>\n\n" + "\n".join(items)
            + "\n\nВсе выпуски и книги — в постах по ссылкам: там о чём они, кому полезны и где взять.\n\n#подборка")
    return text, len(items)


def run_watchlist(rt: Runtime) -> dict[str, Any]:
    summary: dict[str, Any] = {"contour": CONTOUR, "mode": rt.mode, "published": 0}
    try:
        settings = rt.settings()
    except BoardUnavailable as e:
        rt.notifier.notify(f"подборка «Что посмотреть» пропущена: {e}")
        summary["status"] = "board_unavailable"
        return summary
    if settings.pause:
        summary["status"] = "paused"
        return summary
    rubric = rt.rubrics().get("book_video")
    if rubric is None or not rubric.enabled:
        summary["status"] = "rubric_disabled"
        return summary
    year, week, _ = rt.today().isocalendar()
    done_key = f"watchlist:{year}-W{week:02d}"
    if rt.already_done(done_key):
        summary["status"] = "already_published_this_week"
        return summary
    text, count = build(rt)
    summary["items"] = count
    if not text:
        summary["status"] = "too_few"
        rt.mark_done(done_key)  # повторный запуск в то же воскресенье ничего не изменит
        return summary
    # числа в строках — из вышедших постов, они уже сверены с первоисточниками
    if errors := lint_post(text, rubric="watchlist", max_chars=MAX_CHARS, source_url="", weekly=True,
                           allowed_text=text):
        rt.notifier.notify(f"подборка «Что посмотреть» не прошла проверку: {';'.join(errors)}")
        summary["status"] = "lint_error"
        summary["errors"] = errors
        return summary
    rt.write_out("watchlist.html", text)
    try:
        mid = rt.tg.send_message(rt.channel_id, text, preview=False)
    except TelegramError as e:
        rt.notifier.notify(f"Telegram не принял подборку «Что посмотреть»: {e}")
        summary["status"] = "telegram_error"
        return summary
    rt.mark_done(done_key)
    rt.state.record_published(ref=f"watchlist-{rt.today().isoformat()}", rubric="watchlist", urgent=False,
                              title="Что посмотреть и послушать", source_url="", domain="", published_at=rt.now(),
                              day=rt.today(), slot=None, tg_message_id=mid, counts_regular=False)
    summary.update(published=1, status="published")
    return summary
