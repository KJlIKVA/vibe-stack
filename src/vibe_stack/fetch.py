"""Загрузка первоисточника. Всё загруженное — недоверенные данные: только читаем и режем."""

from __future__ import annotations

import ipaddress
import logging
import re
import socket
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx

from .config import Fetch, env
from .htmltext import html_to_text, page_images, published_from_meta
from .models import FetchedDoc
from .sources import reddit
from .sources.md_changelog import entry_ids, md_url, parse_entries
from .sources.youtube import api_get as yt_api_get
from .sources.youtube import duration_minutes, video_id
from .timeutil import Clock
from .urls import canonical_url, github_repo, host_of, same_site

log = logging.getLogger(__name__)

# запись changelog о выходе модели (решение 65): её дополняют страница анонса и описание модели
_LAUNCH = re.compile(r"\b(launch(?:ed|es|ing)?|releas(?:ed|es|ing)|introduc(?:e|ed|es|ing)|now available)\b",
                     re.IGNORECASE)
_MD_LINK = re.compile(r"\]\((https?://[^)\s]+)\)")
_CODE_ID = re.compile(r"`([a-z0-9][a-z0-9.-]+)`")
TEXT_TYPES = ("text/html", "text/plain", "text/markdown", "application/xhtml", "application/xml", "text/xml",
              "application/json", "application/rss", "application/atom")
_RELEASE_PATH = re.compile(r"^/([^/]+)/([^/]+)/releases/tag/([^/?#]+)")


class Fetcher(Protocol):
    def fetch(self, url: str, *, purpose: str) -> FetchedDoc: ...


class UnsafeURL(ValueError):
    pass


