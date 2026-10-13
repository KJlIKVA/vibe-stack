"""Решение 64: Reddit — проекты, статьи и обсуждения; ветка через RSS; лимит ~1 запрос в минуту; строгий отбор."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from vibe_stack import collect
from vibe_stack.board import LocalBoard
from vibe_stack.fetch import FixtureFetcher, HttpFetcher
from vibe_stack.llm import FakeLLM
from vibe_stack.models import Candidate, ScoreResult
from vibe_stack.runtime import Runtime
from vibe_stack.sources import build_source, reddit
from vibe_stack.steps import VerifyOutcome, WriteOutcome, gate
from vibe_stack.storage import State
from vibe_stack.telegram import DryRunTelegram, Notifier
from vibe_stack.urls import canonical_url

THREAD = "https://www.reddit.com/r/ClaudeCode/comments/1abc/haiku_for_explore/"
LONG = "Я поменял модель субагента Explore на Haiku в настройках агента. " * 10


def entry(title: str, permalink: str, sub: str, link: str = "", body: str = "") -> str:
    body_html = f"&lt;!-- SC_OFF --&gt;&lt;div class=\"md\"&gt;{body}&lt;/div&gt;&lt;!-- SC_ON --&gt;" if body else ""
    link_html = f" &lt;span&gt;&lt;a href=\"{link or permalink}\"&gt;[link]&lt;/a&gt;&lt;/span&gt;"
    return (f'<entry><author><name>/u/someone</name></author><category term="{sub}" label="r/{sub}"/>'
            f'<title>{title}</title><link href="{permalink}"/><published>2026-10-13T05:00:00+00:00</published>'
            f'<content type="html">{body_html} submitted by{link_html}</content></entry>')


def feed(*entries: str) -> str:
    return '<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">' + "".join(entries) + "</feed>"


def client(routes: dict[str, object], seen: list[str] | None = None) -> httpx.Client:
    def handler(req: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(str(req.url))
        val = routes.get(str(req.url))
        if callable(val):
            return val()
        if isinstance(val, str):
            return httpx.Response(200, text=val, headers={"content-type": "application/atom+xml"})
        return httpx.Response(404, text="")
    return httpx.Client(transport=httpx.MockTransport(handler))


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch) -> list[float]:
    """Паузы лимита Reddit — без настоящего сна; часы тестовые."""
    slept: list[float] = []
    t = [1000.0]
    monkeypatch.setattr(reddit.throttle, "last", None)
    monkeypatch.setattr(reddit.throttle, "sleep", lambda s: (slept.append(s), t.__setitem__(0, t[0] + s)))
    monkeypatch.setattr(reddit.throttle, "clock", lambda: t[0])
    return slept


def test_listing_takes_projects_articles_and_real_discussions(cfg) -> None:
    src = next(s for s in cfg.sources if s.name == "reddit")
    listing = feed(
        # пост о проекте → репозиторий
        entry("llama : add a GPU cache", "https://www.reddit.com/r/LocalLLaMA/comments/1/x/", "LocalLLaMA",
              link="https://github.com/ggml-org/llama.cpp"),
        # картинка без текста → нет
        entry("Damage control", "https://www.reddit.com/r/ClaudeAI/comments/2/y/", "ClaudeAI",
              link="https://i.redd.it/abc.jpeg"),
        # обсуждение с текстом → сама ветка
        entry("Haiku for Explore", THREAD, "ClaudeCode", body=LONG),
        # обсуждение с одним репозиторием в тексте → README репозитория
        entry("I made a skill for diagrams", "https://www.reddit.com/r/ClaudeAI/comments/4/w/", "ClaudeAI",
              link="https://i.redd.it/x.gif",
              body=LONG + '&lt;a href="https://github.com/Someone/penciled/blob/main/README.md"&gt;repo&lt;/a&gt;'),
        # короткий пост → нет
        entry("Is Claude down?", "https://www.reddit.com/r/ClaudeAI/comments/5/v/", "ClaudeAI", body="Anyone else?"),
        # мем по заголовку → нет
        entry("Funny meme about Opus", "https://www.reddit.com/r/ClaudeAI/comments/6/u/", "ClaudeAI", body=LONG),
        # длинный текст, но сабреддит не из обсуждений → только ссылка наружу
        entry("NVIDIA RTX news", "https://www.reddit.com/r/LocalLLaMA/comments/7/t/", "LocalLLaMA",
              link="https://example.com/rtx", body=LONG),
    )
    got = build_source(src, client({src.url: listing}), lambda: datetime.now(UTC), 30).collect()
    assert [(c.url, c.extra.get("discussion", False)) for c in got] == [
        ("https://github.com/ggml-org/llama.cpp", False),
        (THREAD, True),
        ("https://github.com/someone/penciled", False),
        ("https://example.com/rtx", False),
    ]
    talk = got[1]
    assert talk.source_type == "reddit" and talk.extra["subreddit"] == "ClaudeCode"
    assert talk.summary.startswith("Я поменял модель") and talk.signal > got[3].signal  # выше в «лучшем за день»
    assert talk.for_prompt()["extra"] == {"discussion": True, "subreddit": "ClaudeCode"}  # модель видит, что это


def test_thread_document_has_post_comments_and_bot_summary(cfg, now) -> None:
    rss = feed(
        entry("Haiku for Explore", THREAD, "ClaudeCode", body="&lt;p&gt;Поменял модель Explore на Haiku.&lt;/p&gt;"),
        '<entry><author><name>/u/AutoModerator</name></author><title>/u/AutoModerator on x</title>'
        '<content type="html">&lt;div class="md"&gt;Your post was flaired&lt;/div&gt;</content></entry>',
        '<entry><author><name>/u/ClaudeAI-mod-bot</name></author><title>/u/ClaudeAI-mod-bot on x</title>'
        '<content type="html">&lt;div class="md"&gt;TL;DR: работает, но Haiku хуже ищет&lt;/div&gt;</content></entry>',
        '<entry><author><name>/u/dev1</name></author><title>/u/dev1 on x</title>'
        '<content type="html">&lt;div class="md"&gt;У меня лимиты стали тратиться втрое медленнее&lt;/div&gt;</content>'
        '</entry>',
    )
    seen: list[str] = []
    api = "https://www.reddit.com/r/ClaudeCode/comments/1abc/.rss?sort=top&limit=20"
    doc = HttpFetcher(cfg.fetch, lambda: now, client({api: rss}, seen), check_urls=False).fetch(THREAD,
                                                                                               purpose="score")
    assert seen == [api] and doc.ok and doc.title == "Haiku for Explore"
    assert doc.text.startswith("[обсуждение Reddit] r/ClaudeCode")
    assert "а не документация" in doc.text and "Поменял модель Explore на Haiku." in doc.text
    assert "Краткий итог обсуждения (пишет бот сабреддита автоматически) ---\nTL;DR" in doc.text
    assert "[комментарий] У меня лимиты" in doc.text and "flaired" not in doc.text and "dev1" not in doc.text
    assert doc.published_meta == "2026-10-13T05:00:00+00:00"


def test_requests_wait_for_reddit_limit_and_retry_429(no_sleep) -> None:
    url = "https://www.reddit.com/r/x/top/.rss"
    answers = iter([httpx.Response(200, text="a"), httpx.Response(429, headers={"x-ratelimit-reset": "30"}),
                    httpx.Response(200, text="b")])
    c = client({url: lambda: next(answers)})
    assert reddit.get(c, url).text == "a" and no_sleep == []
    assert reddit.get(c, url).text == "b"
    assert no_sleep == [reddit.MIN_INTERVAL_S, 31.0]  # пауза между запросами и ожидание после 429
    tired = client({url: lambda: httpx.Response(429, headers={"x-ratelimit-reset": "600"})})
    with pytest.raises(httpx.HTTPStatusError):
        reddit.get(tired, url)  # ждать 10 минут не будем: источник подождёт следующего сбора


def score(category: str, usefulness: int = 4, novelty: int = 3) -> ScoreResult:
    return ScoreResult.model_validate({"id": "x", "category": category, "claims": ["a", "b"], "scores": {
        "novelty": novelty, "usefulness": usefulness, "verifiability": 2, "substance": 2, "audience_fit": 2}})


def test_discussion_gate_is_stricter(cfg) -> None:
    rubrics = {k: v.model_copy(update={"enabled": True}) for k, v in cfg.rubrics.items()}
    assert gate(score("trick"), cfg, rubrics, discussion=True).passed                     # 13 из 15, польза 4
    assert gate(score("tool"), cfg, rubrics, discussion=True).reasons == ["discussion_not_practical"]
    assert gate(score("case", usefulness=3), cfg, rubrics, discussion=True).reasons == ["low_usefulness"]
    assert gate(score("trick", novelty=0), cfg, rubrics, discussion=True).reasons == ["low_score"]  # 11 нужно
    assert gate(score("trick", novelty=0), cfg, rubrics).passed       # у статьи порог прежний: 10 из 15
    assert gate(score("tool", usefulness=3), cfg, rubrics).passed


def make_rt(cfg, tmp_path, now, responses) -> Runtime:
    state = State(tmp_path / "s.db")
    clock = lambda: now  # noqa: E731
    docs = {canonical_url(THREAD): {"text": "[обсуждение Reddit] r/ClaudeCode\n\n--- Пост ---\n" + LONG}}
    return Runtime(cfg=cfg, state=state, board=LocalBoard(tmp_path / "b.json"), tg=DryRunTelegram(tmp_path / "tg"),
                   notifier=Notifier(None, None, tmp_path / "a.log"),
                   llm=FakeLLM(cfg.llm, state, "p", clock, "Europe/Moscow", responses),
                   fetcher=FixtureFetcher(docs, clock), clock=clock, run_id="p", out_dir=tmp_path,
                   mode="dry-run", channel_id="@vibestack_off", sources_factory=lambda _: [])


def candidate() -> Candidate:
    return Candidate(source="reddit", source_type="reddit", url=THREAD, title="Haiku for Explore",
                     extra={"discussion": True, "subreddit": "ClaudeCode"})


def test_discussion_post_says_whose_experience_it_is(cfg, tmp_path, now, monkeypatch) -> None:
    c = candidate()
    wrote: dict[str, object] = {}
    monkeypatch.setattr(collect, "verify", lambda *a, **k: VerifyOutcome(None, ["a", "b"], []))
    monkeypatch.setattr(collect, "write", lambda *a, **k: (wrote.update(k), WriteOutcome("<b>пост</b>", []))[1])
    rt = make_rt(cfg, tmp_path, now, {("score", c.id): score("trick").model_dump()})
    rubrics = {k: v.model_copy(update={"enabled": True}) for k, v in cfg.rubrics.items()}
    assert collect.process_candidate(rt, c, rubrics) == "queued"
    prompt = rt.llm.calls[0][2]
    assert '"discussion": true' in prompt and "Обсуждения Reddit (extra.discussion = true)" in prompt
    assert "<обсуждение_reddit>" in str(wrote["notes"]) and "Ники участников не упоминай" in str(wrote["notes"])


def test_discussion_without_practice_is_rejected_before_verify(cfg, tmp_path, now, monkeypatch) -> None:
    c = candidate()
    monkeypatch.setattr(collect, "verify", lambda *a, **k: pytest.fail("проверка B не нужна"))
    rt = make_rt(cfg, tmp_path, now, {("score", c.id): score("analysis_candidate").model_dump()})
    rubrics = {k: v.model_copy(update={"enabled": True}) for k, v in cfg.rubrics.items()}
    assert collect.process_candidate(rt, c, rubrics) == "rejected"
    assert rt.state.candidates_between(now.date(), now.date())[-1]["reasons"] == '["discussion_not_practical"]'
