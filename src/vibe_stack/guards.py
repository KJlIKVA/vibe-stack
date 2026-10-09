"""Детекторы на стороне кода: «ИИ предлагает, код ограничивает».

Эти проверки не заменяют оценку модели, а страхуют её: явные случаи код ловит сам.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlsplit

from .urls import host_of, same_site

_INJECTION = [
    r"ignore\s+(?:all\s+|any\s+)?(?:the\s+)?(?:previous|prior|above|earlier|preceding)\s+(?:instructions|prompts?|directions|rules)",
    r"disregard\s+(?:all\s+|any\s+)?(?:the\s+)?(?:previous|prior|above|earlier)\s+(?:instructions|prompts?|rules)",
    r"forget\s+(?:all\s+)?(?:your|the)\s+(?:previous\s+)?(?:instructions|rules)",
    r"mark\s+(?:this|it)\s+as\s+(?:verified|approved|supported)",
    r"(?:you\s+are|act\s+as)\s+now\s+(?:a|an|in)\b",
    r"new\s+system\s+prompt",
    r"игнорируй(?:те)?\s+(?:все\s+)?(?:предыдущие|прошлые|прежние|вышеуказанные)\s+(?:указания|инструкции|правила)",
    r"забудь(?:те)?\s+(?:все\s+)?(?:предыдущие\s+)?(?:указания|инструкции|правила)",
    r"(?:отметь|пометь)(?:те)?\s+(?:это\s+)?как\s+(?:проверенн|подтвержд)",
    r"скажи,?\s+что\s+(?:это\s+)?проверено",
]
INJECTION_RE = re.compile("|".join(f"(?:{p})" for p in _INJECTION), re.IGNORECASE)

# curl/wget ... | sh|bash|zsh, а также PowerShell iwr|iex
PIPE_TO_SHELL_RE = re.compile(
    r"\b(?:curl|wget)\b[^\n|]*\|\s*(?:sudo\s+)?(?:ba|z|da|k)?sh\b"
    r"|\b(?:iwr|irm|invoke-webrequest|invoke-restmethod)\b[^\n|]*\|\s*iex\b"
    r"|\b(?:ba|z)?sh\s+<\(\s*(?:curl|wget)\b",
    re.IGNORECASE,
)
SAFE_INSTALL_RE = re.compile(
    r"\b(?:pip3?|pipx|uv|uvx|poetry)\s+(?:tool\s+)?(?:install|add|run)\b|\buvx\s+\S"
    r"|\b(?:npm|pnpm|yarn|bun)\s+(?:i|install|add|create|dlx|x)\b|\bnpx\s+\S|\bbunx\s+\S"
    r"|\bbrew\s+install\b|\bcargo\s+install\b|\bgo\s+install\b|\bdocker\s+(?:run|pull|compose)\b"
    r"|\b(?:apt|apt-get|dnf|yum|pacman|winget|scoop|choco)\s+(?:-S\s+|install\b)"
    r"|\bgem\s+install\b|\bclaude\s+(?:mcp|plugin)\s+(?:add|install)\b|\bgit\s+clone\b",
    re.IGNORECASE,
)


def find_injection(text: str) -> str | None:
    m = INJECTION_RE.search(text)
    return m.group(0) if m else None


def has_pipe_to_shell(text: str) -> bool:
    return PIPE_TO_SHELL_RE.search(text) is not None


def only_unsafe_install(text: str) -> bool:
    """В источнике есть `curl … | bash`, и нет ни одного другого способа установки."""
    return has_pipe_to_shell(text) and SAFE_INSTALL_RE.search(text) is None


def affiliate_reason(url: str, affiliate_params: list[str]) -> str | None:
    """Партнёрские метки в ссылке кандидата."""
    params = {k.lower() for k, _ in parse_qsl(urlsplit(url).query)}
    hit = params & {p.lower() for p in affiliate_params}
    if not hit:
        return None
    host = host_of(url)
    # tag= — партнёрская метка Amazon; на остальных сайтах это обычный параметр
    if hit == {"tag"} and "amazon." not in host:
        return None
    return "affiliate_param:" + ",".join(sorted(hit))


def domain_in(url: str, domains: list[str]) -> bool:
    """Совпадение по домену; элементы вида `site.com/path` блокируют только этот путь."""
    host = host_of(url)
    path = urlsplit(url).path
    for d in domains:
        dom, _, prefix = d.partition("/")
        if same_site(host, dom) and (not prefix or path.lstrip("/").startswith(prefix)):
            return True
    return False


# Решение 61: пишем только о вышедших книгах. Метки раннего доступа и «скоро выйдет» на страницах издательств:
# Manning — «MEAP began …», «Publication in Spring 2027 (estimated)»; O'Reilly — «Early Release»;
# Pragmatic Bookshelf — «This book is in beta»; Springer/Apress — «Due: 12 November 2026».
UNRELEASED_BOOK_RE = re.compile(
    r"\bMEAP began\b|\bEarly Release\b|\bPublication in\b[^\n]{0,40}\(\s*estimated\s*\)"
    r"|\bthis (?:book|title) is (?:currently )?in beta\b|\bDue:\s*\d{1,2}\s+[A-Z][a-z]+\s+\d{4}",
    re.IGNORECASE,
)
# Pull request, issue или коммит — предложенное изменение, а не вышедшее: читателю пока нечем пользоваться
_GH_UNRELEASED_PATH = re.compile(r"^/[^/]+/[^/]+/(?:pull|pulls|issues|commit|compare)(?:/|$)")


def unreleased_book(text: str) -> bool:
    return UNRELEASED_BOOK_RE.search(text) is not None


def github_work_in_progress(url: str) -> bool:
    return host_of(url) == "github.com" and _GH_UNRELEASED_PATH.match(urlsplit(url).path) is not None
