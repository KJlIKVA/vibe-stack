"""Reddit (решение 64): лучшее за день из сабреддитов по теме канала, только через RSS — JSON и страницы Reddit
роботам закрыты (403), официальный API выдают только по заявке.

Из записи ленты получается:
- ссылка на проект — пост ведёт на GitHub или Hugging Face;
- обсуждение — у поста свой текст не короче `min_body_chars` в сабреддите из `discussion_subreddits`. Если в
  тексте ровно один репозиторий GitHub, первоисточник — он («сделал skill» → README), иначе сама ветка: пост и
  лучшие комментарии (`thread_doc`);
- ссылка наружу — статья или сайт (как было в решении 48).
Картинки и видео без текста, мемы и посты по `skip_title_regex` не берём.

Reddit пускает без ключа примерно один запрос в минуту с адреса, поэтому все запросы к нему идут через `get`:
между ними не меньше `MIN_INTERVAL_S`, а на 429 — одна повторная попытка после паузы из заголовка.
"""

from __future__ import annotations

import calendar
import logging
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime
from urllib.parse import urlsplit

import feedparser
import httpx

from ..config import SourceConfig
from ..htmltext import html_to_text
from ..models import Candidate
from ..urls import github_repo, same_site

log = logging.getLogger(__name__)

HOSTS = ("reddit.com", "redd.it")
PROJECT_HOSTS = ("github.com", "huggingface.co")
MIN_INTERVAL_S = 65.0       # ≈1 запрос в минуту с адреса, с запасом
MAX_RETRY_WAIT_S = 120.0    # 429 с паузой дольше — не ждём, источник пропускается до следующего сбора
THREAD_COMMENTS = 20        # сколько лучших комментариев берём в документ ветки
BOT_SUMMARY = re.compile(r"mod-bot$", re.IGNORECASE)  # краткий итог обсуждения от бота r/ClaudeAI
SKIP_AUTHORS = {"automoderator"}

_THREAD = re.compile(r"^/r/([A-Za-z0-9_]+)/comments/([a-z0-9]+)", re.IGNORECASE)
_BODY = re.compile(r"<!-- SC_OFF -->(.*?)<!-- SC_ON -->", re.DOTALL)
_LINK = re.compile(r'<a href="([^"]+)">\s*\[link\]\s*</a>')
_HREF = re.compile(r'href="([^"]+)"')


class _Throttle:
    """Один на процесс: и лента, и загрузка веток считаются в общем лимите Reddit."""

    def __init__(self) -> None:
        self.last: float | None = None
        self.sleep: Callable[[float], None] = time.sleep
        self.clock: Callable[[], float] = time.monotonic

    def wait(self) -> None:
        if self.last is not None:
            pause = MIN_INTERVAL_S - (self.clock() - self.last)
            if pause > 0:
                log.info("reddit: пауза %.0f с (лимит запросов)", pause)
                self.sleep(pause)
        self.last = self.clock()


throttle = _Throttle()


def get(client: httpx.Client, url: str) -> httpx.Response:
    throttle.wait()
    r = client.get(url)
    if r.status_code == 429:
        try:
            reset = float(r.headers.get("x-ratelimit-reset") or MIN_INTERVAL_S)
        except ValueError:
            reset = MIN_INTERVAL_S
        if reset > MAX_RETRY_WAIT_S:
            r.raise_for_status()
        log.info("reddit: 429, повтор через %.0f с", reset + 1)
        throttle.sleep(reset + 1)
        throttle.last = throttle.clock()
        r = client.get(url)
    r.raise_for_status()
    return r


def thread_ref(url: str) -> tuple[str, str] | None:
    """(сабреддит, id поста) для ссылки на ветку Reddit, иначе None."""
    parts = urlsplit(url)
    if not same_site((parts.hostname or "").lower(), "reddit.com"):
        return None
    m = _THREAD.match(parts.path)
    return (m.group(1), m.group(2).lower()) if m else None


