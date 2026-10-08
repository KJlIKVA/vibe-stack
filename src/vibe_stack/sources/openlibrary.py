"""Новые книги издательства через поиск Open Library (решение 53): Apress.

Ни RSS, ни открытого каталога у Apress нет, а лента Springer показывает в основном анонсы книг следующего года.
Open Library находит книги издательства по году и теме, но описаний у новых книг там нет, поэтому адрес
кандидата — страница книги у издательства по ISBN (url_template). У Apress это link.springer.com/book/<ISBN>:
дата выхода, описание и оглавление — по ним идут оценка и сверка.

O'Reilly и Packt Open Library тоже находит, но их страницы роботам закрыты (403 и Cloudflare), а в Google Books
этих книг нет — сверять не по чему, поэтому их здесь нет.
"""

from __future__ import annotations

import re

import httpx

from ..config import SourceConfig
from ..models import Candidate

API = "https://openlibrary.org/search.json"


class OpenLibrarySource:
    def __init__(self, cfg: SourceConfig, client: httpx.Client, year: int) -> None:
        self.cfg = cfg
        self.name = cfg.name
        self.client = client
        self.year = year

    def collect(self) -> list[Candidate]:
        assert self.cfg.query and self.cfg.url_template
        r = self.client.get(API, params={"q": self.cfg.query, "sort": "new", "limit": 30,
                                         "fields": "title,isbn,first_publish_year,author_name"}, follow_redirects=True)
        r.raise_for_status()
        skip = [re.compile(p) for p in self.cfg.skip_title_regex]
        include = [re.compile(p, re.IGNORECASE) for p in self.cfg.include_title_regex]
        out = []
        for doc in r.json().get("docs") or []:
            title = str(doc.get("title") or "").strip()
            isbn = next((i for i in doc.get("isbn") or [] if re.fullmatch(r"97[89]\d{10}", i)), None)
            # дата в Open Library — только год: берём книги этого и прошлого года, точную дату видно на странице
            if not title or not isbn or (doc.get("first_publish_year") or 0) < self.year - 1:
                continue
            if any(p.search(title) for p in skip) or (include and not any(p.search(title) for p in include)):
                continue
            out.append(Candidate(
                source=self.name, source_type="openlibrary", url=self.cfg.url_template.format(isbn=isbn), title=title,
                whitelist=self.cfg.whitelist, official_domains=self.cfg.official_domains, signal=0.5,
                extra={"authors": ", ".join(doc.get("author_name") or [])[:120]},
            ))
        return out
