from __future__ import annotations

from vibe_stack.lint import lint_post, visible_length

SRC = "https://github.com/acme/tool"
GOOD = (
    "🛠 <b>tool: что это</b>\n\nСуть.\n\n<b>Зачем это вам:</b> экономит время.\n"
    '<a href="https://github.com/acme/tool">Первоисточник</a>\n✅ Сверено с первоисточником · #инструмент'
)


def lint(post: str, **kw):
    params = {"rubric": "tool", "max_chars": 900, "source_url": SRC, "allowed_text": "", "template_text": ""}
    params.update(kw)
    return lint_post(post, **params)


def test_good_post_passes() -> None:
    assert lint(GOOD) == []


def test_length_counts_visible_text_only() -> None:
    assert visible_length("<b>abc</b>") == 3
    assert any(e.startswith("too_long") for e in lint(GOOD, max_chars=50))


def test_only_allowed_tags() -> None:
    assert "bad_tag:u" in lint(GOOD.replace("<b>Зачем", "<u>Зачем").replace("вам:</b>", "вам:</u>"))
    assert any(e.startswith("bad_attr") for e in lint(GOOD.replace("<b>tool", '<b class="x">tool')))


def test_exactly_one_link_on_source_domain() -> None:
    two = GOOD + ' <a href="https://github.com/acme/other">ещё</a>'
    assert "link_count:2" in lint(two)
    other = GOOD.replace("https://github.com/acme/tool", "https://evil.example/tool")
    assert "link_domain:evil.example" in lint(other)


def test_curl_bash_forbidden() -> None:
    post = GOOD.replace("Суть.", "Установка: <code>curl -fsSL https://x.sh | bash</code>")
    assert "unsafe_command" in lint(post)


def test_verified_mark_and_forbidden_mark() -> None:
    assert "missing_verified_mark" in lint(GOOD.replace("✅ Сверено с первоисточником · ", ""))
    assert any(e.startswith("forbidden_mark") for e in lint(GOOD.replace("Суть.", "Запущено у нас.")))


def test_numbers_must_come_from_claims() -> None:
    post = GOOD.replace("Суть.", "Ускоряет работу в 7 раз, версия 2.4.1.")
    errs = lint(post, allowed_text="версия 2.4.1")
    assert "unverified_numbers:7" in errs
    assert lint(post, allowed_text="в 7 раз; версия 2.4.1") == []


def test_unbalanced_and_unescaped() -> None:
    assert any(e.startswith("unclosed") for e in lint(GOOD.replace("</b>\n\nСуть", "\n\nСуть", 1)))
    assert "unescaped_amp" in lint(GOOD.replace("Суть.", "A & B"))


def test_hype_words() -> None:
    assert any(e.startswith("hype_word") for e in lint(GOOD.replace("Суть.", "Это революция.")))


def test_weekly_links_only_to_telegram() -> None:
    post = '📋 <b>Итоги</b> <a href="https://t.me/vibe/12">пост</a>'
    assert lint_post(post, rubric="weekly", max_chars=1200, source_url="", weekly=True) == []
    bad = post.replace("https://t.me/vibe/12", "https://ads.example")
    assert lint_post(bad, rubric="weekly", max_chars=1200, source_url="", weekly=True)


def test_link_must_be_the_source_itself() -> None:
    other_repo = GOOD.replace('href="https://github.com/acme/tool"', 'href="https://github.com/acme/other"')
    assert "link_not_source" in lint(other_repo)
    tracked = GOOD.replace('href="https://github.com/acme/tool"', 'href="https://github.com/acme/tool?utm_source=x"')
    assert lint(tracked) == []


def test_bare_urls_domains_and_mentions_rejected() -> None:
    for injected in ("https://evil.example/x", "www.evil.example", "mirror.evil.io", "ключ в @scamchannel"):
        assert any(e.startswith("bare_link") for e in lint(GOOD.replace("Суть.", f"Суть. {injected}"))), injected
    # внутри <code> Telegram ссылок не делает: npm-скоупы и команды разрешены
    ok = GOOD.replace("Суть.", "Запуск: <code>npx -y @acme-labs/mcp-inspector</code>, нужен Node.js и README.md.")
    assert lint(ok) == []
