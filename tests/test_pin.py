"""Закреп: создаётся и закрепляется один раз, правится только при изменении, хранит прошлую дату при сбое."""

from __future__ import annotations

from vibe_stack.board import LocalBoard
from vibe_stack.fetch import FixtureFetcher
from vibe_stack.fixtures import FixtureLeaderboard
from vibe_stack.llm import FakeLLM
from vibe_stack.pin import run_pin
from vibe_stack.runtime import Runtime
from vibe_stack.storage import State
from vibe_stack.telegram import DryRunTelegram, Notifier, TelegramError


def make_rt(cfg, tmp_path, now) -> Runtime:
    state = State(tmp_path / "s.db")
    clock = lambda: now  # noqa: E731
    return Runtime(cfg=cfg, state=state, board=LocalBoard(tmp_path / "b.json"), tg=DryRunTelegram(tmp_path / "tg"),
                   notifier=Notifier(None, None, tmp_path / "a.log"),
                   llm=FakeLLM(cfg.llm, state, "p", clock, "Europe/Moscow", {}), fetcher=FixtureFetcher({}, clock),
                   clock=clock, run_id="p", out_dir=tmp_path, mode="dry-run", channel_id="@c",
                   sources_factory=lambda _: [])


def arena(top, date="2026-10-06", fail=False):
    return FixtureLeaderboard({"key": "arena_text", "label": "Текст", "date": date, "top": top,
                               "data_url": "https://example.org/arena", "fail": fail})


def test_create_pin_once_then_edit_only_on_change(cfg, tmp_path, now) -> None:
    rt = make_rt(cfg, tmp_path, now)
    s = run_pin(rt, [arena(["A", "B", "C"])])
    assert s["status"] == "created_and_pinned" and rt.tg.pinned == [-1]
    assert "<b>Текст</b>\n🥇 A\n🥈 B\n🥉 C" in rt.tg.sent[0][1]
    assert run_pin(rt, [arena(["A", "B", "C"])])["status"] == "unchanged"
    s = run_pin(rt, [arena(["B", "A", "C"], date="2026-10-07")])
    assert s["status"] == "edited" and len(rt.tg.pinned) == 1
    assert "🥇 B\n🥈 A" in rt.tg.edited[-1][1]
    # места сменились — кроме правки закрепа, в канал вышел короткий пост об изменении (решение 58)
    assert len(rt.tg.sent) == 2 and "<b>Текст:</b> B — новый лидер, A опустилась на 🥈 место." in rt.tg.sent[1][1]


def test_failure_keeps_previous_date(cfg, tmp_path, now) -> None:
    rt = make_rt(cfg, tmp_path, now)
    run_pin(rt, [arena(["A", "B", "C"], date="2026-10-05")])
    s = run_pin(rt, [arena([], fail=True)])
    assert s["status"] == "unchanged" and s["problems"]
    # другой рейтинг обновился — закреп правится, а упавший остаётся прошлым, «свежий» не выдумываем
    video = FixtureLeaderboard({"key": "arena_video", "label": "Видео", "date": "2026-10-06", "top": ["V1", "V2", "V3"],
                                "data_url": "https://example.org/arena"})
    s = run_pin(rt, [arena([], fail=True), video])
    assert s["status"] == "edited"
    text = rt.tg.edited[-1][1]
    assert "🥇 A\n🥈 B\n🥉 C" in text and "🥇 V1" in text


def test_no_permitted_source_means_no_rating_block(cfg, tmp_path, now) -> None:
    rt = make_rt(cfg, tmp_path, now)
    run_pin(rt, [])
    text = rt.tg.sent[0][1]
    assert "Топ моделей" not in text and text.startswith("📌")


