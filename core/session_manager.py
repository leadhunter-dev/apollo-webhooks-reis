import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

import structlog

from core import config

log = structlog.get_logger()


class PlaywrightSessionManager:
    """Gerencia autenticação e persistência de sessão por cliente."""

    def __init__(self, sessions_dir: str = None):
        self.sessions_dir = Path(sessions_dir or config.SESSIONS_DIR)
        self.sessions_dir.mkdir(parents=True, exist_ok=True)

    def get_session_path(self, client_id: str) -> Path:
        """Returns Path('.sessions/{client_id}_playwright.json')."""
        return self.sessions_dir / f"{client_id}_playwright.json"

    def get_profile_dir(self, client_id: str) -> Path:
        """Diretório do perfil Chrome persistente — cofre ÚNICO de login.

        Usado tanto pelos comandos do main.py (via get_authenticated_page)
        quanto pelo viewer scripts/open_apollo.py. O login fica gravado em
        disco aqui e sobrevive a fechar/reabrir o navegador.
        """
        return self.sessions_dir / f"{client_id}_chrome_profile"

    def has_valid_session_file(self, client_id: str) -> bool:
        """Returns True if the session file exists."""
        return self.get_session_path(client_id).exists()

    def save_session(self, client_id: str, context) -> None:
        """Saves context.storage_state() to JSON file."""
        storage_state = context.storage_state()
        session_path = self.get_session_path(client_id)
        with open(session_path, "w", encoding="utf-8") as f:
            json.dump(storage_state, f, indent=2)

    def load_session(self, client_id: str, context) -> bool:
        """
        Loads storage_state from file into context if it exists.
        Returns True if loaded, False if file does not exist.
        """
        session_path = self.get_session_path(client_id)
        if not session_path.exists():
            return False
        with open(session_path, "r", encoding="utf-8") as f:
            storage_state = json.load(f)
        context.add_cookies(storage_state.get("cookies", []))
        return True

    def invalidate_session(self, client_id: str) -> None:
        """Deletes the session file if it exists."""
        session_path = self.get_session_path(client_id)
        if session_path.exists():
            session_path.unlink()

    def get_authenticated_page(self, client_id: str, playwright_instance,
                                email: str = None, password: str = None):
        """
        Returns (browser, context, page) authenticated.
        - If saved session exists: loads and checks if Apollo is logged in.
        - If email+password provided: auto-fills login form.
        - Otherwise: headful mode, waits for manual login, saves session.
        - Do not use during unit tests (use mock instead).
        """
        # Cofre ÚNICO de login: perfil Chrome persistente. O login fica gravado
        # em disco e sobrevive a fechar/reabrir — não há mais o storage_state
        # JSON como fonte da verdade (evita o "cofre duplo" que divergia).
        profile_dir = self.get_profile_dir(client_id)
        profile_dir.mkdir(parents=True, exist_ok=True)

        # Use real Chrome (not Chromium) to avoid Cloudflare bot detection.
        launch_kwargs = dict(
            headless=False,  # never headless — Cloudflare blocks headless reliably
            args=config.chrome_launch_args(),
        )
        try:
            context = playwright_instance.chromium.launch_persistent_context(
                str(profile_dir), channel="chrome", **launch_kwargs
            )
        except Exception:
            # channel=chrome pode falhar por Chrome ausente OU perfil travado.
            # Tenta o Chromium padrão; se também falhar no MESMO perfil, é lock.
            try:
                context = playwright_instance.chromium.launch_persistent_context(
                    str(profile_dir), **launch_kwargs
                )
            except Exception as exc2:
                raise RuntimeError(
                    f"[auth] Já há uma janela do Apollo aberta no perfil "
                    f"'{client_id}'. Feche-a (ou o scripts/open_apollo.py) e "
                    f"rode de novo."
                ) from exc2

        page = context.pages[0] if context.pages else context.new_page()
        # Remove webdriver flag to avoid bot detection
        page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        page.goto(config.APOLLO_IO_URL)

        def _active_page():
            """Returns the most recently opened page in the context."""
            pages = context.pages
            return pages[-1] if pages else page

        def _bring_to_front():
            """Opcional: traz Chrome pra frente (macOS). Desligado por padrão."""
            if not config.should_bring_browser_to_front():
                return
            if sys.platform != "darwin":
                return
            try:
                subprocess.run(
                    ["osascript", "-e",
                     'tell application "Google Chrome" to activate'],
                    timeout=5, capture_output=True,
                )
            except Exception:
                pass

        def _is_logged_in(timeout_ms: int = 5000) -> bool:
            """
            Poll until Apollo SPA shows a non-login page, or timeout.

            Sinal CONFIÁVEL = rota real do Apollo (não /login). NÃO usamos o
            cookie `_leadgenie_session` como atalho: ele persiste mesmo após a
            sessão expirar no servidor, dando falso-positivo de "logado".
            """
            deadline = time.time() + (timeout_ms / 1000)
            while time.time() < deadline:
                try:
                    p = _active_page()
                    # URL REAL da SPA: o Apollo roteia por hash sem navegação de
                    # documento, então p.url fica congelado em '#/'. Lemos
                    # location.href ao vivo (leitura simples, não trava).
                    try:
                        current_url = (p.evaluate("() => window.location.href") or "").lower()
                    except Exception:
                        current_url = (p.url or "").lower()
                except Exception:
                    time.sleep(0.5)
                    continue

                if "app.apollo.io" not in current_url:
                    time.sleep(0.5)
                    continue

                # Still on login/auth page → keep waiting
                if any(token in current_url for token in ["login", "sign_in", "sign-in", "authenticate"]):
                    time.sleep(0.5)
                    continue

                # If there's a hash fragment, skip bare loading states (#/ or #!/)
                if "#" in current_url:
                    hash_part = current_url.split("#")[-1]
                    if hash_part in ("", "/", "!/"):
                        time.sleep(0.5)
                        continue

                # Any other Apollo route (with or without hash) = authenticated
                return True

            return False

        # Caminho feliz: o perfil persistente já está logado → segue direto.
        if _is_logged_in(timeout_ms=12000):
            self.save_session(client_id, context)  # export JSON (backup opcional)
            return context, context, _active_page()

        # Não logado (sessão do servidor expirou) → re-login no MESMO perfil,
        # que grava o novo login em disco de forma durável.
        if email and password:
            print(f"[auth] Logging in as {email}…")
            try:
                page.wait_for_selector('input[type="email"], input[name="email"]', timeout=10000)
                page.fill('input[type="email"], input[name="email"]', email)
                page.fill('input[type="password"]', password)
                page.keyboard.press("Enter")
                print("[auth] Credenciais preenchidas. Aguardando Apollo carregar…")
                if not _is_logged_in(timeout_ms=60000):
                    print("[auth] MFA ou verificação adicional detectada. Aguardando ação manual (2 min)…")
                    _bring_to_front()
                    if not _is_logged_in(timeout_ms=120000):
                        raise TimeoutError("[auth] Timeout aguardando login.")
            except TimeoutError:
                raise
            except Exception as e:
                print(f"[auth] Erro no login automático: {e}")
                print("[auth] Faça login manualmente na janela aberta.")
                _bring_to_front()
                if not _is_logged_in(timeout_ms=120000):
                    raise TimeoutError("[auth] Timeout aguardando login manual.")
        else:
            print(f"[auth] Browser aberto. Faça login no Apollo.io para '{client_id}'.")
            print("[auth] Você tem 5 minutos. O login fica salvo no perfil — só precisa fazer isso quando a sessão expirar.")
            if config.should_bring_browser_to_front():
                _bring_to_front()
            else:
                print("[auth] Janela do Apollo aberta em segundo plano (sem roubar foco). "
                      "Use APOLLO_BRING_TO_FRONT=1 se precisar dela na frente.", flush=True)
            if not _is_logged_in(timeout_ms=300000):
                raise TimeoutError("[auth] Timeout aguardando login manual (5 min).")

        self.save_session(client_id, context)
        print(f"[auth] Login salvo no perfil persistente de '{client_id}'.")

        return context, context, _active_page()

    def capture_api_session(self, page) -> dict:
        """
        Intercept Apollo's internal API requests to capture x-csrf-token.

        Navigates to #/sequences and waits for Apollo to fire an authenticated
        API request. Returns {"csrf_token": str|None, "cookies": list}.

        IMPORTANT: call this AFTER get_authenticated_page() while the browser
        is still open.
        """
        csrf_token = None

        def _on_request(request):
            nonlocal csrf_token
            if csrf_token is None and "apollo.io/api" in request.url:
                token = request.headers.get("x-csrf-token")
                if token:
                    csrf_token = token

        page.on("request", _on_request)

        # Navigate to a page that triggers authenticated API calls
        try:
            page.goto("https://app.apollo.io/#/sequences", wait_until="domcontentloaded", timeout=20000)
            # Poll up to 15 seconds
            deadline = time.time() + 15
            while csrf_token is None and time.time() < deadline:
                page.wait_for_timeout(300)
        except Exception as exc:
            log.warning("session_manager.csrf_capture_error", error=str(exc))

        # Remove listener
        try:
            page.remove_listener("request", _on_request)
        except Exception:
            pass

        if csrf_token:
            log.info("session_manager.csrf_captured")
        else:
            # Fallback: read X-CSRF-TOKEN directly from browser cookies
            try:
                cookies = page.context.cookies()
                for c in cookies:
                    if c.get("name", "").upper() in ("X-CSRF-TOKEN", "XCSRF-TOKEN", "CSRF-TOKEN"):
                        import urllib.parse as _urlparse
                        csrf_token = _urlparse.unquote(c["value"])
                        log.info("session_manager.csrf_from_cookie", token_prefix=csrf_token[:10])
                        break
            except Exception as exc:
                log.warning("session_manager.csrf_cookie_error", error=str(exc))

        if not csrf_token:
            log.warning("session_manager.csrf_not_captured")

        return {"csrf_token": csrf_token}