def check_public_url(url: str) -> None:
    """Защита от SSRF: только http(s) и только публичные адреса."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise UnsafeURL(f"схема/хост не разрешены: {url}")
    host = parts.hostname
    if host in ("localhost",) or host.endswith((".local", ".internal")):
        raise UnsafeURL(f"внутренний хост: {host}")
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as e:
        raise UnsafeURL(f"DNS не разрешился: {host}") from e
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            raise UnsafeURL(f"внутренний адрес {ip} для {host}")


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rsplit("\n", 1)[0] + "\n[…обрезано]"


class HttpFetcher:
    def __init__(self, cfg: Fetch, clock: Clock, client: httpx.Client | None = None,
                 check_urls: bool = True) -> None:
        self.cfg = cfg
        self.clock = clock
        self.check_urls = check_urls
        self.client = client or httpx.Client(
            timeout=cfg.timeout_s, follow_redirects=False, headers={"User-Agent": cfg.user_agent}
        )

    # --- публичный метод ---------------------------------------------------------------------------
    def fetch(self, url: str, *, purpose: str) -> FetchedDoc:
        try:
            repo = github_repo(url)
            canon = canonical_url(url)
            if repo and urlsplit(canon).path == f"/{repo}":
                return self._github_repo(url, repo)
            if repo and (m := _RELEASE_PATH.match(urlsplit(url).path)):
                return self._github_release(url, repo, m.group(3))
            if entry_ids(url):
                return self._changelog_entry(url)
            if video_id(url):
                return self._youtube(url)
            if reddit.thread_ref(url):
                return self._reddit(url)
            return self._generic(url)
        except (httpx.HTTPError, UnsafeURL, ValueError) as e:
            log.info("fetch failed %s: %s", url, e)
            return FetchedDoc(url=url, final_url=url, ok=False, fetched_at=self.clock(), error=str(e)[:300])

    # --- обычная страница ---------------------------------------------------------------------------
    def _get(self, url: str) -> tuple[int, str, str, str]:
        """(status, content-type, текст, итоговый url). Редиректы проверяем вручную — каждый адрес."""
        current = url
        for _ in range(6):
            if self.check_urls:
                check_public_url(current)
            with self.client.stream("GET", current) as r:
                if r.is_redirect and "location" in r.headers:
                    current = str(r.url.join(r.headers["location"]))
                    continue
                ctype = r.headers.get("content-type", "").lower()
                if r.status_code >= 400 or (ctype and not ctype.startswith(TEXT_TYPES)):
                    return r.status_code, ctype, "", current
                body = bytearray()
                for chunk in r.iter_bytes():
                    body.extend(chunk)
                    if len(body) > self.cfg.max_bytes:
                        break
                return r.status_code, ctype, bytes(body).decode(r.encoding or "utf-8", errors="replace"), current
        raise ValueError("слишком много редиректов")

    def _generic(self, url: str) -> FetchedDoc:
        fallback = any(same_site(host_of(url), h) for h in self.cfg.archive_fallback_hosts)
        try:
            status, ctype, raw, final_url = self._get(url)
        except httpx.TimeoutException:
            if not fallback:
                raise
            status, ctype, raw, final_url = 408, "", "", url  # сайт не ответил вовремя — как отказ (решение 59)
        archived = False
        if status in (403, 408) and fallback:
            # сайт не пускает роботов из облака или не отвечает (openai.com) — берём последнюю копию страницы
            # из Архива интернета
            a_status, a_ctype, a_raw, _ = self._get(f"https://web.archive.org/web/2id_/{url}")
            if a_status < 400 and a_raw:
                status, ctype, raw, archived = a_status, a_ctype, a_raw, True
        now = self.clock()
        if status >= 400:
            return FetchedDoc(url=url, final_url=final_url, ok=False, http_status=status,
                              fetched_at=now, error=f"HTTP {status}")
        if ctype and not ctype.startswith(TEXT_TYPES):
            return FetchedDoc(url=url, final_url=final_url, ok=False, http_status=status,
                              fetched_at=now, error=f"тип {ctype} не поддерживается")
        image, figures = None, []
        if "html" in ctype or raw.lstrip()[:200].lower().startswith(("<!doctype html", "<html")):
            title, text, meta = html_to_text(raw)
            published = published_from_meta(meta)
            image, figures = page_images(raw, final_url)
        else:
            title, text, published = "", raw, None
        header = [f"[страница] {url}"]
        if archived:
            header.append("[текст из последней копии страницы в web.archive.org: сайт не пускает робота напрямую]")
        if title:
            header.append(f"Заголовок страницы: {title}")
        if published:
            header.append(f"Дата публикации (метаданные страницы): {published}")
        body = "\n".join(header) + "\n\n" + text
        return FetchedDoc(
            url=url, final_url=final_url, ok=bool(text.strip()), http_status=status,
            fetched_at=now, title=title, published_meta=published,
            text=truncate(body, self.cfg.max_doc_chars), error=None if text.strip() else "пустая страница",
            image=image, figures=figures,
        )

    # --- GitHub ---------------------------------------------------------------------------
    def _gh_headers(self, accept: str = "application/vnd.github+json") -> dict[str, str]:
        h = {"Accept": accept, "X-GitHub-Api-Version": "2022-11-28"}
        if token := env("GITHUB_TOKEN"):
            h["Authorization"] = f"Bearer {token}"
        return h

    def _gh_json(self, path: str) -> Any:
        r = self.client.get(f"https://api.github.com{path}", headers=self._gh_headers())
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()

    def _github_repo(self, url: str, repo: str) -> FetchedDoc:
        meta = self._gh_json(f"/repos/{repo}")
        now = self.clock()
        if not meta:
            return FetchedDoc(url=url, final_url=url, ok=False, http_status=404, fetched_at=now,
                              error="репозиторий не найден")
        readme_r = self.client.get(
            f"https://api.github.com/repos/{repo}/readme", headers=self._gh_headers("application/vnd.github.raw+json")
        )
        readme = readme_r.text if readme_r.status_code == 200 else ""
        latest = self._gh_json(f"/repos/{repo}/releases/latest")
        lines = [
            f"[репозиторий GitHub] {meta.get('full_name')}",
            f"Описание: {meta.get('description') or '—'}",
            f"Звёзды: {meta.get('stargazers_count')} · Лицензия: {(meta.get('license') or {}).get('spdx_id') or '—'}"
            f" · Создан: {meta.get('created_at')} · Последний push: {meta.get('pushed_at')}",
            f"Темы: {', '.join(meta.get('topics') or []) or '—'}",
            f"Архивирован: {'да' if meta.get('archived') else 'нет'}",
        ]
        if meta.get("homepage"):
            lines.append(f"Сайт проекта (из настроек репозитория): {meta['homepage']}")
        if latest:
            lines.append(f"Последний релиз: {latest.get('tag_name')} от {latest.get('published_at')}")
        text = "\n".join(lines) + "\n\n--- README ---\n" + (readme or "(README нет)")
        return FetchedDoc(
            url=url, final_url=meta.get("html_url") or url, ok=bool(readme), http_status=200, fetched_at=now,
            title=meta.get("full_name") or repo, published_meta=meta.get("created_at"),
            updated_meta=max(filter(None, [meta.get("pushed_at"), (latest or {}).get("published_at")]), default=None),
            text=truncate(text, self.cfg.max_doc_chars), error=None if readme else "нет README",
            image=github_card(meta.get("full_name") or repo),
        )

    def _github_release(self, url: str, repo: str, tag: str) -> FetchedDoc:
        rel = self._gh_json(f"/repos/{repo}/releases/tags/{tag}")
        now = self.clock()
        if not rel:
            return FetchedDoc(url=url, final_url=url, ok=False, http_status=404, fetched_at=now,
                              error="релиз не найден")
        meta = self._gh_json(f"/repos/{repo}") or {}
        lines = [
            f"[релиз GitHub] {repo} {rel.get('tag_name')}",
            f"Название релиза: {rel.get('name') or '—'}",
            f"Опубликован: {rel.get('published_at')} · Пре-релиз: {'да' if rel.get('prerelease') else 'нет'}",
            f"Автор публикации: {(rel.get('author') or {}).get('login') or '—'}",
            f"Репозиторий: {meta.get('description') or '—'} · Звёзды: {meta.get('stargazers_count')}",
        ]
        body = rel.get("body") or ""
        text = "\n".join(lines) + "\n\n--- Release notes ---\n" + (body or "(описания релиза нет)")
        return FetchedDoc(
            url=url, final_url=rel.get("html_url") or url, ok=bool(body.strip()), http_status=200, fetched_at=now,
            title=f"{repo} {rel.get('tag_name')}", published_meta=rel.get("published_at"),
            text=truncate(text, self.cfg.max_doc_chars), error=None if body.strip() else "пустые release notes",
            image=github_card(repo),
        )

    # --- changelog в Markdown, YouTube ---------------------------------------------------------------
    def _changelog_entry(self, url: str) -> FetchedDoc:
        """Запись changelog (адрес …/changelog?entry=<id>) или записи одного дня (?entries=<id>,<id>, решение 70):
        документ — текст именно этих записей."""
        status, _, raw, _ = self._get(md_url(url))
        now = self.clock()
        ids = entry_ids(url)
        by_id = {e.id: e for e in parse_entries(raw)} if status < 400 else {}
        entries = [by_id[i] for i in ids if i in by_id]
        if len(entries) != len(ids) or not entries:
            return FetchedDoc(url=url, final_url=url, ok=False, http_status=status or 404, fetched_at=now,
                              error=f"HTTP {status}" if status >= 400 else "запись changelog не найдена")
        head = "[запись changelog]" if len(entries) == 1 else f"[записи changelog за день: {len(entries)}]"
        text = f"{head} {url.split('?')[0]}\n\n" + "\n\n".join(e.text() for e in entries)
        image, figures = None, []
        for entry in entries:
            for link in self._launch_pages(url, entry.body):
                if page := self._linked_page(link):
                    text += (f"\n\n--- Страница, на которую указывает запись (официальный источник): {link} ---\n"
                             f"{page.text}")
                    image, figures = image or page.image, figures or page.figures
        title = entries[0].title + (f" (+{len(entries) - 1})" if len(entries) > 1 else "")
        return FetchedDoc(url=url, final_url=url, ok=True, http_status=status, fetched_at=now, title=title,
                          published_meta=entries[0].day.isoformat(), text=truncate(text, self.cfg.max_doc_chars),
                          image=image, figures=figures)

    def _launch_pages(self, url: str, body: str) -> list[str]:
        """Страницы к записи о выходе модели: анонс по id модели (правило fetch.model_pages для хоста changelog —
        там бенчмарки и цена) и описания моделей, на которые ссылается запись (/models/)."""
        if not _LAUNCH.search(body):
            return []
        out: list[str] = []
        if rule := self.cfg.model_pages.get(urlsplit(url).hostname or ""):
            out += [rule.url.format(id=m) for m in _CODE_ID.findall(body) if re.fullmatch(rule.id, m)][:1]
        out += [u for u in _MD_LINK.findall(body) if "/models/" in urlsplit(u).path]
        return list(dict.fromkeys(out))[:3]

    def _linked_page(self, link: str) -> FetchedDoc | None:
        """Страница из записи: у документации — Markdown-версия (короче и чище), иначе обычная страница."""
        limit = self.cfg.max_doc_chars // 2
        try:
            status, ctype, raw, _ = self._get(md_url(link))
            if status < 400 and "markdown" in ctype and raw.strip():
                return FetchedDoc(url=link, final_url=link, ok=True, fetched_at=self.clock(),
                                  text=truncate(raw, limit))
            page = self._generic(link)
        except (httpx.HTTPError, UnsafeURL, ValueError) as e:
            log.info("страница к записи changelog %s: %s", link, e)
            return None
        return page.model_copy(update={"text": truncate(page.text, limit)}) if page.ok else None

    def _youtube(self, url: str) -> FetchedDoc:
        """Видео — через YouTube Data API: страницы YouTube из облака закрыты. Субтитров нет — только описание."""
        now = self.clock()
        vid = video_id(url)
        if not env("YOUTUBE_API_KEY"):
            return FetchedDoc(url=url, final_url=url, ok=False, fetched_at=now, error="YouTube: нет YOUTUBE_API_KEY")
        data = yt_api_get(self.client, "videos", {"part": "snippet,contentDetails", "id": vid})
        item = (data.get("items") or [None])[0]
        if not item:
            return FetchedDoc(url=url, final_url=url, ok=False, http_status=404, fetched_at=now,
                              error="видео не найдено")
        sn, cd = item.get("snippet") or {}, item.get("contentDetails") or {}
        minutes = duration_minutes(cd.get("duration") or "")
        thumbs = sn.get("thumbnails") or {}
        thumb = next((thumbs[k]["url"] for k in ("maxres", "standard", "high") if k in thumbs), None)
        lines = [
            f"[видео YouTube] {sn.get('title') or ''}",
            f"Канал: {sn.get('channelTitle') or '—'} · Опубликовано: {sn.get('publishedAt') or '—'}"
            + (f" · Длительность: {minutes} мин" if minutes else ""),
            f"Язык: {sn.get('defaultAudioLanguage') or sn.get('defaultLanguage') or '—'}",
            "Субтитры недоступны: пересказ возможен только по описанию.",
        ]
        text = "\n".join(lines) + "\n\n--- Описание ---\n" + (sn.get("description") or "(описания нет)")
        return FetchedDoc(url=url, final_url=url, ok=bool(sn.get("description")), http_status=200, fetched_at=now,
                          title=sn.get("title") or "", published_meta=sn.get("publishedAt"),
                          text=truncate(text, self.cfg.max_doc_chars), image=thumb,
                          error=None if sn.get("description") else "у видео нет описания")

    def _reddit(self, url: str) -> FetchedDoc:
        """Ветка Reddit — через её RSS: страницы и JSON Reddit роботам закрыты (решение 64)."""
        title, text, posted = reddit.thread_doc(self.client, url)
        now = self.clock()
        if not text:
            return FetchedDoc(url=url, final_url=url, ok=False, http_status=404, fetched_at=now,
                              error="ветка не найдена")
        return FetchedDoc(url=url, final_url=url, ok=True, http_status=200, fetched_at=now, title=title,
                          published_meta=posted, text=truncate(text, self.cfg.max_doc_chars))


class FixtureFetcher:
    """Документы из фикстур: url → {text, status, ...}; отдельная версия для повторной загрузки (verify)."""

    def __init__(self, docs: dict[str, dict[str, Any]], clock: Clock, max_chars: int = 24_000) -> None:
        self.docs = docs
        self.clock = clock
        self.max_chars = max_chars
        self.calls: list[tuple[str, str]] = []

    def fetch(self, url: str, *, purpose: str) -> FetchedDoc:
        self.calls.append((url, purpose))
        spec = self.docs.get(canonical_url(url))
        if spec is not None and purpose == "verify" and "verify_document" in spec:
            spec = spec["verify_document"]
        now = self.clock()
        if spec is None:
            return FetchedDoc(url=url, final_url=url, ok=False, http_status=404, fetched_at=now, error="HTTP 404")
        status = int(spec.get("status", 200))
        text = spec.get("text", "")
        ok = status < 400 and bool(text.strip())
        return FetchedDoc(
            url=url, final_url=url, ok=ok, http_status=status, fetched_at=now, title=spec.get("title", ""),
            published_meta=spec.get("published_meta"), updated_meta=spec.get("updated_meta"),
            image=spec.get("image"), figures=list(spec.get("figures") or []),
            text=truncate(f"[страница] {url}\n\n{text}", self.max_chars),
            error=None if ok else (f"HTTP {status}" if status >= 400 else "пустая страница"),
        )


def github_card(repo: str) -> str:
    """Карточка репозитория, которую GitHub сам рисует для превью (название, описание, звёзды)."""
    return f"https://opengraph.githubassets.com/1/{repo}"


def domain_of(url: str) -> str:
    return host_of(url)
