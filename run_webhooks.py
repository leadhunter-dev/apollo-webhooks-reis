#!/usr/bin/env python3
"""CLI standalone do motor de webhooks Apollo → Leavo (conta reis_office).

Uso:
  python3 run_webhooks.py run --dry-run     # mostra o que dispararia, sem POST nem gravar dedup
  python3 run_webhooks.py run               # dispara de verdade
  python3 run_webhooks.py check-tracking    # lê se o rastreamento de abertura está ligado
  python3 run_webhooks.py enable-tracking   # liga o rastreamento (necessário p/ 'abriu 2x')

'run' usa a API pública (sem login) se houver APOLLO_API_KEY_REIS_OFFICE no
.env — cobre 'abriu 2x' por completo + aviso rápido de resposta (sem texto).
Sem API key, cai pro caminho de browser (login manual), que também classifica
resposta como positiva de verdade (lê o texto) e consegue revelar telefone
novo do lead (~8 créditos da própria conta).
check-tracking / enable-tracking sempre precisam de browser — não existem na
API pública do Apollo.
"""
from __future__ import annotations

import argparse
import sys

from core import apollo_api
from core import event_webhooks as ew

DEFAULT_CLIENT = "reis_office"


def _print_run_result(result, tag: str, client: str) -> None:
    print(f"\n[webhooks {tag}] cliente={client}")
    if not result.fires:
        print("  Nenhum evento elegível encontrado.")
    for f in result.fires:
        if result.dry_run and f.skipped_reason == "dry_run":
            mark = "→ DISPARARIA"
        elif f.delivered:
            mark = f"✓ enviado ({f.status_code})"
        elif f.skipped_reason == "already_fired":
            mark = "· já disparado"
        else:
            mark = f"✗ falhou ({f.status_code}) {f.error or ''}"
        print(f"  [{f.event}] {f.contact_email or f.contact_id} | {f.campaign_name} — {mark}")
    print(f"\n  Resumo: {result.counts()}")


def cmd_run(client: str, dry_run: bool) -> None:
    if apollo_api.is_configured(client):
        fwd = ew.EventWebhookForwarder(client, dry_run=dry_run)
        result = fwd.run_via_api(client)
        _print_run_result(result, "DRY-RUN · API" if dry_run else "LIVE · API", client)
        return

    from playwright.sync_api import sync_playwright

    from core.session_manager import PlaywrightSessionManager

    with sync_playwright() as p:
        manager = PlaywrightSessionManager()
        browser, context, page = manager.get_authenticated_page(client, p)
        try:
            fwd = ew.EventWebhookForwarder(client, dry_run=dry_run)
            result = fwd.run(page, context=context)
            _print_run_result(result, "DRY-RUN" if dry_run else "LIVE", client)
            try:
                manager.save_session(client, context)
            except Exception:
                pass
        finally:
            browser.close()


def cmd_check_tracking(client: str) -> None:
    from playwright.sync_api import sync_playwright

    from core import apollo_fetch as api
    from core.session_manager import PlaywrightSessionManager

    with sync_playwright() as p:
        manager = PlaywrightSessionManager()
        browser, context, page = manager.get_authenticated_page(client, p)
        try:
            csrf = api.capture_csrf(page)
            state = ew.read_tracking_state(page, csrf)
            print(f"\n[webhooks check-tracking] cliente={client}")
            print(f"  user_id               = {state.get('user_id')}")
            print(f"  enable_open_tracking  = {state.get('enable_open_tracking')}")
            print(f"  enable_click_tracking = {state.get('enable_click_tracking')}")
            if not state.get("enable_open_tracking"):
                print("\n  ⚠️  Open tracking DESLIGADO — o evento 'abriu 2x' não terá dados.")
                print("     Ligue com: python3 run_webhooks.py enable-tracking")
            try:
                manager.save_session(client, context)
            except Exception:
                pass
        finally:
            browser.close()


def cmd_enable_tracking(client: str) -> None:
    from playwright.sync_api import sync_playwright

    from core import apollo_fetch as api
    from core.session_manager import PlaywrightSessionManager

    with sync_playwright() as p:
        manager = PlaywrightSessionManager()
        browser, context, page = manager.get_authenticated_page(client, p)
        try:
            csrf = api.capture_csrf(page)
            state = ew.read_tracking_state(page, csrf)
            uid = state.get("user_id")
            if state.get("enable_open_tracking") is True:
                print(f"[webhooks] Open tracking JÁ está ligado (user {uid}). Nada a fazer.")
            else:
                print(f"[webhooks] Ligando open tracking (user {uid})…")
                res = ew.enable_open_tracking(page, csrf, uid)
                if res.get("ok"):
                    print(f"  ✓ Ligado via {res.get('endpoint_used')}. "
                          f"antes={res['before'].get('enable_open_tracking')} "
                          f"depois={res['after'].get('enable_open_tracking')}")
                    print("  Obs.: só vale para emails enviados a partir de agora.")
                else:
                    print("  ✗ Não consegui ligar via API (endpoint não confirmado).")
                    print("     Faça manual no Apollo: Settings → Mailboxes/Email → 'Track email opens'.")
            try:
                manager.save_session(client, context)
            except Exception:
                pass
        finally:
            browser.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Webhooks Apollo → Leavo (reis_office)")
    parser.add_argument("--client", default=DEFAULT_CLIENT, help=f"Client ID (default: {DEFAULT_CLIENT})")
    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="Detectar eventos e disparar os webhooks (dedup por contato)")
    run_p.add_argument("--dry-run", action="store_true", help="Mostra o que dispararia, sem POST nem gravar dedup")

    sub.add_parser("check-tracking", help="Ler estado do rastreamento de abertura")
    sub.add_parser("enable-tracking", help="Ligar rastreamento de abertura (necessário p/ 'abriu 2x')")

    args = parser.parse_args()

    if args.command == "run":
        cmd_run(args.client, args.dry_run)
    elif args.command == "check-tracking":
        cmd_check_tracking(args.client)
    elif args.command == "enable-tracking":
        cmd_enable_tracking(args.client)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
