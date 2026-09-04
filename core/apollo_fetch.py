"""Fetch same-origin contra a API interna do Apollo (via browser autenticado).

Padrão do projeto: roda `fetch()` DENTRO da página Playwright, herda cookies
e usa x-csrf-token. Sem crédito. Prefixos sempre `/api/v1/...`.
"""
from __future__ import annotations

import time
import urllib.parse
from typing import Any, Optional

_FETCH_JS = """
async ({method, path, payload, csrf}) => {
  try {
    const opts = {
      method: method || 'GET',
      headers: {
        'Accept': 'application/json',
        'x-csrf-token': csrf || '',
      },
      credentials: 'include',
    };
    if (payload !== null && payload !== undefined && method !== 'GET' && method !== 'HEAD') {
      opts.headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(payload);
    }
    const resp = await fetch(path, opts);
    let data = null;
    try { data = await resp.json(); } catch (e) {}
    return {status: resp.status, data, ok: resp.ok};
  } catch (e) {
    return {status: -1, data: null, ok: false, error: String(e)};
  }
}
"""


def csrf_from_cookies(page) -> str:
    try:
        for c in page.context.cookies():
            if c.get("name", "").upper() in ("X-CSRF-TOKEN", "XCSRF-TOKEN", "CSRF-TOKEN"):
                return urllib.parse.unquote(c["value"])
    except Exception:
        pass
    return ""


def capture_csrf(page, navigate_to: str = "https://app.apollo.io/#/sequences",
                 timeout_s: float = 25.0) -> str:
    """Navega e captura x-csrf-token de uma request real do Apollo."""
    captured = {"csrf": ""}

    def _on(req):
        t = req.headers.get("x-csrf-token")
        if t and not captured["csrf"]:
            captured["csrf"] = t

    page.on("request", _on)
    try:
        try:
            page.goto(navigate_to, wait_until="domcontentloaded", timeout=45000)
        except Exception:
            pass
        deadline = time.time() + timeout_s
        while not captured["csrf"] and time.time() < deadline:
            page.wait_for_timeout(350)
    finally:
        try:
            page.remove_listener("request", _on)
        except Exception:
            pass
    return captured["csrf"] or csrf_from_cookies(page)


def apollo_request(
    page,
    method: str,
    path: str,
    csrf: str,
    payload: Optional[dict] = None,
) -> dict[str, Any]:
    """Executa request. path deve ser `/api/v1/...`."""
    if not path.startswith("/"):
        path = "/" + path
    if not path.startswith("/api/v1/"):
        path = "/api/v1" + path if path.startswith("/") else "/api/v1/" + path
    try:
        return page.evaluate(_FETCH_JS, {
            "method": method.upper(),
            "path": path,
            "payload": payload,
            "csrf": csrf or "",
        }) or {"status": -1, "data": None, "ok": False}
    except Exception as exc:
        return {"status": -1, "data": None, "ok": False, "error": str(exc)}


def get(page, path: str, csrf: str) -> dict[str, Any]:
    return apollo_request(page, "GET", path, csrf)


def post(page, path: str, csrf: str, payload: Optional[dict] = None) -> dict[str, Any]:
    return apollo_request(page, "POST", path, csrf, payload)


def patch(page, path: str, csrf: str, payload: Optional[dict] = None) -> dict[str, Any]:
    return apollo_request(page, "PATCH", path, csrf, payload)
