"""«Слово дня»: только с источником, который определяет термин; после публикации — в словарь."""

from __future__ import annotations

from datetime import timedelta

from vibe_stack.board import LocalBoard
from vibe_stack.config import GlossaryConfig, GlossaryTerm
from vibe_stack.fetch import FixtureFetcher
from vibe_stack.glossary import pick_terms, run_glossary, term_candidate
from vibe_stack.llm import FakeLLM
from vibe_stack.models import PostRecord, Status
from vibe_stack.publish import run_publish
from vibe_stack.runtime import Runtime
from vibe_stack.storage import State
from vibe_stack.telegram import DryRunTelegram, Notifier
from vibe_stack.urls import canonical_url

MCP = GlossaryTerm(term="MCP", aliases=["Model Context Protocol"], source="https://modelcontextprotocol.io/intro")
RAG = GlossaryTerm(term="RAG", aliases=["retrieval-augmented generation"], source="https://en.wikipedia.org/wiki/RAG")
DOC = ("MCP (Model Context Protocol) is an open protocol that connects AI applications to external tools and data. "
       "Think of MCP like a USB-C port for AI applications.")
CLAIMS = ["MCP — открытый протокол, который подключает AI-приложения к внешним инструментам и данным.",
          "Авторы сравнивают MCP с портом USB-C для AI-приложений."]
POST = ('📖 <b>Слово дня: MCP</b>\n\nMCP — открытый протокол: он подключает AI-приложения к внешним инструментам '
        'и данным.\n<b>Пример:</b> авторы сравнивают его с портом USB-C для AI-приложений.\n\n'
        '<a href="https://modelcontextprotocol.io/intro">Источник</a> · #словарь')


class FakeGlossaryPage:
    def __init__(self) -> None:
        self.synced: list[list[str]] = []

    def sync(self, entries) -> str:
        self.synced.append([e["term"] for e in entries])
        return "https://telegra.ph/Slovar-Vibe-Stack"


def make_rt(cfg, tmp_path, now, terms, responses=None) -> Runtime:
    cfg = cfg.model_copy(update={"glossary": GlossaryConfig(terms=terms)})
    state = State(tmp_path / "s.db")
    clock = lambda: now  # noqa: E731
    docs = {canonical_url(MCP.source): {"text": DOC}}
    rt = Runtime(cfg=cfg, state=state, board=LocalBoard(tmp_path / "b.json"), tg=DryRunTelegram(tmp_path / "tg"),
                 notifier=Notifier(None, None, tmp_path / "a.log"),
                 llm=FakeLLM(cfg.llm, state, "g", clock, "Europe/Moscow", responses or {}),
                 fetcher=FixtureFetcher(docs, clock), clock=clock, run_id="g", out_dir=tmp_path, mode="dry-run",
                 channel_id="@vibestack_off", sources_factory=lambda _: [])
    rt.glossary_page = FakeGlossaryPage()
    return rt


def mcp_responses() -> dict:
    cid = term_candidate(MCP).id
    return {
        ("glossary", cid): {"id": cid, "term": "MCP", "has_definition": True, "hard_stops": [], "claims": CLAIMS,
                            "example": "", "not_to_confuse": ""},
        ("verify", cid): {"verdict": "pass", "fail_reason": "", "approved_claims": [],
                          "checks": [{"claim": c, "status": "supported", "evidence": "intro"} for c in CLAIMS]},
        ("write", cid): POST,
    }


def test_terms_from_this_weeks_posts_go_first(cfg, tmp_path, now) -> None:
    rt = make_rt(cfg, tmp_path, now, [MCP, RAG])
    rt.board.add_post(PostRecord(title="Новый RAG-сервер", rubric="tool", status=Status.PUBLISHED,
                                 html="про retrieval-augmented generation", published_at=now - timedelta(hours=3)))
    assert [t.term for t in pick_terms(rt)] == ["RAG", "MCP"]


def test_full_cycle_term_to_glossary(cfg, tmp_path, now) -> None:
    rt = make_rt(cfg, tmp_path, now, [MCP], mcp_responses())
    assert run_glossary(rt)["queued"] == 1
    assert run_glossary(rt)["status"] == "already_queued"  # в очереди не больше одного «Слова дня»
    s = run_publish(rt)
    assert s["status"] == "published" and "Слово дня: MCP" in rt.tg.sent[0][1]
    (entry,) = rt.state.glossary_entries()
    assert entry["term"] == "MCP"
    assert entry["post_url"] is None  # в dry-run message_id отрицательный — ссылки на пост нет
    assert "открытый протокол" in entry["definition"]
    assert rt.board.data.glossary[0].term == "MCP"
    assert rt.glossary_page.synced == [["MCP"]]
    assert pick_terms(rt) == []  # опубликованный термин больше не предлагается


def test_glossary_posts_do_not_go_stale(cfg, tmp_path, now) -> None:
    rt = make_rt(cfg, tmp_path, now, [MCP])
    ref = rt.board.add_post(PostRecord(title="Слово дня: MCP", rubric="glossary", status=Status.APPROVED,
                                       source_url=MCP.source, source_domain="modelcontextprotocol.io", html=POST,
                                       found_at=now - timedelta(days=20)))
    assert run_publish(rt)["status"] == "published"
    assert rt.board.get(ref).status == Status.PUBLISHED
