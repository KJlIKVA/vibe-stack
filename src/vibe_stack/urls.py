"""Канонизация ссылок и ключи дедупликации."""

from __future__ import annotations

import hashlib
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

TRACKING_PREFIXES = ("utm_",)
TRACKING_PARAMS = {"fbclid", "gclid", "mc_cid", "mc_eid", "ref_src", "source", "s", "si"}

_GH_REPO = re.compile(r"^/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)(?:/|$)")
_GH_RESERVED = {
    "orgs", "topics", "search", "marketplace", "features", "settings", "sponsors",
    "collections", "trending", "about", "pricing", "login", "join", "explore", "apps",
}


def host_of(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def same_site(host: str, domain: str) -> bool:
    """host совпадает с domain или является его поддоменом."""
    host, domain = host.lower().removeprefix("www."), domain.lower().removeprefix("www.")
    return host == domain or host.endswith("." + domain)


def canonical_url(url: str) -> str:
    parts = urlsplit(url.strip())
    scheme = "https" if parts.scheme in ("http", "https", "") else parts.scheme
    host = host_of(url)
    path = re.sub(r"/{2,}", "/", parts.path or "/")
    if path != "/" and path.endswith("/"):
        path = path[:-1]
    if host == "github.com":
        path = path.removesuffix(".git")
        m = _GH_REPO.match(path)
        if m and m.group(1).lower() not in _GH_RESERVED:
            rest = path[m.end():]
            # корень репозитория, README и вкладки кода — это один и тот же объект
            if not rest or rest.startswith(("blob/", "tree/")) or rest in ("readme", "README.md"):
                path = f"/{m.group(1)}/{m.group(2)}".lower()
    query = [
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=False)
        if not k.lower().startswith(TRACKING_PREFIXES) and k.lower() not in TRACKING_PARAMS
    ]
    return urlunsplit((scheme, host, path, urlencode(sorted(query)), ""))


def github_repo(url: str) -> str | None:
    """owner/repo для ссылок на github.com (нижний регистр), иначе None."""
    if host_of(url) != "github.com":
        return None
    m = _GH_REPO.match(urlsplit(url).path)
    if not m or m.group(1).lower() in _GH_RESERVED:
        return None
    return f"{m.group(1)}/{m.group(2).removesuffix('.git')}".lower()


def normalize_title(title: str) -> str:
    t = title.lower()
    t = re.sub(r"[^\w\s]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def title_hash(title: str) -> str:
    return hashlib.sha1(normalize_title(title).encode()).hexdigest()[:16]


def dedup_keys(url: str, title: str) -> list[str]:
    canon = canonical_url(url)
    keys = [f"url:{canon}"]
    repo = github_repo(url)
    # ключ репозитория — только для самого репозитория: релизы одного репо — разные события
    if repo and urlsplit(canon).path == f"/{repo}":
        keys.append(f"repo:{repo}")
    nt = normalize_title(title)
    if len(nt) >= 12:  # короткие заголовки вроде «v1.2.0» слишком часто совпадают
        keys.append(f"title:{title_hash(title)}")
    return keys


def short_id(*parts: str) -> str:
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:12]
