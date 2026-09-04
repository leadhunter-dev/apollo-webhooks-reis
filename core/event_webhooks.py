"""EventWebhookForwarder — dispara webhooks (Leavo) a partir de eventos das
campanhas do Apollo, lendo a API interna (custo zero de crédito).

Dois eventos, por cliente/config em `clients/<client>_webhooks.json`:

  * ``positive_reply``   → contato respondeu e o texto classifica como
    ``interested`` (via `core.reply_classifier.categorize_reply`, PT+EN).
  * ``opened_multiple``  → "abriu 2x": o contato abriu a MESMA mensagem
    ``>= min_opens`` vezes  OU  abriu a mensagem do step ``trigger_step_position``.
    Requer rastreamento de abertura LIGADO no Apollo (`num_opens` só é
    preenchido quando `open_tracking_enabled`). Ver `webhooks check-tracking`.

Dedup: cada (contato, evento) dispara UMA vez. Estado em
`data/ops/webhook_fired/<client>.json` — idempotente entre execuções.

Endpoints internos usados (sessão browser same-origin, `/api/v1/...`):
  POST /emailer_messages/search   (stat 'opened' → num_opens / campaign_position)
  + reuso de `ResponseHandler._fetch_replies_via_api` para respostas + corpo.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import requests as _requests
import structlog

from core import apollo_fetch as api
from core import config
from core import telegram as tg

log = structlog.get_logger()

FIRED_DIR = Path(config.DATA_DIR) / "ops" / "webhook_fired"

EVENT_POSITIVE_REPLY = "positive_reply"
EVENT_OPENED_MULTIPLE = "opened_multiple"
EVENT_REPLY_PING = "reply_ping"  # "alguém respondeu" via API, sem texto/classificação


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_webhook_config(client_id: str) -> dict:
    """Carrega clients/<client_id>_webhooks.json."""
    path = Path(config.CLIENTS_DIR) / f"{client_id}_webhooks.json"
    if not path.exists():
        raise FileNotFoundError(f"Webhook config não encontrada: {path}")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _format_date(iso_or_raw: str) -> str:
    """'2026-08-12 14:03' ou ISO -> 'qui 12/08 14:03'. Best-effort: se não
    parsear, devolve a string original."""
    if not iso_or_raw:
        return ""
    raw = iso_or_raw.replace("T", " ").strip()
    dt = None
    for length, fmt in ((19, "%Y-%m-%d %H:%M:%S"), (16, "%Y-%m-%d %H:%M")):
        try:
            dt = datetime.strptime(raw[:length], fmt)
            break
        except ValueError:
            continue
    if dt is None:
        return iso_or_raw
    dias = ["seg", "ter", "qua", "qui", "sex", "sáb", "dom"]
    return f"{dias[dt.weekday()]} {dt.strftime('%d/%m %H:%M')}"


def _contact_link(contact_id: str) -> str:
    return f"https://app.apollo.io/#/contacts/{contact_id}" if contact_id else ""


def _build_telegram_message(client_id: str, fire: "WebhookFire") -> str:
    """Mensagem detalhada, tagueada por [CLIENTE] + categoria, pro Telegram:
    empresa, cargo, telefone (se enriquecido), link direto pro contato."""
    tag = tg.client_prefix(fire.campaign_name)
    name = tg.escape_html(fire.contact_name or "(sem nome)")
    email = tg.escape_html(fire.contact_email or "")
    camp = tg.escape_html(fire.campaign_name or "")
    contact = fire.payload.get("contact", {}) or {}
    title = tg.escape_html(contact.get("title") or "")
    company = tg.escape_html(contact.get("company") or "")
    phone = contact.get("phone") or ""
    link = _contact_link(fire.contact_id)

    who_lines = [f"👤 <b>{name}</b> &lt;{email}&gt;"]
    role_bits = " · ".join(x for x in (title, company) if x)
    if role_bits:
        who_lines.append(f"🏢 {role_bits}")
    if phone:
        who_lines.append(f"📱 {tg.escape_html(phone)}")

    if fire.event == EVENT_POSITIVE_REPLY:
        reply = fire.payload.get("reply", {}) or {}
        snip = tg.escape_html(" ".join((reply.get("snippet") or "").split())[:500])
        subject = tg.escape_html(reply.get("subject") or "")
        when = _format_date(reply.get("date") or "")
        lines = [f"🟢 <b>[{tag}]</b> Resposta positiva", *who_lines, f"📋 {camp}"]
        if subject:
            lines.append(f"✉️ {subject}")
        if when:
            lines.append(f"🕒 {when}")
        if snip:
            lines.append(f"💬 “{snip}”")
    elif fire.event == EVENT_REPLY_PING:
        step = fire.payload.get("reply", {}).get("step_position")
        lines = [f"📩 <b>[{tag}]</b> Alguém respondeu (aguardando classificação)",
                 *who_lines, f"📋 {camp}"]
        if step:
            lines.append(f"↳ step {step}")
        lines.append("⏳ o texto/classificação chega num próximo ciclo (sessão de browser)")
    else:  # opened_multiple
        o = fire.payload.get("opens", {}) or {}
        steps = o.get("distinct_steps") or []
        subjects = o.get("step_subjects") or {}
        lines = [f"👀 <b>[{tag}]</b> Abriu {len(steps)}x", *who_lines, f"📋 {camp}"]
        for pos in steps:
            subj = tg.escape_html(subjects.get(pos, "") or subjects.get(str(pos), ""))
            lines.append(f"  · step {pos}{': ' + subj if subj else ''}")

    if link:
        lines.append(f'🔗 <a href="{link}">abrir contato no Apollo</a>')
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Result models
# ---------------------------------------------------------------------------

@dataclass
class WebhookFire:
    event: str
    contact_id: str
    contact_email: str
    contact_name: str
    campaign_id: str
    campaign_name: str
    payload: dict = field(default_factory=dict)
    channel: str = "webhook"   # 'webhook' | 'email'
    target: str = ""           # URL (webhook) ou email (digest)
    ident: str = ""            # identidade de dedup (email|contact_id)
    delivered: bool = False
    status_code: Optional[int] = None
    error: Optional[str] = None
    skipped_reason: Optional[str] = None  # 'already_fired' | 'dry_run' | 'disabled'


@dataclass
class ForwarderRun:
    client_id: str
    dry_run: bool = False
    fires: list = field(default_factory=list)

    def counts(self) -> dict:
        c: dict[str, int] = {}
        for f in self.fires:
            key = f.event if f.delivered else (f.skipped_reason or "failed")
            c[key] = c.get(key, 0) + 1
        return c


# ---------------------------------------------------------------------------
# Pure logic (testável sem browser/rede)
# ---------------------------------------------------------------------------

def opened_event_from_messages(
    messages: list[dict],
    min_distinct_steps: int = 2,
    require_tracked: bool = True,
) -> Optional[dict]:
    """Dado as mensagens ABERTAS de um contato numa campanha, decide se o evento
    de engajamento dispara. Regra: o lead abriu emails de **>= min_distinct_steps
    steps (posições) DISTINTOS**.

    Por que presença (e não contagem): o Apollo NÃO expõe aberturas por mensagem
    (`num_opens` volta 0 na API interna). Só sabemos SE uma mensagem foi aberta
    (ela aparece no filtro 'opened'). Cada `m` aqui já é uma mensagem aberta.

    `require_tracked` descarta "aberturas fantasma": com o tracking off, o Apollo
    conta *resposta* como abertura (open_tracking_enabled=False). Exigindo
    open_tracking_enabled=True, contamos só aberturas reais rastreadas.

    Retorna {distinct_steps, num_distinct_steps} ou None.
    """
    positions: set[int] = set()
    for m in messages:
        if require_tracked and not m.get("open_tracking_enabled"):
            continue
        pos = m.get("campaign_position")
        try:
            pos = int(pos) if pos is not None else None
        except (TypeError, ValueError):
            pos = None
        if pos is not None:
            positions.add(pos)

    if len(positions) >= min_distinct_steps:
        return {
            "distinct_steps": sorted(positions),
            "num_distinct_steps": len(positions),
        }
    return None


def _clean_meta(contact: dict) -> dict:
    """Extrai {first_name, last_name, company, title} de um registro de
    contato do Apollo (mesmo shape em /contacts/search, interno ou público).
    Só inclui campos NÃO-VAZIOS — nunca sobrescreve um campo já preenchido no
    CRM de destino com string em branco."""
    out = {}
    for src_key, out_key in (
        ("first_name", "first_name"), ("last_name", "last_name"),
        ("organization_name", "company"), ("title", "title"),
    ):
        val = (contact.get(src_key) or "").strip()
        if val:
            out[out_key] = val
    return out


def select_best_phone(phone_numbers: list[dict]) -> Optional[dict]:
    """Escolhe o telefone PESSOAL do contato (nunca o geral da empresa).

    Apollo devolve vários tipos em `phone_numbers` — inclusive `work_hq`, que é
    o telefone da RECEPÇÃO da empresa, não da pessoa. Mandar isso pro CRM como
    "telefone do lead" engana quem for ligar (liga na recepção achando que é
    direto). Por isso: só `mobile`/`direct`/`work_direct`; nunca `work_hq` nem
    tipos vazios. Se só houver o da empresa, retorna None (nada é melhor que
    dado enganoso).
    """
    PESSOAL = ("mobile", "direct", "work_direct", "personal")
    for tipo in PESSOAL:
        for p in phone_numbers or []:
            if (p.get("type") or "").lower() == tipo and p.get("sanitized_number"):
                return {"number": p.get("raw_number", ""), "type": tipo,
                        "status": p.get("status", "")}
    return None


def build_reply_payload(client_id: str, campaign_id: str, campaign_name: str, reply: dict,
                        phone: Optional[dict] = None) -> dict:
    body = reply.get("body", "") or ""
    contact = {
        "id": reply.get("contact_id", ""),
        "name": reply.get("name", ""),
        "email": reply.get("email", ""),
    }
    if phone:
        contact["phone"] = phone["number"]
        contact["phone_type"] = phone["type"]
    return {
        "event": EVENT_POSITIVE_REPLY,
        "client": client_id,
        "campaign_id": campaign_id,
        "campaign_name": campaign_name,
        "contact": contact,
        "reply": {
            "subject": reply.get("subject", ""),
            "snippet": body[:500],
            "date": reply.get("date", ""),
        },
        "fired_at": _now_iso(),
        "source": "apollo-ops",
    }


def build_reply_ping_payload(client_id: str, campaign_id: str, campaign_name: str,
                             contact_id: str, name: str, email: str,
                             step_position: Optional[int] = None) -> dict:
    """Payload do aviso 'alguém respondeu' via API pública — sem texto (a API
    pública nunca devolve o email de entrada). Vira `positive_reply` de
    verdade só depois do job de classificação via browser (ver RUNBOOK)."""
    return {
        "event": EVENT_REPLY_PING,
        "client": client_id,
        "campaign_id": campaign_id,
        "campaign_name": campaign_name,
        "contact": {"id": contact_id, "name": name, "email": email},
        "reply": {"step_position": step_position},
        "fired_at": _now_iso(),
        "source": "apollo-ops-api",
    }


def build_opened_payload(
    client_id: str, campaign_id: str, campaign_name: str, contact: dict, summary: dict,
    phone: Optional[dict] = None, step_subjects: Optional[dict] = None,
) -> dict:
    contact_out = {
        "id": contact.get("id", ""),
        "name": contact.get("name", ""),
        "email": contact.get("email", ""),
    }
    if phone:
        contact_out["phone"] = phone["number"]
        contact_out["phone_type"] = phone["type"]
    opens_out = dict(summary)
    if step_subjects:
        opens_out["step_subjects"] = {
            pos: step_subjects.get(pos, "") for pos in summary.get("distinct_steps", [])
        }
    return {
        "event": EVENT_OPENED_MULTIPLE,
        "client": client_id,
        "campaign_id": campaign_id,
        "campaign_name": campaign_name,
        "contact": contact_out,
        "opens": opens_out,
        "fired_at": _now_iso(),
        "source": "apollo-ops",
    }


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def event_identity(email: str, contact_id: str) -> str:
    """Identidade estável p/ dedup: email (lower) quando houver, senão contact_id.

    Evita disparar o mesmo lead várias vezes quando ele existe como vários
    registros de contato (um por campanha)."""
    e = (email or "").strip().lower()
    return e or (contact_id or "")


def is_own_domain(email: str, own_domains: set[str]) -> bool:
    """True se o email pertence a um domínio (ou subdomínio) do próprio cliente."""
    if not own_domains or not email:
        return False
    domain = email.split("@", 1)[-1].lower()
    return any(domain == od or domain.endswith("." + od) for od in own_domains)


def _best_body(msg: dict) -> str:
    """Corpo da resposta: prefere o texto mais completo entre body_text e o
    body_html despido de tags (espelha ResponseHandler)."""
    import html as _html
    import re as _re

    text = (msg.get("body_text") or "").strip()
    html_raw = (msg.get("body_html") or "").strip()
    if html_raw:
        stripped = _re.sub(r"<[^>]+>", " ", html_raw)
        stripped = _html.unescape(stripped)
        stripped = _re.sub(r"[ \t]+", " ", stripped).strip()
        stripped = _re.sub(r"\n{3,}", "\n\n", stripped)
    else:
        stripped = ""
    return stripped if len(stripped) > len(text) else text


def _build_digest(client_id: str, fires: list) -> tuple[str, str]:
    """Monta (assunto, corpo) do email-resumo dos eventos novos."""
    n = len(fires)
    reps = [f for f in fires if f.event == EVENT_POSITIVE_REPLY]
    opens = [f for f in fires if f.event == EVENT_OPENED_MULTIPLE]
    subject = f"[Apollo Ops] {n} novo(s) evento(s) — {client_id}"

    lines = [f"Apollo Ops — {n} novo(s) evento(s) na conta '{client_id}':", ""]
    if reps:
        lines.append(f"== Respostas positivas ({len(reps)}) ==")
        for f in reps:
            snip = (f.payload.get("reply", {}) or {}).get("snippet", "")
            snip = " ".join(snip.split())[:200]
            lines.append(f"  • {f.contact_name or '(sem nome)'} <{f.contact_email}> — {f.campaign_name}")
            if snip:
                lines.append(f"      “{snip}”")
        lines.append("")
    if opens:
        lines.append(f"== Abriu 2+ steps ({len(opens)}) ==")
        for f in opens:
            o = f.payload.get("opens", {}) or {}
            steps = o.get("distinct_steps") or []
            lines.append(f"  • {f.contact_name or '(sem nome)'} <{f.contact_email}> — "
                         f"{f.campaign_name} (abriu steps {steps})")
        lines.append("")
    lines.append("— apollo-ops")
    return subject, "\n".join(lines)


# ---------------------------------------------------------------------------
# Dedup store (JSON por cliente)
# ---------------------------------------------------------------------------

class FiredStore:
    """Persiste (evento -> {contact_id: iso}) em data/ops/webhook_fired/<client>.json."""

    def __init__(self, client_id: str, base_dir: Path = FIRED_DIR):
        self.client_id = client_id
        self.path = Path(base_dir) / f"{client_id}.json"
        self._data: dict = {}
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            try:
                self._data = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception:
                self._data = {}
        self._data.setdefault(EVENT_POSITIVE_REPLY, {})
        self._data.setdefault(EVENT_OPENED_MULTIPLE, {})
        self._data.setdefault(EVENT_REPLY_PING, {})

    def already_fired(self, event: str, contact_id: str) -> bool:
        return contact_id in self._data.get(event, {})

    def mark(self, event: str, contact_id: str) -> None:
        self._data.setdefault(event, {})[contact_id] = _now_iso()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8"
        )


# ---------------------------------------------------------------------------
# Forwarder
# ---------------------------------------------------------------------------

class EventWebhookForwarder:
    def __init__(self, client_id: str, cfg: dict = None, dry_run: bool = False,
                 store: FiredStore = None, http=None):
        self.client_id = client_id
        self.cfg = cfg if cfg is not None else load_webhook_config(client_id)
        self.dry_run = dry_run
        self.store = store if store is not None else FiredStore(client_id)
        self._http = http or _requests
        self._campaigns: dict = self.cfg.get("campaigns", {})
        self._notify_email: str = self.cfg.get("notify_email", "") or ""
        self._telegram_enabled: bool = bool(self.cfg.get("telegram_enabled", True)) and tg.is_configured()
        self._discover: bool = bool(self.cfg.get("discover_active", False))
        self._enrich_phone: bool = bool(self.cfg.get("enrich_phone", False))
        self._seen: set = set()  # (event, identity) já tratados nesta execução
        self._own_domains: set[str] = self._load_own_domains()

    def _channel_target(self, event_cfg: dict) -> tuple[Optional[str], str]:
        """Decide o canal de entrega de um evento: webhook (se tem url) senão
        email (se há notify_email). Retorna (channel|None, target)."""
        url = event_cfg.get("url")
        if url:
            return "webhook", url
        if self._notify_email:
            return "email", self._notify_email
        return None, ""

    def _load_own_domains(self) -> set[str]:
        # Preferência: 'own_domains' na própria config de webhooks; senão, a do
        # config Apollo do cliente (clients/<id>.json).
        if "own_domains" in self.cfg:
            return {d.lower() for d in self.cfg.get("own_domains", [])}
        try:
            from core.config import get_client_apollo_config
            return {d.lower() for d in get_client_apollo_config(self.client_id).get("own_domains", [])}
        except Exception:
            return set()

    # ---- IO: fetch --------------------------------------------------------

    def _fetch_opened_messages(self, page, csrf: str, seq_id: str) -> dict[str, list]:
        """Retorna {contact_id: [message, ...]} das mensagens ABERTAS da campanha.

        Usa o filtro 'opened'; quando o tracking está ligado, essas mensagens
        trazem num_opens>=1. Pagina até esvaziar (cap defensivo de páginas).
        """
        by_contact: dict[str, list] = {}
        for pg in range(1, 21):
            res = api.post(page, "/emailer_messages/search", csrf, {
                "emailer_message_stats": ["opened"],
                "emailer_campaign_id": seq_id,
                "per_page": 100, "page": pg, "finder_version": 1,
                "context": "emailer-campaign-show-messages-page",
            })
            if res.get("status") != 200:
                log.warning("event_webhooks.opened_fetch_error",
                            seq=seq_id, status=res.get("status"))
                break
            msgs = (res.get("data") or {}).get("emailer_messages") or []
            for m in msgs:
                cid = m.get("contact_id")
                if cid:
                    by_contact.setdefault(cid, []).append(m)
            if len(msgs) < 100:
                break
        return by_contact

    def _fetch_contact_meta(self, page, csrf: str, contact_id: str) -> dict:
        """Nome/sobrenome + empresa + cargo do contato (grátis — só leitura do
        registro já existente, o contato já é nosso porque respondeu/abriu).
        Best-effort: qualquer falha retorna {} e não impede o disparo do evento.
        Campos vazios são omitidos (não sobrescreve com string em branco no CRM
        de destino — ver `_clean_meta`)."""
        try:
            res = api.post(page, "/contacts/search", csrf, {"ids": [contact_id], "per_page": 1})
            c = ((res.get("data") or {}).get("contacts") or [{}])[0]
            return _clean_meta(c)
        except Exception as exc:
            log.warning("event_webhooks.contact_meta_error", contact_id=contact_id, error=str(exc))
            return {}

    def _fetch_contact_phone(self, page, csrf: str, contact_id: str) -> Optional[dict]:
        """Telefone PESSOAL do contato, sem gastar crédito à toa.

        1) Lê o contato (grátis) — se já tem mobile/direct revelado (por esta
           conta ou por qualquer outra que já tenha revelado o mesmo `person`
           na base do Apollo), retorna na hora.
        2) Senão, abre a página do contato (`#/contacts/{id}`) — isso dispara
           o enriquecimento em segundo plano do próprio Apollo (confirmado ao
           vivo em 10/08: ~8 créditos, mesmo pool de lead credits da conta,
           SEM precisar de API key nem de conta separada). Aguarda e relê.

        Best-effort: qualquer falha retorna None e não interrompe o disparo do
        evento (telefone é enriquecimento, não deve travar resposta/abertura).
        """
        try:
            res = api.post(page, "/contacts/search", csrf, {"ids": [contact_id], "per_page": 1})
            c = ((res.get("data") or {}).get("contacts") or [{}])[0]
            phone = select_best_phone(c.get("phone_numbers") or [])
            if phone:
                return phone

            # não tem ainda — abre a página do contato pra disparar o
            # enriquecimento lazy do Apollo, espera, e relê.
            page.goto(f"https://app.apollo.io/#/contacts/{contact_id}",
                     wait_until="domcontentloaded", timeout=20000)
            page.wait_for_timeout(5000)

            res2 = api.post(page, "/contacts/search", csrf, {"ids": [contact_id], "per_page": 1})
            c2 = ((res2.get("data") or {}).get("contacts") or [{}])[0]
            return select_best_phone(c2.get("phone_numbers") or [])
        except Exception as exc:
            log.warning("event_webhooks.phone_enrich_error", contact_id=contact_id, error=str(exc))
            return None

    # ---- IO: post ---------------------------------------------------------

    def _post(self, url: str, payload: dict) -> tuple[bool, Optional[int], Optional[str]]:
        try:
            resp = self._http.post(url, json=payload, timeout=15)
            ok = 200 <= resp.status_code < 300
            return ok, resp.status_code, (None if ok else (resp.text or "")[:300])
        except Exception as exc:
            return False, None, str(exc)

    # ---- orchestration ----------------------------------------------------

    def run(self, page, context=None, only_events: Optional[set] = None) -> ForwarderRun:
        """`only_events`: subconjunto de {EVENT_POSITIVE_REPLY, EVENT_OPENED_MULTIPLE}
        pra rodar só uma parte (ex.: job de classificação via browser roda só
        respostas — 'abriu 2x' já é 100% coberto pela API, rodar nos dois
        lugares seria redundante). None = ambos (comportamento antigo)."""
        run = ForwarderRun(client_id=self.client_id, dry_run=self.dry_run)

        csrf = api.capture_csrf(page)
        if not csrf:
            log.warning("event_webhooks.no_csrf")

        # Auto-descoberta de campanhas ATIVAS (quando discover_active=true).
        if self._discover:
            self._campaigns = self._discover_active_campaigns(page, csrf)
            log.info("event_webhooks.discovered", client=self.client_id,
                     count=len(self._campaigns))

        # --- 1) Respostas positivas ---
        rep_cfg = self.cfg.get("positive_reply", {})
        rep_ch, rep_target = self._channel_target(rep_cfg)
        if (only_events is None or EVENT_POSITIVE_REPLY in only_events) \
                and rep_cfg.get("enabled") and rep_ch:
            self._run_positive_replies(page, csrf, rep_ch, rep_target, run)

        # --- 2) Abriu 2x ---
        op_cfg = self.cfg.get("opened_multiple", {})
        op_ch, op_target = self._channel_target(op_cfg)
        if (only_events is None or EVENT_OPENED_MULTIPLE in only_events) \
                and op_cfg.get("enabled") and op_ch:
            self._run_opened_multiple(page, csrf, op_cfg, op_ch, op_target, run)

        # --- entrega (webhook per-fire; email em digest) ---
        self._deliver_all(run, page, csrf)

        if not self.dry_run:
            self.store.save()
        return run

    # ------------------------------------------------------------------
    # Caminho alternativo: API pública (API key), SEM browser/login.
    # Cobre 'abriu 2x' por completo e um AVISO RÁPIDO de resposta (sem texto —
    # a API pública nunca devolve email de entrada, só a mensagem que
    # ENVIAMOS). O texto/classificação de sentimento continua vindo do job
    # browser menos frequente (ver core/orchestrator.py::_job_reply_classify).
    # ------------------------------------------------------------------

    def run_via_api(self, account: str) -> ForwarderRun:
        from core import apollo_api as apapi

        run = ForwarderRun(client_id=account, dry_run=self.dry_run)

        if self._discover or not self._campaigns:
            self._campaigns = {cid: c.get("name", "")
                               for cid, c in apapi.list_active_campaigns(account).items()}
            log.info("event_webhooks.discovered_api", client=account, count=len(self._campaigns))

        op_cfg = self.cfg.get("opened_multiple", {})
        op_ch, op_target = self._channel_target(op_cfg)
        if op_cfg.get("enabled") and op_ch:
            self._run_opened_multiple_api(account, op_cfg, op_ch, op_target, run)

        rep_cfg = self.cfg.get("positive_reply", {})
        if rep_cfg.get("enabled"):
            self._run_reply_ping_api(account, run)

        self._deliver_all_api(run, account)

        if not self.dry_run:
            self.store.save()
        return run

    def _run_opened_multiple_api(self, account: str, op_cfg, channel, target, run) -> None:
        from core import apollo_api as apapi

        min_steps = int(op_cfg.get("min_distinct_steps", 2))
        require_tracked = bool(op_cfg.get("require_tracked", True))

        for seq_id, seq_name in self._campaigns.items():
            msgs = apapi.search_messages_all_pages(account, seq_id, ["opened"])
            by_contact: dict[str, list] = {}
            for m in msgs:
                cid = m.get("contact_id")
                if cid:
                    by_contact.setdefault(cid, []).append(m)
            for cid, cmsgs in by_contact.items():
                summary = opened_event_from_messages(cmsgs, min_steps, require_tracked)
                if summary is None:
                    continue
                sample = cmsgs[0]
                email = sample.get("to_email", "")
                if is_own_domain(email, self._own_domains):
                    continue
                contact = {"id": cid, "name": sample.get("to_name", ""), "email": email}
                step_subjects: dict = {}
                for m in cmsgs:
                    pos = m.get("campaign_position")
                    try:
                        pos = int(pos) if pos is not None else None
                    except (TypeError, ValueError):
                        pos = None
                    if pos is not None and pos not in step_subjects:
                        step_subjects[pos] = (m.get("subject") or "").strip()
                fire = WebhookFire(
                    event=EVENT_OPENED_MULTIPLE,
                    contact_id=cid,
                    contact_email=email,
                    contact_name=contact["name"],
                    campaign_id=seq_id,
                    campaign_name=seq_name,
                    channel=channel, target=target,
                    payload=build_opened_payload(self.client_id, seq_id, seq_name, contact, summary,
                                                 step_subjects=step_subjects),
                )
                self._consider(fire, run)

    def _run_reply_ping_api(self, account: str, run) -> None:
        """Aviso rápido de resposta via API — SEM texto, e SEM ir pro webhook/
        email (esses são só pra resposta CONFIRMADA/classificada — ver job
        browser). Este evento é Telegram-only: channel='' faz `_deliver_all_api`
        pular webhook/email e só disparar o aviso no Telegram. Pula quem já foi
        confirmado como positivo pra não duplicar aviso."""
        from core import apollo_api as apapi

        for seq_id, seq_name in self._campaigns.items():
            for m in apapi.search_messages_all_pages(account, seq_id, ["replied"]):
                cid = m.get("contact_id", "")
                email = m.get("to_email", "")
                if not cid or is_own_domain(email, self._own_domains):
                    continue
                ident = event_identity(email, cid)
                if self.store.already_fired(EVENT_POSITIVE_REPLY, ident):
                    continue  # já confirmado/classificado — não repetir aviso
                fire = WebhookFire(
                    event=EVENT_REPLY_PING,
                    contact_id=cid,
                    contact_email=email,
                    contact_name=m.get("to_name", ""),
                    campaign_id=seq_id,
                    campaign_name=seq_name,
                    channel="", target="",
                    payload=build_reply_ping_payload(
                        self.client_id, seq_id, seq_name, cid, m.get("to_name", ""), email,
                        step_position=m.get("campaign_position"),
                    ),
                )
                self._consider(fire, run)

    def _fetch_contact_meta_api(self, account: str, contact_id: str) -> dict:
        from core import apollo_api as apapi

        res = apapi.post(account, "/contacts/search", {"ids": [contact_id], "per_page": 1})
        if not res.get("ok"):
            return {}
        c = ((res.get("data") or {}).get("contacts") or [{}])[0]
        meta = _clean_meta(c)
        # Telefone: só o que já estiver revelado (sem o gatilho de lazy-load
        # que precisa de browser) — best-effort, fica sem se não tiver.
        phone = select_best_phone(c.get("phone_numbers") or [])
        if phone:
            meta["phone"] = phone["number"]
            meta["phone_type"] = phone["type"]
        return meta

    def _deliver_all_api(self, run: ForwarderRun, account: str) -> None:
        """Mesma lógica de entrega de `_deliver_all`, mas com enriquecimento
        de contato via API (sem telefone lazy-load, que exige browser)."""
        pending = [f for f in run.fires if f.skipped_reason is None and not f.delivered]

        if self.dry_run:
            for f in pending:
                f.skipped_reason = "dry_run"
                log.info("event_webhooks.would_fire_api", evt=f.event, ch=f.channel,
                         contact=f.contact_email, campaign=f.campaign_name)
            return

        for f in pending:
            meta = self._fetch_contact_meta_api(account, f.contact_id)
            if meta:
                f.payload.setdefault("contact", {}).update(meta)

        for f in [x for x in pending if x.channel == "webhook"]:
            ok, status, err = self._post(f.target, f.payload)
            f.delivered, f.status_code, f.error = ok, status, err
            if ok:
                self.store.mark(f.event, f.ident)
                log.info("event_webhooks.fired_api", evt=f.event, contact=f.contact_email, status=status)
            else:
                log.error("event_webhooks.fire_failed_api", evt=f.event,
                          contact=f.contact_email, status=status, error=err)

        email_fires = [x for x in pending if x.channel == "email"]
        by_recipient: dict[str, list] = {}
        for f in email_fires:
            by_recipient.setdefault(f.target, []).append(f)
        for recipient, fires in by_recipient.items():
            ok, err = self._send_digest_email(recipient, fires)
            for f in fires:
                f.delivered, f.error = ok, err
                if ok:
                    self.store.mark(f.event, f.ident)

        # Ver comentário/fix em _deliver_all: Telegram só controla dedup/
        # delivered p/ fires SEM canal primário (reply_ping) — nunca esconde
        # falha do canal primário (webhook/email) atrás de um Telegram ok.
        if self._telegram_enabled:
            for f in pending:
                text = _build_telegram_message(self.client_id, f)
                ok, err = tg.send(text)
                is_primary_channel = f.channel not in ("", None)
                if ok:
                    if not is_primary_channel:
                        self.store.mark(f.event, f.ident)
                        f.delivered = True
                elif not is_primary_channel:
                    log.warning("event_webhooks.telegram_failed_api",
                               evt=f.event, contact=f.contact_email, error=err)

    def _discover_active_campaigns(self, page, csrf: str) -> dict:
        """Lista campanhas ATIVAS (active && !archived) → {id: name}."""
        found: dict = {}
        for pg in range(1, 12):
            res = api.post(page, "/emailer_campaigns/search", csrf, {
                "page": pg, "per_page": 100, "display_mode": "explorer_mode",
                "open_factor_names": [], "sort_by_field": "updated_at",
                "sort_ascending": False,
            })
            camps = (res.get("data") or {}).get("emailer_campaigns") or []
            for c in camps:
                if c.get("active") and not c.get("archived"):
                    found[c.get("id")] = c.get("name", "")
            if len(camps) < 100:
                break
        return found

    def _run_positive_replies(self, page, csrf, channel, target, run) -> None:
        from core.reply_classifier import categorize_reply

        try:
            replies = self._fetch_replies_generic(page, csrf, self._campaigns)
        except Exception as exc:
            log.error("event_webhooks.reply_fetch_error", error=str(exc))
            replies = []

        for reply in replies:
            cid = reply.get("contact_id", "")
            email = reply.get("email", "")
            if not cid:
                continue
            if is_own_domain(email, self._own_domains):
                continue  # auto-reply / encaminhamento interno — não é lead
            if categorize_reply(reply.get("body", "") or "") != "interested":
                continue

            campaign_id = reply.get("seq_id", "")
            seq_name = reply.get("seq", "")
            fire = WebhookFire(
                event=EVENT_POSITIVE_REPLY,
                contact_id=cid,
                contact_email=email,
                contact_name=reply.get("name", ""),
                campaign_id=campaign_id,
                campaign_name=seq_name,
                channel=channel, target=target,
                payload=build_reply_payload(self.client_id, campaign_id, seq_name, reply),
            )
            self._consider(fire, run)

    def _run_opened_multiple(self, page, csrf, op_cfg, channel, target, run) -> None:
        min_steps = int(op_cfg.get("min_distinct_steps", 2))
        require_tracked = bool(op_cfg.get("require_tracked", True))

        for seq_id, seq_name in self._campaigns.items():
            by_contact = self._fetch_opened_messages(page, csrf, seq_id)
            for cid, msgs in by_contact.items():
                summary = opened_event_from_messages(msgs, min_steps, require_tracked)
                if summary is None:
                    continue
                sample = msgs[0]
                email = sample.get("to_email", "")
                if is_own_domain(email, self._own_domains):
                    continue
                contact = {"id": cid, "name": sample.get("to_name", ""), "email": email}
                step_subjects: dict = {}
                for m in msgs:
                    pos = m.get("campaign_position")
                    try:
                        pos = int(pos) if pos is not None else None
                    except (TypeError, ValueError):
                        pos = None
                    if pos is not None and pos not in step_subjects:
                        step_subjects[pos] = (m.get("subject") or "").strip()
                fire = WebhookFire(
                    event=EVENT_OPENED_MULTIPLE,
                    contact_id=cid,
                    contact_email=email,
                    contact_name=contact["name"],
                    campaign_id=seq_id,
                    campaign_name=seq_name,
                    channel=channel, target=target,
                    payload=build_opened_payload(self.client_id, seq_id, seq_name, contact, summary,
                                                 step_subjects=step_subjects),
                )
                self._consider(fire, run)

    def _fetch_replies_generic(self, page, csrf: str, campaigns: dict) -> list[dict]:
        """Busca respostas (com corpo) das campanhas dadas, SEM depender de
        clients/<id>.json. {id:name} → lista de dicts com corpo para classificar.

        1) /emailer_messages/search (stat 'replied') por campanha → contact_ids.
        2) /activities?contact_id=… (downloaded_email) → corpo da resposta.
        """
        contacts: dict = {}  # cid -> {name,email,seq,seq_id}
        for seq_id, seq_name in campaigns.items():
            for pg in range(1, 11):
                res = api.post(page, "/emailer_messages/search", csrf, {
                    "emailer_message_stats": ["replied"],
                    "emailer_campaign_id": seq_id,
                    "per_page": 100, "page": pg, "finder_version": 1,
                    "context": "emailer-campaign-show-messages-page",
                })
                if res.get("status") != 200:
                    break
                msgs = (res.get("data") or {}).get("emailer_messages") or []
                for m in msgs:
                    cid = m.get("contact_id")
                    if cid and cid not in contacts:
                        contacts[cid] = {
                            "name": m.get("to_name", ""),
                            "email": m.get("to_email", ""),
                            "seq": seq_name, "seq_id": seq_id,
                        }
                if len(msgs) < 100:
                    break

        replies: list[dict] = []
        for cid, info in contacts.items():
            act = api.get(
                page,
                f"/activities?contact_id={cid}&events_grouping=chronologically&types[]=emailer_message",
                csrf,
            )
            if act.get("status") != 200:
                continue
            for a in (act.get("data") or {}).get("activities", []):
                m = a.get("emailer_message", {}) or {}
                if m.get("type") != "downloaded_email":
                    continue
                body = _best_body(m)
                date_str = m.get("completed_at") or m.get("created_at", "")
                replies.append({
                    "contact_id": cid,
                    "name": m.get("from_name", info["name"]),
                    "email": m.get("from_email", info["email"]),
                    "seq": info["seq"], "seq_id": info["seq_id"],
                    "subject": m.get("subject", ""),
                    "date": (date_str or "")[:16].replace("T", " "),
                    "body": body,
                })
        return replies

    # ---- dedup gate + entrega --------------------------------------------

    def _consider(self, fire: WebhookFire, run: ForwarderRun) -> bool:
        """Portão de dedup. Registra o fire em run.fires; retorna True se é novo
        (a entregar) ou False se já disparado antes."""
        ident = event_identity(fire.contact_email, fire.contact_id)
        fire.ident = ident
        key = (fire.event, ident)
        if key in self._seen or self.store.already_fired(fire.event, ident):
            fire.skipped_reason = "already_fired"
            run.fires.append(fire)
            return False
        self._seen.add(key)
        run.fires.append(fire)
        return True

    def _deliver_all(self, run: ForwarderRun, page=None, csrf: str = "") -> None:
        pending = [f for f in run.fires if f.skipped_reason is None and not f.delivered]

        if self.dry_run:
            for f in pending:
                f.skipped_reason = "dry_run"
                log.info("event_webhooks.would_fire", evt=f.event, ch=f.channel,
                         contact=f.contact_email, campaign=f.campaign_name)
            return

        # Enriquecimento de empresa/cargo (grátis, sempre — só leitura do
        # registro já existente do contato). Alimenta o payload (webhook/email)
        # E a mensagem do Telegram.
        if page is not None:
            for f in pending:
                meta = self._fetch_contact_meta(page, csrf, f.contact_id)
                if meta:
                    f.payload.setdefault("contact", {}).update(meta)

        # Enriquecimento de telefone (opt-in, só p/ quem vai ser entregue de
        # verdade — nunca gasta em quem já disparou antes ou está em dry-run).
        if self._enrich_phone and page is not None:
            for f in pending:
                try:
                    phone = self._fetch_contact_phone(page, csrf, f.contact_id)
                except Exception as exc:
                    log.warning("event_webhooks.phone_enrich_error",
                               contact_id=f.contact_id, error=str(exc))
                    phone = None
                if phone:
                    f.payload.setdefault("contact", {})["phone"] = phone["number"]
                    f.payload["contact"]["phone_type"] = phone["type"]

        # Webhooks: um POST por evento.
        for f in [x for x in pending if x.channel == "webhook"]:
            ok, status, err = self._post(f.target, f.payload)
            f.delivered, f.status_code, f.error = ok, status, err
            if ok:
                self.store.mark(f.event, f.ident)
                log.info("event_webhooks.fired", evt=f.event, contact=f.contact_email, status=status)
            else:
                log.error("event_webhooks.fire_failed", evt=f.event,
                          contact=f.contact_email, status=status, error=err)

        # Email: UM digest com todos os eventos novos (por destinatário).
        email_fires = [x for x in pending if x.channel == "email"]
        by_recipient: dict[str, list] = {}
        for f in email_fires:
            by_recipient.setdefault(f.target, []).append(f)
        for recipient, fires in by_recipient.items():
            ok, err = self._send_digest_email(recipient, fires)
            for f in fires:
                f.delivered, f.error = ok, err
                if ok:
                    self.store.mark(f.event, f.ident)

        # Telegram: mensagem individual em tempo real, ADICIONAL ao canal
        # primário (webhook/email) — não substitui.
        #
        # BUG corrigido em 18/08: quando o fire TEM canal primário (webhook/
        # email) e ele FALHA, mandar o Telegram com sucesso NÃO pode marcar
        # dedup nem virar f.delivered=True — isso escondia leads que nunca
        # chegaram no Leavo (ficavam "já disparados" pra sempre, sem retry).
        # Telegram só controla dedup/delivered para fires SEM canal primário
        # (reply_ping, channel=""), onde ele É o próprio canal de entrega.
        if self._telegram_enabled:
            for f in pending:
                text = _build_telegram_message(self.client_id, f)
                ok, err = tg.send(text)
                is_primary_channel = f.channel not in ("", None)
                if ok:
                    if not is_primary_channel:
                        self.store.mark(f.event, f.ident)
                        f.delivered = True
                elif not is_primary_channel:
                    log.warning("event_webhooks.telegram_failed",
                               evt=f.event, contact=f.contact_email, error=err)

    def _send_digest_email(self, recipient: str, fires: list[WebhookFire]) -> tuple[bool, Optional[str]]:
        """Envia UM email-resumo com todos os eventos novos. (ok, err)."""
        if not config.SMTP_HOST or not config.SMTP_USER:
            return False, "SMTP não configurado"
        subject, body = _build_digest(self.client_id, fires)
        try:
            import smtplib
            from email.mime.text import MIMEText
            msg = MIMEText(body, "plain", "utf-8")
            msg["Subject"] = subject
            msg["From"] = (f"{config.SENDER_NAME} <{config.SMTP_USER}>"
                           if config.SENDER_NAME else config.SMTP_USER)
            msg["To"] = recipient
            with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT) as server:
                server.starttls()
                server.login(config.SMTP_USER, config.SMTP_PASS)
                server.sendmail(config.SMTP_USER, [recipient], msg.as_string())
            log.info("event_webhooks.digest_sent", to=recipient, events=len(fires))
            return True, None
        except Exception as exc:
            log.error("event_webhooks.digest_failed", to=recipient, error=str(exc))
            return False, str(exc)


# ---------------------------------------------------------------------------
# Tracking helpers (ligar rastreamento de abertura)
# ---------------------------------------------------------------------------

def read_tracking_state(page, csrf: str) -> dict:
    """Lê o estado atual de open/click tracking do usuário logado."""
    prof = api.get(page, "/users/current", csrf)
    u = ((prof.get("data") or {}).get("user")) or (prof.get("data") or {})
    return {
        "user_id": u.get("id"),
        "enable_open_tracking": u.get("enable_open_tracking"),
        "enable_click_tracking": u.get("enable_click_tracking"),
    }


def enable_open_tracking(page, csrf: str, user_id: str) -> dict:
    """Tenta LIGAR o rastreamento de abertura (account-wide p/ o usuário).

    Tenta endpoints candidatos e VERIFICA relendo /users/current. Retorna
    {ok, before, after, endpoint_used}.
    """
    before = read_tracking_state(page, csrf)
    candidates = [
        ("PATCH", f"/users/{user_id}"),
        ("PATCH", "/users/current"),
        ("PUT", f"/users/{user_id}"),
    ]
    payload = {"enable_open_tracking": True}
    used = None
    for method, path in candidates:
        res = api.apollo_request(page, method, path, csrf, payload)
        after = read_tracking_state(page, csrf)
        if after.get("enable_open_tracking") is True:
            used = f"{method} {path}"
            return {"ok": True, "before": before, "after": after, "endpoint_used": used,
                    "last_status": res.get("status")}
    after = read_tracking_state(page, csrf)
    return {"ok": after.get("enable_open_tracking") is True,
            "before": before, "after": after, "endpoint_used": used}
