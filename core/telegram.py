"""Envio de mensagens pro Telegram (Bot API) — canal compartilhado por
event_webhooks (tempo real), campaign_survey (relatório diário) e
copy_optimizer (digest semanal). Custo zero de crédito, só HTTP.

Config: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID no .env (core/config.py).
"""
from __future__ import annotations

import re
from typing import Optional

import requests as _requests
import structlog

from core import config

log = structlog.get_logger()


def is_configured() -> bool:
    return bool(config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID)


def send(text: str, chat_id: str = "", token: str = "") -> tuple[bool, Optional[str]]:
    """Manda uma mensagem HTML pro Telegram via Bot API sendMessage. (ok, err)."""
    token = token or config.TELEGRAM_BOT_TOKEN
    chat_id = chat_id or config.TELEGRAM_CHAT_ID
    if not token or not chat_id:
        return False, "Telegram não configurado (TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID)"
    try:
        resp = _requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=15,
        )
        ok = resp.status_code == 200
        if not ok:
            log.warning("telegram.send_failed", status=resp.status_code, body=(resp.text or "")[:300])
        return ok, (None if ok else (resp.text or "")[:300])
    except Exception as exc:
        log.warning("telegram.send_error", error=str(exc))
        return False, str(exc)


def client_prefix(campaign_name: str) -> str:
    """Extrai o prefixo [X] do nome da campanha (mesma regra do copy_optimizer).
    Sem prefixo -> 'SEM_PREFIXO'."""
    m = re.match(r"\s*\[([^\]]+)\]", campaign_name or "")
    return m.group(1).strip().upper() if m else "SEM_PREFIXO"


def escape_html(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
