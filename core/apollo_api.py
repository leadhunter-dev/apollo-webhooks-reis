"""Cliente fino da API PÚBLICA do Apollo (api.apollo.io/api/v1) via API key.

Mesma família de endpoints/schema que `core/apollo_fetch.py` usa via sessão de
browser (é o mesmo backend) — mas aqui autentica com `X-Api-Key` e não precisa
de Playwright/login algum. Retorna o mesmo formato `{status, data, ok}` de
`apollo_fetch.apollo_request`, então quem já lê `res.get("data")` funciona
igual trocando só a origem da chamada.

Cobre: relatório de campanha (stats já vêm no objeto da campanha), quem abriu
quais steps, quem respondeu (`replied=true` na mensagem enviada). NÃO cobre:
texto da resposta / classificação de sentimento — a API pública explicitamente
não devolve emails de entrada (ver RUNBOOK, seção Telegram). Isso continua
via `core/apollo_fetch.py` + sessão de browser.
"""
from __future__ import annotations

from typing import Any, Optional

import requests as _requests
import structlog

from core import config

log = structlog.get_logger()

BASE_URL = "https://api.apollo.io/api/v1"


def is_configured(account: str) -> bool:
    return bool(config.apollo_api_key(account))


def apollo_request(account: str, method: str, path: str,
                   payload: Optional[dict] = None) -> dict[str, Any]:
    api_key = config.apollo_api_key(account)
    if not api_key:
        return {"status": -1, "data": None, "ok": False, "error": f"sem API key para '{account}'"}
    if not path.startswith("/"):
        path = "/" + path
    url = BASE_URL + path
    try:
        resp = _requests.request(
            method.upper(), url,
            headers={"X-Api-Key": api_key, "Content-Type": "application/json",
                     "Accept": "application/json"},
            json=payload if payload is not None and method.upper() not in ("GET", "HEAD") else None,
            params=payload if payload is not None and method.upper() in ("GET", "HEAD") else None,
            timeout=20,
        )
        try:
            data = resp.json()
        except Exception:
            data = None
        return {"status": resp.status_code, "data": data, "ok": resp.ok}
    except Exception as exc:
        log.warning("apollo_api.request_error", account=account, path=path, error=str(exc))
        return {"status": -1, "data": None, "ok": False, "error": str(exc)}


def get(account: str, path: str, params: Optional[dict] = None) -> dict[str, Any]:
    return apollo_request(account, "GET", path, params)


def post(account: str, path: str, payload: Optional[dict] = None) -> dict[str, Any]:
    return apollo_request(account, "POST", path, payload)


# ---------------------------------------------------------------------------
# Helpers de alto nível
# ---------------------------------------------------------------------------

def list_active_campaigns(account: str) -> dict[str, dict]:
    """{campaign_id: campaign_dict} das campanhas ATIVAS (active && !archived)."""
    found: dict[str, dict] = {}
    for pg in range(1, 12):
        res = post(account, "/emailer_campaigns/search", {"page": pg, "per_page": 100})
        if not res.get("ok"):
            log.warning("apollo_api.list_campaigns_error", account=account, status=res.get("status"))
            break
        camps = (res.get("data") or {}).get("emailer_campaigns") or []
        for c in camps:
            if c.get("active") and not c.get("archived"):
                found[c["id"]] = c
        if len(camps) < 100:
            break
    return found


def get_campaign(account: str, campaign_id: str) -> dict:
    """Detalhe + stats agregados de UMA campanha (mesmos campos do objeto de
    /search, sempre atualizado)."""
    res = get(account, f"/emailer_campaigns/{campaign_id}")
    if not res.get("ok"):
        return {}
    return (res.get("data") or {}).get("emailer_campaign") or {}


def search_messages(account: str, campaign_id: str, stats: list[str],
                    page: int = 1, per_page: int = 100) -> list[dict]:
    """Mensagens ENVIADAS que casam o(s) stat(s) (ex.: 'opened', 'replied').
    NÃO inclui o texto de resposta do lead — só a mensagem que ENVIAMOS."""
    res = post(account, "/emailer_messages/search", {
        "emailer_campaign_id": campaign_id,
        "emailer_message_stats": stats,
        "page": page, "per_page": per_page,
    })
    if not res.get("ok"):
        return []
    return (res.get("data") or {}).get("emailer_messages") or []


def search_messages_all_pages(account: str, campaign_id: str, stats: list[str],
                              max_pages: int = 20) -> list[dict]:
    out: list[dict] = []
    for pg in range(1, max_pages + 1):
        msgs = search_messages(account, campaign_id, stats, page=pg, per_page=100)
        out.extend(msgs)
        if len(msgs) < 100:
            break
    return out
