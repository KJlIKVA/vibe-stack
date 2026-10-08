"""Оформление поста (решение 47): подвал от кода, картинка над текстом, цена и сравнение в посте о новой модели."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest

from vibe_stack import footer, images, prompts
from vibe_stack.board import LocalBoard
from vibe_stack.fetch import FixtureFetcher, github_card
from vibe_stack.htmltext import page_images
from vibe_stack.lint import lint_post
from vibe_stack.llm import FakeLLM, LLMError, OpenAILLM
from vibe_stack.models import Candidate, FetchedDoc, PostRecord, Status
from vibe_stack.publish import run_publish
from vibe_stack.runtime import Runtime
from vibe_stack.storage import State
from vibe_stack.telegram import DryRunTelegram, Notifier, Telegram

SRC = "https://github.com/acme/tool"
NEW_FOOTER = f'Сверено с <a href="{SRC}">первоисточником</a> ✅\n\n'


def rubric(cfg, key: str):
    return cfg.rubrics[key]


def template(cfg, key: str) -> str:
    return prompts.load(prompts.OVERLAY_FILES[rubric(cfg, key).overlay])


# --- подвал ---------------------------------------------------------------------------
@pytest.mark.parametrize(("key", "tail", "tag"), [
    ("tool", f'<a href="{SRC}">Первоисточник</a>\n✅ Сверено с первоисточником · #инструмент', "#инструмент"),
    ("urgent", f'✅ Сверено с первоисточником  \n<a href="{SRC}">Официальный источник</a> · #срочно', "#срочно"),
    ("book_video", f'<a href="{SRC}">Ссылка</a> · #видео', "#видео"),       # модель выбрала #видео — оставляем
    ("tool", f'<a href="{SRC}">Первоисточник</a> · #рубрика #реклама', "#инструмент"),  # чужие хештеги не берём
    ("tool", "", "#инструмент"),                                                    # подвала не было вовсе
])
def test_footer_replaces_model_footer(cfg, key, tail, tag) -> None:
    body = "🛠 <b>Tool: что это</b>\n\nСуть.\n<b>Подводный камень:</b> подробности смотрите в первоисточнике."
    out = footer.apply(body + "\n" + tail, source_url=SRC, rubric=rubric(cfg, key), template=template(cfg, key))
    assert out == body + "\n\n" + NEW_FOOTER + tag
    assert footer.apply(out, source_url=SRC, rubric=rubric(cfg, key), template=template(cfg, key)) == out


def test_footer_drops_check_mark_in_the_middle(cfg) -> None:
    """У «Книги/видео» модель ставит «✅ Сверено…» перед «Где взять» — отметка одна, в подвале (решение 50)."""
    body = "📚 <b>Book</b> — книга.\n\nО чём.\n<b>Сверено:</b> оглавление по ссылке"
    post = (body + "\n✅ Сверено с первоисточником  \n<b>Где взять:</b> по ссылке\n\n"
            + f'<a href="{SRC}">Ссылка</a> · #книга')
    out = footer.apply(post, source_url=SRC, rubric=rubric(cfg, "book_video"), template=template(cfg, "book_video"))
    assert out == body + "\n<b>Где взять:</b> по ссылке\n\n" + NEW_FOOTER + "#книга"


def test_footer_keeps_sandbox_mark_and_escapes_url(cfg) -> None:
    src = "https://example.dev/post?a=1&b=2"
    post = "Текст.\n🧪 Запущено в песочнице: установка и <code>x --help</code>\n✅ Сверено с первоисточником"
    out = footer.apply(post, source_url=src, rubric=rubric(cfg, "tool"), template=template(cfg, "tool"))
    assert out.split("\n")[1].startswith("🧪") and 'href="https://example.dev/post?a=1&amp;b=2"' in out


def test_footer_output_passes_lint(cfg) -> None:
    post = footer.apply("🛠 <b>tool: что это</b>\n\nСуть.", source_url=SRC, rubric=rubric(cfg, "tool"),
                        template=template(cfg, "tool"))
    assert lint_post(post, rubric="tool", max_chars=900, source_url=SRC) == []


# --- картинки со страницы ---------------------------------------------------------------------------
PAGE = """<html><head>
<meta property="og:image" content="/images/hero-2880x1620.png">
</head><body>
<header><img src="https://cdn.example.com/brand.png" alt="Brand"></header>
<article>
<img src="/_next/image?url=https%3A%2F%2Fcdn.example.com%2Fbench-2600x2578.png&amp;w=3840" alt="">
<img src="https://cdn.example.com/partner-143x64.png" alt="">
<img src="https://cdn.example.com/acme.svg" alt="">
<img src="https://cdn.example.com/x.png" alt="Acme logo">
<img src="http://insecure.example.com/a.png" alt="">
<img data-src="https://cdn.example.com/pricing.webp" alt="Pricing table">
</article>
<footer><img src="https://cdn.example.com/footer.png"></footer>
</body></html>"""


def test_page_images() -> None:
    main, figures = page_images(PAGE, "https://www.example.com/news/model")
    assert main == "https://www.example.com/images/hero-2880x1620.png"
    # Next.js-обёртка раскрыта; логотипы, svg, мелкие, http и картинки шапки/подвала отброшены
    assert figures == ["https://cdn.example.com/bench-2600x2578.png", "https://cdn.example.com/pricing.webp"]


def test_github_card() -> None:
    assert github_card("acme/tool") == "https://opengraph.githubassets.com/1/acme/tool"


# --- выбор картинки для поста о модели ---------------------------------------------------------------
def make_rt(cfg, tmp_path, now, responses=None) -> Runtime:
    state = State(tmp_path / "s.db")
    clock = lambda: now  # noqa: E731
    cfg.schedule.publish_slots = ["10:00", "14:00", "18:00"]
    cfg.schedule.slot_window_minutes = 120
    return Runtime(cfg=cfg, state=state, board=LocalBoard(tmp_path / "b.json"), tg=DryRunTelegram(tmp_path / "tg"),
                   notifier=Notifier(None, None, tmp_path / "a.log"),
                   llm=FakeLLM(cfg.llm, state, "p", clock, "Europe/Moscow", responses or {}),
                   fetcher=FixtureFetcher({}, clock), clock=clock, run_id="p", out_dir=tmp_path, mode="dry-run",
                   channel_id="@c", sources_factory=lambda _: [])


CAND = Candidate(source="anthropic-news", source_type="sitemap", url="https://www.anthropic.com/news/claude-x",
                 title="Introducing Claude X")
DOC = FetchedDoc(url=CAND.url, final_url=CAND.url, ok=True, fetched_at=datetime(2026, 10, 7, tzinfo=UTC), text="...",
                 image="https://cdn.example.com/hero.png",
                 figures=[f"https://cdn.example.com/f{i}.png" for i in range(10)])


def test_new_model_post_gets_the_benchmark_chart(cfg, tmp_path, now) -> None:
    key = ("image", f"{CAND.id}#image")
    rt = make_rt(cfg, tmp_path, now, {key: {"index": 2, "kind": "benchmark"}})
    assert images.choose(rt, CAND, DOC, new_model=True) == "https://cdn.example.com/f2.png"
    # модели показали не больше max_figures картинок, по порядку
    assert rt.llm.images[f"{CAND.id}#image"] == DOC.figures[:cfg.images.max_figures]
    prompt = rt.llm.calls[-1][2]
    assert "до 7" in prompt and "{{" not in prompt and prompts.load("safety").strip() in prompt


@pytest.mark.parametrize("answer", [{"index": -1, "kind": "none"}, {"index": 99, "kind": "benchmark"}, None])
def test_no_chart_falls_back_to_main_image(cfg, tmp_path, now, answer) -> None:
    responses = {("image", f"{CAND.id}#image"): answer} if answer else {}  # None — модель недоступна
    rt = make_rt(cfg, tmp_path, now, responses)
    assert images.choose(rt, CAND, DOC, new_model=True) == DOC.image


def test_regular_post_does_not_call_the_model(cfg, tmp_path, now) -> None:
    rt = make_rt(cfg, tmp_path, now)
    assert images.choose(rt, CAND, DOC) == DOC.image and rt.llm.calls == []
    cfg.images.enabled = False
    assert images.choose(rt, CAND, DOC, new_model=True) is None


def test_publish_sends_remembered_image_above_text(cfg, tmp_path, now) -> None:
    rt = make_rt(cfg, tmp_path, now)
    old = ('🛠 <b>Tool: что это</b>\n\nСуть.\n\n<a href="https://github.com/acme/tool">Первоисточник</a>\n'
           '✅ Сверено с первоисточником · #инструмент')  # пост из очереди в старом формате
    ref = rt.board.add_post(PostRecord(title="Tool", rubric="tool", status=Status.APPROVED, source_url=SRC,
                                       source_domain="github.com", score=12, found_at=now - timedelta(hours=1),
                                       html=old, candidate_id="c1"))
    images.remember(rt, "c1", github_card("acme/tool"))
    assert run_publish(rt)["status"] == "published"
    mid = next(iter(rt.tg.texts))
    assert rt.tg.images[mid] == "https://opengraph.githubassets.com/1/acme/tool"
    sent = rt.tg.texts[mid]
    assert sent.endswith(NEW_FOOTER + "#инструмент") and "Первоисточник</a>" not in sent
    assert rt.board.get(ref).html == sent  # в Notion — то, что ушло в канал


def test_telegram_large_image_preview_above_text() -> None:
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen.update(json.loads(req.content))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 5}})

    tg = Telegram("123:ABC", client=httpx.Client(transport=httpx.MockTransport(handler)), sleep=lambda s: None)
    tg.send_message("@ch", "x", preview_url="https://src.dev/a", image_url="https://cdn.dev/b.png")
    assert seen["link_preview_options"] == {"url": "https://cdn.dev/b.png", "prefer_large_media": True,
                                            "show_above_text": True}


def test_openai_sends_images_in_low_detail(cfg, tmp_path, now, monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-000000")
    llm = OpenAILLM(cfg.llm, State(tmp_path / "s.db"), "r", lambda: now, "Europe/Moscow")
    captured = {}

    def create(**kw):
        captured.update(kw)
        return SimpleNamespace(status="completed", output_text='{"index": 0, "kind": "pricing"}',
                               usage=SimpleNamespace(input_tokens=500, output_tokens=20,
                                                     input_tokens_details=SimpleNamespace(cached_tokens=0)))

    llm.client = SimpleNamespace(responses=SimpleNamespace(create=create))
    assert llm.json("image", "prompt", "c1", images=["https://cdn.dev/a.png"]).kind == "pricing"
    content = captured["input"][0]["content"]
    assert content[0] == {"type": "input_text", "text": "prompt"}
    assert content[1] == {"type": "input_image", "image_url": "https://cdn.dev/a.png", "detail": "low"}


def test_image_step_failure_is_not_fatal(cfg, tmp_path, now, monkeypatch) -> None:
    rt = make_rt(cfg, tmp_path, now)
    monkeypatch.setattr(rt.llm, "json", lambda *a, **k: (_ for _ in ()).throw(LLMError("boom")))
    assert images.pick_chart(rt, CAND, DOC) is None


# --- пост о новой модели ---------------------------------------------------------------------------
def test_new_model_notes_follow_the_template() -> None:
    notes = prompts.load("model_release")
    out = prompts.write_prompt(["c1"], {"category": "urgent", "url": "u", "title": "t", "mode": "urgent"},
                               "urgent", "🚨", "#срочно", notes=notes)
    assert out.index("до 500 знаков") < out.index("<b>Цена:</b>") < out.index("до 900 знаков")
    assert "<новая_модель>" in out
    assert "<новая_модель>" not in prompts.write_prompt(
        ["c1"], {"category": "urgent", "url": "u", "title": "t", "mode": "urgent"}, "urgent", "🚨", "#срочно")


def test_triage_asks_for_price_and_comparison_for_new_models() -> None:
    text = prompts.load("triage_U")
    assert "new_model" in text and "цена" in text and "бенчмарков" in text