def test_navigator_layout(cfg, tmp_path, now) -> None:
    """Блоки через пустую строку, места столбиком; без дат, оговорок, подписи Arena и рубрик (решение 54).
    Указание источника по CC BY 4.0 — ссылка в заголовке «Топ моделей»."""
    rt = make_rt(cfg, tmp_path, now)
    video = FixtureLeaderboard({"key": "arena_video", "label": "Видео", "date": "2026-09-22", "top": ["V1", "V2", "V3"],
                                "data_url": "https://example.org/arena"})
    run_pin(rt, [arena(["A", "B", "C"]), video])
    assert rt.tg.sent[0][1] == (
        "📌 <b>Vibe Stack — навигатор</b>\n\n"
        '🏆 <b><a href="https://example.org/arena">Топ моделей</a></b>\n\n'
        "<b>Текст</b>\n🥇 A\n🥈 B\n🥉 C\n\n"
        "<b>Видео</b>\n🥇 V1\n🥈 V2\n🥉 V3")


def test_navigator_shows_scores(cfg, tmp_path, now) -> None:
    rt = make_rt(cfg, tmp_path, now)
    board = FixtureLeaderboard({"key": "arena_text", "label": "Текст", "date": "2026-10-02", "top": ["A", "B", "C"],
                                "scores": ["1525", "1505", "1504"], "data_url": "https://example.org/arena"})
    run_pin(rt, [board])
    assert "<b>Текст</b>\n🥇 A — 1525\n🥈 B — 1505\n🥉 C — 1504" in rt.tg.sent[0][1]


def test_deleted_pin_is_recreated(cfg, tmp_path, now) -> None:
    rt = make_rt(cfg, tmp_path, now)
    run_pin(rt, [arena(["A", "B", "C"])])

    def gone(*a, **k):
        raise TelegramError("editMessageText: 400 Bad Request: message to edit not found", retryable=False)

    rt.tg.edit_message_text = gone
    s = run_pin(rt, [arena(["X", "Y", "Z"], date="2026-10-07")])
    assert s["status"] == "created_and_pinned" and len(rt.tg.pinned) == 2


def test_pause_blocks_pin(cfg, tmp_path, now) -> None:
    rt = make_rt(cfg, tmp_path, now)
    rt.board.set_settings(pause=True)
    assert run_pin(rt, [arena(["A", "B", "C"])])["status"] == "paused"
    assert rt.tg.sent == []


def test_pin_failure_does_not_repost(cfg, tmp_path, now) -> None:
    rt = make_rt(cfg, tmp_path, now)
    real_pin = rt.tg.pin_chat_message

    def no_rights(*a, **k):
        raise TelegramError("pinChatMessage: 400 Bad Request: not enough rights to pin a message", retryable=False)

    rt.tg.pin_chat_message = no_rights
    for _ in range(3):  # прав на закрепление нет — сообщение отправлено один раз, а не каждый запуск
        assert run_pin(rt, [arena(["A", "B", "C"])])["status"] == "telegram_error"
    assert len(rt.tg.sent) == 1 and rt.tg.edited == []
    rt.tg.pin_chat_message = real_pin  # права выдали — следующий запуск только закрепляет
    assert run_pin(rt, [arena(["A", "B", "C"])])["status"] == "pinned"
    assert len(rt.tg.sent) == 1 and rt.tg.pinned == [-1]
    assert run_pin(rt, [arena(["A", "B", "C"])])["status"] == "unchanged"


def test_lost_state_adopts_pinned_navigator(cfg, tmp_path, now) -> None:
    rt = make_rt(cfg, tmp_path, now)
    run_pin(rt, [arena(["A", "B", "C"])])
    rt.state.db.execute("DELETE FROM kv WHERE key LIKE 'pin:%'")  # как после потери ветки state
    s = run_pin(rt, [arena(["X", "Y", "Z"], date="2026-10-07")])
    assert s["status"] == "edited" and len(rt.tg.sent) == 1  # второй навигатор не появился


def test_unpinned_and_deleted_pin_is_recreated_even_without_changes(cfg, tmp_path, now) -> None:
    rt = make_rt(cfg, tmp_path, now)
    run_pin(rt, [arena(["A", "B", "C"])])
    rt.tg.pinned.clear()  # навигатор удалили вручную: в канале ничего не закреплено
    real_pin = rt.tg.pin_chat_message

    def pin(chat_id, message_id):
        if message_id == -1:
            raise TelegramError("pinChatMessage: 400 Bad Request: message to pin not found", retryable=False)
        real_pin(chat_id, message_id)

    rt.tg.pin_chat_message = pin
    s = run_pin(rt, [arena(["A", "B", "C"])])
    assert s["status"] == "created_and_pinned" and rt.tg.pinned == [-2]