def _is_reddit(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return any(same_site(host, h) for h in HOSTS)


def _text(html: str) -> str:
    return html_to_text(html)[1].strip() if html else ""


def _dt(entry: feedparser.FeedParserDict, *names: str) -> datetime | None:
    for n in names:
        t = entry[n] if n in entry.keys() else None  # noqa: SIM118 — см. rss._entry_dt
        if t:
            return datetime.fromtimestamp(calendar.timegm(t), tz=UTC)
    return None


def _content(entry: feedparser.FeedParserDict) -> str:
    return (entry.get("content") or [{}])[0].get("value", "") or entry.get("summary") or ""


def _subreddit(entry: feedparser.FeedParserDict) -> str:
    return next((t.get("term") or "" for t in entry.get("tags") or []), "")


def _one_repo(body_html: str) -> str | None:
    """Репозиторий GitHub, если в тексте поста ссылки ровно на один (на несколько — это обсуждение)."""
    repos = {r for href in _HREF.findall(body_html) if (r := github_repo(href))}
    return f"https://github.com/{repos.pop()}" if len(repos) == 1 else None


class RedditSource:
    def __init__(self, cfg: SourceConfig, client: httpx.Client) -> None:
        self.cfg = cfg
        self.name = cfg.name
        self.client = client

    def collect(self) -> list[Candidate]:
        assert self.cfg.url
        feed = feedparser.parse(get(self.client, self.cfg.url).content)
        skip = [re.compile(p) for p in self.cfg.skip_title_regex]
        talk = {s.lower() for s in self.cfg.discussion_subreddits}
        out: list[Candidate] = []
        entries = feed.entries
        for rank, e in enumerate(entries):
            title = (e.get("title") or "").strip()
            permalink = e.get("link") or ""
            if not title or not permalink or any(p.search(title) for p in skip):
                continue
            html = _content(e)
            link = (m.group(1) if (m := _LINK.search(html)) else "")
            body_html = (m.group(1) if (m := _BODY.search(html)) else "")
            body = _text(body_html)
            sub = _subreddit(e)
            extra: dict[str, object] = {"subreddit": sub}
            if link and not _is_reddit(link) and any(same_site(urlsplit(link).hostname or "", h)
                                                     for h in PROJECT_HOSTS):
                url = link                                 # пост о проекте: первоисточник — репозиторий/модель
            elif sub.lower() in talk and len(body) >= self.cfg.min_body_chars:
                if repo := _one_repo(body_html):
                    url = repo                             # «сделал skill/инструмент» — первоисточник README
                else:
                    url = permalink                        # обсуждение: первоисточник — сама ветка
                    extra["discussion"] = True
            elif link and not _is_reddit(link):
                url = link                                 # статья или сайт
            else:
                continue                                   # картинка, видео, мем, короткий пост
            out.append(Candidate(
                source=self.name,
                source_type="reddit",
                url=url,
                title=title,
                summary=body[:1500],
                published_at=_dt(e, "published_parsed", "updated_parsed"),
                whitelist=self.cfg.whitelist,
                signal=1.0 - rank / max(len(entries), 1),  # лента «лучшее за день» уже по голосам
                extra=extra,
            ))
        return out


def thread_doc(client: httpx.Client, url: str, comments: int = THREAD_COMMENTS) -> tuple[str, str, str | None]:
    """(заголовок, текст, дата поста) ветки: пост и лучшие комментарии из RSS ветки."""
    ref = thread_ref(url)
    if ref is None:
        raise ValueError(f"не ветка Reddit: {url}")
    sub, post_id = ref
    feed = feedparser.parse(get(client, f"https://www.reddit.com/r/{sub}/comments/{post_id}/.rss"
                                        f"?sort=top&limit={comments}").content)
    if not feed.entries:
        return "", "", None
    post, rest = feed.entries[0], feed.entries[1:]
    title = (post.get("title") or "").strip()
    posted = _dt(post, "published_parsed", "updated_parsed")
    body = _text(m.group(1)) if (m := _BODY.search(_content(post))) else ""
    link = (m.group(1) if (m := _LINK.search(_content(post))) else "")
    lines = [
        f"[обсуждение Reddit] r/{sub} · https://www.reddit.com/r/{sub}/comments/{post_id}/",
        f"Заголовок поста: {title}",
        f"Опубликовано: {posted.isoformat() if posted else '—'}",
        "Это форум: пост и комментарии — опыт и мнения участников, а не документация и не официальное заявление.",
    ]
    if link and not _is_reddit(link):
        lines.append(f"Ссылка из поста: {link}")
    parts = ["\n".join(lines), "--- Пост ---\n" + (body or "(своего текста у поста нет: картинка, видео или ссылка)")]
    summary, said = [], []
    for c in rest:
        author = (c.get("author") or "").removeprefix("/u/").strip()
        if author.lower() in SKIP_AUTHORS:
            continue
        text = _text(_content(c))
        if not text:
            continue
        if BOT_SUMMARY.search(author):
            summary.append("--- Краткий итог обсуждения (пишет бот сабреддита автоматически) ---\n" + text)
        else:
            said.append("[комментарий] " + text)
    parts += summary
    if said:
        parts.append("--- Комментарии (самые популярные сверху) ---\n" + "\n\n".join(said))
    return title, "\n\n".join(parts), posted.isoformat() if posted else None
