"""Репозиторий публичный: в отслеживаемых файлах не должно быть токенов, ключей и .env."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PATTERNS = {
    "OpenAI key": re.compile(r"(?<![A-Za-z0-9])sk-(proj-)?[A-Za-z0-9_-]{20,}"),
    "Telegram bot token": re.compile(r"\b\d{8,10}:AA[A-Za-z0-9_-]{30,}"),
    "Notion token": re.compile(r"\b(ntn|secret)_[A-Za-z0-9]{30,}"),
    "GitHub token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}"),
    "private key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
}


def tracked_files() -> list[Path]:
    try:
        out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("не git-репозиторий")
    return [ROOT / f for f in out.splitlines() if f]


def test_no_env_files_tracked() -> None:
    names = {p.name for p in tracked_files()}
    assert ".env" not in names and not any(n.endswith(".db") for n in names)


def test_no_secret_patterns_in_tracked_files() -> None:
    found = []
    for path in tracked_files():
        if not path.is_file() or path.stat().st_size > 2_000_000:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for name, rx in PATTERNS.items():
            if rx.search(text):
                found.append(f"{path.relative_to(ROOT)}: {name}")
    assert not found, "похоже на секрет в публичном репозитории: " + "; ".join(found)