def test_everything_auto_but_notion_can_still_ask_for_approval(cfg, tmp_path, now) -> None:
    """Решение 58: все рубрики выходят автоматически, «Разбор» тоже; режим approve можно включить в Notion."""
    from vibe_stack.board import RubricOverride

    rt = make_rt(cfg, tmp_path, now)
    assert all(r.mode == "auto" for r in rt.rubrics().values())
    rt.board.data.rubrics["tool"] = RubricOverride(mode="approve")
    assert rt.rubrics()["tool"].mode == "approve"


# --- решение 58: пост об изменении рейтинга ----------------------------------------------------------------
def test_ratings_post_layout() -> None:
    """Рейтинги через пустую строку, без хештега (решение 58)."""
    from vibe_stack.pin import Snapshot
    from vibe_stack.ratings_post import build

    def snap(key, label, top):
        return Snapshot(key=key, label=label, date="2026-10-08", top=top, data_url="https://example.org/arena")

    text = build([(snap("c", "Кодинг", ["A", "B", "C"]), snap("c", "Кодинг", ["B", "A", "C"])),
                  (snap("v", "Видео", ["V1", "V2", "V3"]), snap("v", "Видео", ["V1", "V3", "V2"]))],
                 "https://example.org/arena")
    assert text == ("🏆 <b>Рейтинг моделей изменился</b>\n\n"
                    "<b>Кодинг:</b> B — новый лидер, A опустилась на 🥈 место.\n\n"
                    "<b>Видео:</b> V3 поднялась на 🥈 место, V2 опустилась на 🥉 место.\n\n"
                    'Сверено с <a href="https://example.org/arena">первоисточником</a> ✅')


def test_ratings_change_sentences() -> None:
    from vibe_stack.ratings_post import describe

    assert describe(["A", "B", "C"], ["A", "B", "C"]) == []
    assert describe(["A", "B", "C"], ["B", "A", "C"]) == ["B — новый лидер", "A опустилась на 🥈 место"]
    assert describe(["A", "B", "C"], ["A", "D", "B"]) == ["D вошла в топ-3 на 🥈 место", "B опустилась на 🥉 место",
                                                          "C выбыла из топ-3"]
    assert describe(["A", "B", "C"], ["A", "C", "B"]) == ["C поднялась на 🥈 место", "B опустилась на 🥉 место"]


def test_ratings_post_only_on_place_changes_and_once(cfg, tmp_path, now) -> None:
    rt = make_rt(cfg, tmp_path, now)
    video = lambda top, scores: FixtureLeaderboard({  # noqa: E731
        "key": "arena_video", "label": "Видео", "date": "2026-10-06", "top": top, "scores": scores,
        "data_url": "https://example.org/arena"})
    run_pin(rt, [arena(["A", "B", "C"]), video(["V1", "V2", "V3"], ["1516", "1513", "1493"])])
    # изменились только очки — поста нет
    s = run_pin(rt, [arena(["A", "B", "C"]), video(["V1", "V2", "V3"], ["1520", "1510", "1490"])])
    assert s["ratings_post"]["status"] == "no_changes" and len(rt.tg.sent) == 1
    # новый участник в видео — один пост, без картинки, одним предложением на рейтинг
    s = run_pin(rt, [arena(["A", "B", "C"]), video(["V1", "V4", "V2"], ["1520", "1515", "1510"])])
    assert s["ratings_post"] == {"status": "published", "changed": ["arena_video"]}
    post = rt.tg.sent[-1][1]
    assert post.startswith("🏆 <b>Рейтинг моделей изменился</b>\n\n<b>Видео:</b> V4 вошла в топ-3 на 🥈 место, "
                           "V2 опустилась на 🥉 место, V3 выбыла из топ-3.")
    assert post.endswith("первоисточником</a> ✅") and "#" not in post and "<b>Текст:</b>" not in post
    assert rt.state.published_since(rt.today())[-1].rubric == "ratings"
