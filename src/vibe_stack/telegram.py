"""Telegram Bot API: публикация в канал и оповещения админу."""

from __future__ import annotations

import html
import logging
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger(__name__)


class TelegramError(RuntimeError):
    def __init__(self, message: str, retryable: bool, uncertain: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable
        # запрос мог дойти до Telegram: сообщение, возможно, уже в канале — повторять нельзя
        self.uncertain = uncertain


# запрос точно не дошёл до сервера — повтор безопасен даже для sendMessage
_NOT_DELIVERED = (httpx.ConnectError, httpx.ConnectTimeout)


class Telegram:
    """Отправка с повторами: исходная попытка + `retries` повторов с паузой (по умолчанию три).

    Повторяем только то, что точно не опубликовано: ошибки соединения и 429. Для sendMessage таймаут
    чтения и 5xx — «неизвестно, вышел ли пост»: не повторяем (иначе возможен дубль), а сообщаем админу.
    Идемпотентные методы (edit, pin) повторяем и на 5xx/таймаутах. Прочие 4xx повторять бессмысленно.
    """

    def __init__(self, token: str, client: httpx.Client | None = None, retries: int = 3,
                 pause_s: float = 5.0, sleep: Callable[[float], None] = time.sleep) -> None:
        self._base = f"https://api.telegram.org/bot{token}"
        self.client = client or httpx.Client(timeout=30)
        self.retries = retries
        self.pause_s = pause_s
        self.sleep = sleep

    def _call(self, method: str, payload: dict[str, Any], *, idempotent: bool) -> Any:
        last: TelegramError | None = None
        for attempt in range(self.retries + 1):
            try:
                r = self.client.post(f"{self._base}/{method}", json=payload)
                data = r.json()
            except _NOT_DELIVERED as e:
                # текст исключения httpx может содержать URL с токеном — не пишем его
                last = TelegramError(f"{method}: нет соединения ({type(e).__name__})", retryable=True)
            except (httpx.HTTPError, ValueError) as e:
                last = TelegramError(f"{method}: ответ не получен ({type(e).__name__})", retryable=idempotent,
                                     uncertain=not idempotent)
                if not idempotent:
                    raise last from None
            else:
                if data.get("ok"):
                    return data["result"]
                code = data.get("error_code") or r.status_code
                desc = data.get("description", "")
                retry_after = (data.get("parameters") or {}).get("retry_after")
                if code == 429:
                    last = TelegramError(f"{method}: 429 {desc}", retryable=True)
                    if retry_after:
                        self.sleep(float(retry_after))
                        continue
                elif code >= 500:
                    last = TelegramError(f"{method}: {code} {desc}", retryable=idempotent, uncertain=not idempotent)
                    if not idempotent:
                        raise last
                else:
                    raise TelegramError(f"{method}: {code} {desc}", retryable=False)
            if attempt < self.retries:
                self.sleep(self.pause_s * (attempt + 1))
        assert last is not None
        raise last

    def send_message(self, chat_id: str, text: str, *, html: bool = True, preview: bool = True,
                     preview_url: str | None = None, image_url: str | None = None) -> int:
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if html:
            payload["parse_mode"] = "HTML"
        if not preview:
            payload["link_preview_options"] = {"is_disabled": True}
        elif image_url:
            # картинка поста — большое превью над текстом (текст при этом до 4096 знаков, а не 1024 как у фото)
            payload["link_preview_options"] = {"url": image_url, "prefer_large_media": True, "show_above_text": True}
        elif preview_url:
            # превью всегда строится по первоисточнику, а не по первой попавшейся ссылке в тексте
            payload["link_preview_options"] = {"url": preview_url}
        return int(self._call("sendMessage", payload, idempotent=False)["message_id"])

    def edit_message_text(self, chat_id: str, message_id: int, text: str) -> None:
        self._call("editMessageText", {"chat_id": chat_id, "message_id": message_id, "text": text,
                                       "parse_mode": "HTML", "link_preview_options": {"is_disabled": True}},
                   idempotent=True)

    def pin_chat_message(self, chat_id: str, message_id: int) -> None:
        self._call("pinChatMessage", {"chat_id": chat_id, "message_id": message_id, "disable_notification": True},
                   idempotent=True)

    def pinned_message(self, chat_id: str) -> dict[str, Any]:
        """Последнее закреплённое сообщение канала ({} — ничего не закреплено)."""
        return self._call("getChat", {"chat_id": chat_id}, idempotent=True).get("pinned_message") or {}


class DryRunTelegram:
    """Вместо отправки пишет посты в out/. message_id — отрицательные, чтобы их нельзя было спутать."""

    def __init__(self, out_dir: str | Path) -> None:
        self.out = Path(out_dir)
        self.out.mkdir(parents=True, exist_ok=True)
        self.sent: list[tuple[str, str]] = []
        self.edited: list[tuple[int, str]] = []
        self.pinned: list[int] = []
        self.texts: dict[int, str] = {}
        self.images: dict[int, str | None] = {}
        self._next = -1

    def send_message(self, chat_id: str, text: str, *, html: bool = True, preview: bool = True,
                     preview_url: str | None = None, image_url: str | None = None) -> int:
        mid = self._next
        self._next -= 1
        self.sent.append((chat_id, text))
        self.images[mid] = image_url
        self.texts[mid] = text
        (self.out / f"msg{-mid:03d}.html").write_text(text, encoding="utf-8")
        return mid

    def edit_message_text(self, chat_id: str, message_id: int, text: str) -> None:
        self.edited.append((message_id, text))
        self.texts[message_id] = text
        (self.out / f"edit{abs(message_id):03d}.html").write_text(text, encoding="utf-8")

    def pin_chat_message(self, chat_id: str, message_id: int) -> None:
        self.pinned.append(message_id)
        log.info("[dry-run] pin %s", message_id)

    def pinned_message(self, chat_id: str) -> dict[str, Any]:
        if not self.pinned:
            return {}
        mid = self.pinned[-1]
        # как Bot API: текст без разметки (она приходит отдельно, в entities)
        plain = html.unescape(re.sub(r"<[^>]+>", "", self.texts.get(mid, "")))
        return {"message_id": mid, "text": plain}


def split_message(text: str, limit: int = 4000) -> list[str]:
    """Длинный текст — на части по абзацам (теги HTML в отчётах не переходят через пустую строку)."""
    parts: list[str] = []
    cur = ""
    for para in text.strip().split("\n\n"):
        while len(para) > limit:  # абзац длиннее лимита — режем по строкам
            cut = para.rfind("\n", 0, limit)
            cut = cut if cut > 0 else limit
            parts += [p for p in (cur, para[:cut]) if p]
            cur, para = "", para[cut:].lstrip("\n")
        if cur and len(cur) + 2 + len(para) > limit:
            parts.append(cur)
            cur = para
        else:
            cur = f"{cur}\n\n{para}" if cur else para
    return [*parts, cur] if cur else parts


class Notifier:
    """Оповещения в личку ADMIN_CHAT_ID. Без чата или в dry-run — только лог и файл."""

    def __init__(self, tg: Telegram | DryRunTelegram | None, admin_chat_id: str | None, log_path: Path) -> None:
        self.tg = tg
        self.chat = admin_chat_id
        self.log_path = log_path
        self.sent: list[str] = []

    def notify(self, text: str) -> None:
        msg = f"Vibe Stack: {text}"
        self.sent.append(msg)
        log.warning("оповещение админу: %s", text)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(msg + "\n")
        if self.tg is not None and self.chat and isinstance(self.tg, Telegram):
            try:
                self.tg.send_message(self.chat, msg[:4000], html=False, preview=False)
            except Exception as e:
                log.error("не удалось отправить оповещение: %s", type(e).__name__)
