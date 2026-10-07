from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from .config import secret_values

_TG_TOKEN_RE = re.compile(r"bot\d+:[A-Za-z0-9_-]{20,}")


class RedactFilter(logging.Filter):
    """Вычищает значения секретов из всех сообщений лога."""

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        for s in secret_values():
            msg = msg.replace(s, "***")
        msg = _TG_TOKEN_RE.sub("bot***", msg)
        record.msg, record.args = msg, None
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return json.dumps({
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }, ensure_ascii=False)


def setup_logging(out_dir: Path | None, verbose: bool = False) -> None:
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    redact = RedactFilter()
    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S"))
    console.addFilter(redact)
    root.addHandler(console)
    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(out_dir / "run.log.jsonl", encoding="utf-8")
        fh.setFormatter(JsonFormatter())
        fh.addFilter(redact)
        root.addHandler(fh)
    # сторонние библиотеки пишут URL запросов — в Telegram они содержат токен
    for noisy in ("httpx", "httpcore", "openai", "notion_client"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
