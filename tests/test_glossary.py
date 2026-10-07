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

    def sync(self, entries) -> tuple[str, int]:
        self.synced.append(sorted(e["term"] for e in entries))
        return "https://telegra.ph/Slovar-Vibe-Stack", 0


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


def test_telegraph_page_content_and_call() -> None:
    import json

    import httpx

    from vibe_stack.glossary_page import TelegraphPage, build_nodes

    entries = [{"term": "RAG", "definition": "d2", "source_url": "https://w.org/rag", "post_url": None,
                "published_at": "2026-10-01T09:00:00+00:00"},
               {"term": "MCP", "definition": "d1", "source_url": "https://m.io", "post_url": "https://t.me/v/5",
                "published_at": "2026-10-02T09:00:00+00:00"}]
    nodes, dropped = build_nodes(entries)
    assert dropped == 0
    assert [n["children"][0] for n in nodes if n["tag"] == "h4"] == ["MCP", "RAG"]
    tags = {n["tag"] for n in nodes} | {c["tag"] for n in nodes for c in n["children"] if isinstance(c, dict)}
    assert tags <= {"p", "h4", "a"}  # только теги, которые разрешает Telegraph
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["path"] = req.url.path
        seen["form"] = dict(httpx.QueryParams(req.content.decode()))
        return httpx.Response(200, json={"ok": True, "result": {"url": "https://telegra.ph/Slovar-10-07"}})

    page = TelegraphPage("tok", "Slovar-10-07", client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert page.sync(entries) == ("https://telegra.ph/Slovar-10-07", 0)
    assert seen["path"] == "/editPage/Slovar-10-07"
    assert json.loads(seen["form"]["content"]) == nodes


def test_page_drops_oldest_terms_when_over_limit() -> None:
    from vibe_stack.glossary_page import MAX_DEFINITION, build_nodes

    entries = [{"term": f"T{i:03d}", "definition": "слово " * 400, "source_url": f"https://s.org/{i}",
                "post_url": None, "published_at": f"2026-{1 + i // 28:02d}-{1 + i % 28:02d}T09:00:00+00:00"}
               for i in range(200)]
    nodes, dropped = build_nodes(entries)
    shown = [n["children"][0] for n in nodes if n["tag"] == "h4"]
    assert dropped > 0 and len(shown) == 200 - dropped
    assert "T199" in shown and "T000" not in shown  # уходят самые старые, а не конец алфавита
    assert all(len(n["children"][0]) <= MAX_DEFINITION + 1 for n in nodes[2::3])


def test_definition_is_only_verified_g_claims(cfg, tmp_path, now) -> None:
    resp = mcp_responses()
    cid = term_candidate(MCP).id
    resp[("glossary", cid)]["example"] = "Пример из практики"
    resp[("verify", cid)]["checks"].append({"claim": "Пример из практики", "status": "supported", "evidence": "x"})
    resp[("verify", cid)]["checks"].append({"claim": "Выдумка модели", "status": "supported", "evidence": "x"})
    rt = make_rt(cfg, tmp_path, now, [MCP], resp)
    run_glossary(rt)
    run_publish(rt)
    (entry,) = rt.state.glossary_entries()
    assert "Пример" not in entry["definition"] and "Выдумка" not in entry["definition"]
    assert "открытый протокол" in entry["definition"]


def test_lost_state_does_not_repeat_term(cfg, tmp_path, now) -> None:
    from vibe_stack.board import GlossaryEntry

    rt = make_rt(cfg, tmp_path, now, [MCP, RAG])
    rt.board.add_glossary(GlossaryEntry(term="MCP", definition="d", source_url=MCP.source, published_at=now))
    assert [t.term for t in pick_terms(rt)] == ["RAG"]  # SQLite пуст, но в Notion термин уже есть
    rt.board.add_post(PostRecord(title="Слово дня: RAG", rubric="glossary", status=Status.SENDING, html="x"))
    assert pick_terms(rt) == []  # вышел ли пост — неизвестно: повторно не готовим


def test_page_is_rebuilt_from_notion_too(cfg, tmp_path, now) -> None:
    from vibe_stack.board import GlossaryEntry

    rt = make_rt(cfg, tmp_path, now, [MCP], mcp_responses())
    rt.board.add_glossary(GlossaryEntry(term="RAG", definition="d", source_url=RAG.source, published_at=now))
    run_glossary(rt)
    run_publish(rt)
    assert rt.glossary_page.synced == [["MCP", "RAG"]]  # после потери state страница не теряет терминов


def test_no_terms_left_notifies_once_a_week(cfg, tmp_path, now) -> None:
    rt = make_rt(cfg, tmp_path, now, [])
    assert run_glossary(rt)["status"] == "no_terms_left"
    assert run_glossary(rt)["status"] == "no_terms_left"
    assert len([m for m in rt.notifier.sent if "нет доступных терминов" in m]) == 1


def test_mentions_are_whole_words_in_visible_text() -> None:
    from vibe_stack.glossary import mentions, visible_text

    text = visible_text('Новый storage и webhook <a href="https://x.dev/rag">ссылка</a> #skill')
    assert not mentions(text, "RAG") and not mentions(text, "hook") and not mentions(text, "Skill")
    assert mentions(visible_text("LLM-агент для кода"), "LLM")
