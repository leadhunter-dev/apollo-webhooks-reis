"""Unit tests for event_webhooks (sem browser/rede)."""
from __future__ import annotations

import json

from core import event_webhooks as ew
from core.event_webhooks import (
    EVENT_OPENED_MULTIPLE,
    EVENT_POSITIVE_REPLY,
    EventWebhookForwarder,
    FiredStore,
    _clean_meta,
    build_opened_payload,
    build_reply_payload,
    event_identity,
    is_own_domain,
    opened_event_from_messages,
    select_best_phone,
)


# --- identity / own-domain helpers ----------------------------------------

def test_event_identity_prefers_email():
    assert event_identity("A@X.com", "c1") == "a@x.com"
    assert event_identity("", "c1") == "c1"
    assert event_identity(None, "c1") == "c1"


def test_is_own_domain_matches_domain_and_subdomain():
    od = {"reisoffice.com.br"}
    assert is_own_domain("conexao@reisoffice.com.br", od) is True
    assert is_own_domain("x@mail.reisoffice.com.br", od) is True
    assert is_own_domain("lead@engemont.net.br", od) is False
    assert is_own_domain("", od) is False


# --- opened_event_from_messages -------------------------------------------

def _om(pos, tracked=True):
    return {"campaign_position": pos, "open_tracking_enabled": tracked}


def test_opened_fires_on_two_distinct_steps():
    msgs = [_om(1), _om(3)]
    summ = opened_event_from_messages(msgs, min_distinct_steps=2)
    assert summ is not None
    assert summ["num_distinct_steps"] == 2
    assert summ["distinct_steps"] == [1, 3]


def test_opened_does_not_fire_single_step():
    # abriu só o step 1 (uma posição) → não dispara
    assert opened_event_from_messages([_om(1)], min_distinct_steps=2) is None


def test_opened_same_step_twice_is_one_position():
    # mesma posição em duas mensagens não conta como 2 steps distintos
    msgs = [_om(1), _om(1)]
    assert opened_event_from_messages(msgs, min_distinct_steps=2) is None


def test_opened_ignores_untracked_phantom_opens():
    # aberturas fantasma (tracking off) são descartadas por require_tracked
    msgs = [_om(1, tracked=False), _om(2, tracked=False)]
    assert opened_event_from_messages(msgs, min_distinct_steps=2) is None


def test_opened_handles_missing_position():
    msgs = [_om(3), {"open_tracking_enabled": True, "campaign_position": None}]
    assert opened_event_from_messages(msgs, min_distinct_steps=2) is None


def test_opened_string_positions_are_coerced():
    # Apollo às vezes manda campaign_position como string
    msgs = [_om("1"), _om("2")]
    summ = opened_event_from_messages(msgs, min_distinct_steps=2)
    assert summ is not None and summ["num_distinct_steps"] == 2


# --- payloads --------------------------------------------------------------

def test_build_reply_payload_truncates_and_shapes():
    reply = {
        "contact_id": "c1", "name": "Fulano", "email": "f@x.com",
        "subject": "Re: proposta", "date": "23/07/2026 10:00",
        "body": "x" * 999,
    }
    p = build_reply_payload("reis_office", "camp1", "[REIS] X", reply)
    assert p["event"] == EVENT_POSITIVE_REPLY
    assert p["contact"]["email"] == "f@x.com"
    assert len(p["reply"]["snippet"]) == 500
    assert p["campaign_id"] == "camp1"


def test_build_opened_payload_shape():
    contact = {"id": "c2", "name": "Beltrano", "email": "b@y.com"}
    summ = {"distinct_steps": [1, 3], "num_distinct_steps": 2}
    p = build_opened_payload("reis_office", "camp2", "[REIS] Y", contact, summ)
    assert p["event"] == EVENT_OPENED_MULTIPLE
    assert p["opens"]["num_distinct_steps"] == 2
    assert p["contact"]["id"] == "c2"


# --- select_best_phone (nunca vaza telefone da empresa) --------------------

def test_select_best_phone_prefers_mobile():
    nums = [
        {"type": "work_hq", "raw_number": "+55 11 3526-4455", "sanitized_number": "+551135264455"},
        {"type": "mobile", "raw_number": "+55 35 99105-5343", "sanitized_number": "+5535991055343",
         "status": "valid_number"},
    ]
    phone = select_best_phone(nums)
    assert phone == {"number": "+55 35 99105-5343", "type": "mobile", "status": "valid_number"}


def test_select_best_phone_never_returns_company_hq():
    nums = [{"type": "work_hq", "raw_number": "+55 11 3526-4455", "sanitized_number": "+551135264455"}]
    assert select_best_phone(nums) is None


def test_select_best_phone_empty_or_missing():
    assert select_best_phone([]) is None
    assert select_best_phone(None) is None


def test_build_payloads_include_phone_when_given():
    phone = {"number": "+55 35 99105-5343", "type": "mobile"}
    reply = {"contact_id": "c1", "name": "K", "email": "k@x.com", "subject": "s",
             "date": "d", "body": "oi"}
    p1 = build_reply_payload("reis_office", "camp1", "[REIS] X", reply, phone=phone)
    assert p1["contact"]["phone"] == "+55 35 99105-5343"
    assert p1["contact"]["phone_type"] == "mobile"

    contact = {"id": "c2", "name": "K", "email": "k@x.com"}
    summ = {"distinct_steps": [1, 2], "num_distinct_steps": 2}
    p2 = build_opened_payload("reis_office", "camp2", "[REIS] Y", contact, summ, phone=phone)
    assert p2["contact"]["phone"] == "+55 35 99105-5343"


def test_build_payloads_omit_phone_when_none():
    reply = {"contact_id": "c1", "name": "K", "email": "k@x.com", "subject": "s",
             "date": "d", "body": "oi"}
    p = build_reply_payload("reis_office", "camp1", "[REIS] X", reply)
    assert "phone" not in p["contact"]


# --- FiredStore ------------------------------------------------------------

def test_fired_store_dedup_roundtrip(tmp_path):
    store = FiredStore("reis_office", base_dir=tmp_path)
    assert not store.already_fired(EVENT_POSITIVE_REPLY, "c1")
    store.mark(EVENT_POSITIVE_REPLY, "c1")
    store.save()

    reloaded = FiredStore("reis_office", base_dir=tmp_path)
    assert reloaded.already_fired(EVENT_POSITIVE_REPLY, "c1")
    assert not reloaded.already_fired(EVENT_OPENED_MULTIPLE, "c1")


# --- Forwarder dispatch (fake http + fake page) ---------------------------

class _FakeResp:
    def __init__(self, status_code=200, text="ok"):
        self.status_code = status_code
        self.text = text


class _FakeHttp:
    def __init__(self, status_code=200):
        self.calls = []
        self._status = status_code

    def post(self, url, json=None, timeout=None):
        self.calls.append({"url": url, "json": json})
        return _FakeResp(self._status)


def _cfg(own_domains=None, notify_email=None, reply_url="https://leavo/reply"):
    rep = {"enabled": True}
    if reply_url:
        rep["url"] = reply_url
    cfg = {
        "client": "reis_office",
        "own_domains": own_domains if own_domains is not None else [],
        "positive_reply": rep,
        "opened_multiple": {"enabled": False, "url": "https://leavo/open"},
        "campaigns": {"seq1": "[REIS] X"},
    }
    if notify_email:
        cfg["notify_email"] = notify_email
    return cfg


def _fake_categorize_reply(text):
    return "interested" if "quero" in (text or "").lower() else "unknown"


def _wire(monkeypatch, fwd, replies):
    """Injeta replies e evita browser/rede real: patch capture_csrf +
    categorize_reply + _fetch_replies_generic + Telegram (que está configurado
    de VERDADE no .env deste projeto — sem isso, os testes mandam mensagem
    real e ficam lentos esperando o timeout de rede do sandbox)."""
    monkeypatch.setattr("core.event_webhooks.api.capture_csrf", lambda *a, **k: "csrf-test")
    monkeypatch.setattr("core.reply_classifier.categorize_reply", _fake_categorize_reply)
    monkeypatch.setattr("core.event_webhooks.tg.send", lambda *a, **k: (True, None))
    fwd._telegram_enabled = False  # desliga por padrão nos testes; ligar explicitamente quem precisar testar isso
    fwd._fetch_replies_generic = lambda page, csrf, campaigns: replies


def test_forwarder_fires_positive_reply_once(tmp_path, monkeypatch):
    replies = [
        {"contact_id": "c1", "name": "A", "email": "a@x.com", "seq": "[REIS] X",
         "seq_id": "seq1", "subject": "s", "date": "d", "body": "Quero saber mais"},
        {"contact_id": "c1", "name": "A", "email": "a@x.com", "seq": "[REIS] X",
         "seq_id": "seq1", "subject": "s2", "date": "d2", "body": "Quero saber mais"},  # dup
        {"contact_id": "c2", "name": "B", "email": "b@x.com", "seq": "[REIS] X",
         "seq_id": "seq1", "subject": "s", "date": "d", "body": "não tenho interesse"},  # neg
    ]
    http = _FakeHttp(200)
    fwd = EventWebhookForwarder("reis_office", cfg=_cfg(),
                                store=FiredStore("reis_office", base_dir=tmp_path), http=http)
    _wire(monkeypatch, fwd, replies)
    run = fwd.run(page=None, context=None)

    delivered = [f for f in run.fires if f.delivered]
    assert len(delivered) == 1
    assert delivered[0].contact_id == "c1"
    assert len(http.calls) == 1
    assert http.calls[0]["url"] == "https://leavo/reply"

    # Segunda execução: dedup impede novo disparo.
    http2 = _FakeHttp(200)
    fwd2 = EventWebhookForwarder("reis_office", cfg=_cfg(),
                                 store=FiredStore("reis_office", base_dir=tmp_path), http=http2)
    _wire(monkeypatch, fwd2, replies)
    fwd2.run(page=None, context=None)
    assert len(http2.calls) == 0


def test_forwarder_skips_own_domain_and_dedups_by_email(tmp_path, monkeypatch):
    replies = [
        {"contact_id": "own1", "name": "Interno", "email": "conexao@reisoffice.com.br",
         "seq": "[REIS] X", "seq_id": "seq1", "subject": "s", "date": "d", "body": "Quero saber mais"},
        {"contact_id": "c1", "name": "Lead", "email": "lead@engemont.net.br",
         "seq": "[REIS] X", "seq_id": "seq1", "subject": "s", "date": "d", "body": "Quero saber mais"},
        {"contact_id": "c2", "name": "Lead", "email": "Lead@Engemont.net.br",
         "seq": "[REIS] X", "seq_id": "seq1", "subject": "s2", "date": "d2", "body": "Quero saber mais"},
    ]
    http = _FakeHttp(200)
    fwd = EventWebhookForwarder("reis_office", cfg=_cfg(own_domains=["reisoffice.com.br"]),
                                store=FiredStore("reis_office", base_dir=tmp_path), http=http)
    _wire(monkeypatch, fwd, replies)
    run = fwd.run(page=None, context=None)
    delivered = [f for f in run.fires if f.delivered]
    assert len(delivered) == 1
    assert delivered[0].contact_email == "lead@engemont.net.br"
    assert len(http.calls) == 1


def test_forwarder_dry_run_does_not_post_or_persist(tmp_path, monkeypatch):
    replies = [{"contact_id": "c1", "name": "A", "email": "a@x.com", "seq": "[REIS] X",
                "seq_id": "seq1", "subject": "s", "date": "d", "body": "Quero saber mais"}]
    http = _FakeHttp(200)
    fwd = EventWebhookForwarder("reis_office", cfg=_cfg(),
                                store=FiredStore("reis_office", base_dir=tmp_path),
                                http=http, dry_run=True)
    _wire(monkeypatch, fwd, replies)
    run = fwd.run(page=None, context=None)

    assert len(http.calls) == 0
    assert any(f.skipped_reason == "dry_run" for f in run.fires)
    assert not FiredStore("reis_office", base_dir=tmp_path).already_fired(EVENT_POSITIVE_REPLY, "a@x.com")


def test_enrich_phone_adds_to_payload_when_enabled(tmp_path, monkeypatch):
    replies = [{"contact_id": "c1", "name": "K", "email": "k@delfim.com.br", "seq": "[REIS] X",
                "seq_id": "seq1", "subject": "s", "date": "d", "body": "Quero saber mais"}]
    cfg = _cfg()
    cfg["enrich_phone"] = True
    http = _FakeHttp(200)
    fwd = EventWebhookForwarder("reis_office", cfg=cfg,
                                store=FiredStore("reis_office", base_dir=tmp_path), http=http)
    _wire(monkeypatch, fwd, replies)
    fwd._fetch_contact_phone = lambda page, csrf, cid: (
        {"number": "+55 35 99105-5343", "type": "mobile", "status": "valid_number"}
        if cid == "c1" else None
    )
    run = fwd.run(page="fake-page", context=None)

    assert http.calls[0]["json"]["contact"]["phone"] == "+55 35 99105-5343"
    assert run.fires[0].payload["contact"]["phone_type"] == "mobile"


def test_enrich_phone_disabled_by_default(tmp_path, monkeypatch):
    replies = [{"contact_id": "c1", "name": "K", "email": "k@delfim.com.br", "seq": "[REIS] X",
                "seq_id": "seq1", "subject": "s", "date": "d", "body": "Quero saber mais"}]
    http = _FakeHttp(200)
    fwd = EventWebhookForwarder("reis_office", cfg=_cfg(),  # sem enrich_phone
                                store=FiredStore("reis_office", base_dir=tmp_path), http=http)
    _wire(monkeypatch, fwd, replies)
    called = []
    fwd._fetch_contact_phone = lambda page, csrf, cid: called.append(cid) or None
    fwd.run(page="fake-page", context=None)

    assert called == []  # nunca chamado — enrich_phone é opt-in
    assert "phone" not in http.calls[0]["json"]["contact"]


def test_enrich_phone_never_blocks_delivery_on_error(tmp_path, monkeypatch):
    replies = [{"contact_id": "c1", "name": "K", "email": "k@delfim.com.br", "seq": "[REIS] X",
                "seq_id": "seq1", "subject": "s", "date": "d", "body": "Quero saber mais"}]
    cfg = _cfg()
    cfg["enrich_phone"] = True
    http = _FakeHttp(200)
    fwd = EventWebhookForwarder("reis_office", cfg=cfg,
                                store=FiredStore("reis_office", base_dir=tmp_path), http=http)
    _wire(monkeypatch, fwd, replies)

    def _boom(page, csrf, cid):
        raise RuntimeError("navegação falhou")
    fwd._fetch_contact_phone = _boom
    run = fwd.run(page="fake-page", context=None)

    # o evento principal dispara normalmente mesmo com o enriquecimento quebrando
    assert len(http.calls) == 1
    assert run.fires[0].delivered is True


# --- _clean_meta (nome/sobrenome/empresa/cargo) -----------------------------

def test_clean_meta_extracts_known_fields():
    contact = {"first_name": "Kleber", "last_name": "Pinesso",
              "organization_name": "Delfim Têxtil", "title": "Operations Manager"}
    assert _clean_meta(contact) == {
        "first_name": "Kleber", "last_name": "Pinesso",
        "company": "Delfim Têxtil", "title": "Operations Manager",
    }


def test_clean_meta_omits_empty_fields():
    # sem sobrenome/empresa preenchidos — não manda string vazia (sobrescreveria
    # um campo já preenchido no CRM de destino)
    contact = {"first_name": "Kleber", "last_name": "", "organization_name": None}
    assert _clean_meta(contact) == {"first_name": "Kleber"}


def test_clean_meta_empty_contact():
    assert _clean_meta({}) == {}


def test_meta_enrichment_adds_company_and_names_to_payload(tmp_path, monkeypatch):
    replies = [{"contact_id": "c1", "name": "Kleber Pinesso", "email": "k@delfim.com.br",
                "seq": "[REIS] X", "seq_id": "seq1", "subject": "s", "date": "d",
                "body": "Quero saber mais"}]
    http = _FakeHttp(200)
    fwd = EventWebhookForwarder("reis_office", cfg=_cfg(),
                                store=FiredStore("reis_office", base_dir=tmp_path), http=http)
    _wire(monkeypatch, fwd, replies)
    fwd._fetch_contact_meta = lambda page, csrf, cid: (
        {"first_name": "Kleber", "last_name": "Pinesso", "company": "Delfim Têxtil",
         "title": "Operations Manager"} if cid == "c1" else {}
    )
    run = fwd.run(page="fake-page", context=None)

    contact = http.calls[0]["json"]["contact"]
    assert contact["first_name"] == "Kleber"
    assert contact["last_name"] == "Pinesso"
    assert contact["company"] == "Delfim Têxtil"
    assert contact["title"] == "Operations Manager"
    assert run.fires[0].delivered is True


def test_meta_enrichment_runs_even_without_enrich_phone_flag(tmp_path, monkeypatch):
    # empresa/cargo/nome são SEMPRE enriquecidos (não é opt-in como telefone)
    replies = [{"contact_id": "c1", "name": "K", "email": "k@x.com", "seq": "[REIS] X",
                "seq_id": "seq1", "subject": "s", "date": "d", "body": "Quero saber mais"}]
    cfg = _cfg()
    assert "enrich_phone" not in cfg
    http = _FakeHttp(200)
    fwd = EventWebhookForwarder("reis_office", cfg=cfg,
                                store=FiredStore("reis_office", base_dir=tmp_path), http=http)
    _wire(monkeypatch, fwd, replies)
    fwd._fetch_contact_meta = lambda page, csrf, cid: {"company": "Acme"}
    fwd.run(page="fake-page", context=None)

    assert http.calls[0]["json"]["contact"]["company"] == "Acme"


def test_meta_enrichment_skipped_without_page(tmp_path, monkeypatch):
    # page=None (ex.: dry-run local) — nunca tenta buscar meta
    replies = [{"contact_id": "c1", "name": "K", "email": "k@x.com", "seq": "[REIS] X",
                "seq_id": "seq1", "subject": "s", "date": "d", "body": "Quero saber mais"}]
    http = _FakeHttp(200)
    fwd = EventWebhookForwarder("reis_office", cfg=_cfg(),
                                store=FiredStore("reis_office", base_dir=tmp_path), http=http)
    _wire(monkeypatch, fwd, replies)
    called = []
    fwd._fetch_contact_meta = lambda page, csrf, cid: called.append(cid) or {}
    fwd.run(page=None, context=None)

    assert called == []
    assert "company" not in http.calls[0]["json"]["contact"]


# --- regressão: Telegram não pode mascarar falha do canal primário --------

def test_telegram_success_does_not_mask_primary_webhook_failure(tmp_path, monkeypatch):
    """Bug real (18/08): webhook pro Leavo falhou (400, telefone obrigatório
    faltando) mas o aviso adicional do Telegram teve sucesso, e o código
    antigo marcava o lead como 'já disparado' mesmo assim — perdendo-o pra
    sempre, sem chance de retry mesmo depois de corrigir o telefone."""
    replies = [{"contact_id": "c1", "name": "K", "email": "k@x.com", "seq": "[REIS] X",
                "seq_id": "seq1", "subject": "s", "date": "d", "body": "Quero saber mais"}]
    http = _FakeHttp(400)  # webhook do Leavo falha
    fwd = EventWebhookForwarder("reis_office", cfg=_cfg(),
                                store=FiredStore("reis_office", base_dir=tmp_path), http=http)
    _wire(monkeypatch, fwd, replies)
    fwd._telegram_enabled = True
    monkeypatch.setattr("core.event_webhooks.tg.send", lambda *a, **k: (True, None))  # telegram OK

    run = fwd.run(page=None, context=None)

    fire = run.fires[0]
    assert fire.channel == "webhook"
    assert fire.delivered is False  # NÃO pode virar True só por causa do telegram
    assert fire.status_code == 400
    # dedup não pode estar marcado — precisa poder retentar
    assert not FiredStore("reis_office", base_dir=tmp_path).already_fired(EVENT_POSITIVE_REPLY, "k@x.com")


def test_telegram_still_controls_dedup_for_reply_ping_without_primary_channel(tmp_path, monkeypatch):
    """reply_ping não tem canal primário (channel="") — aí sim o Telegram É
    o canal de entrega, e deve controlar dedup normalmente."""
    fwd = EventWebhookForwarder("reis_office", cfg=_cfg(),
                                store=FiredStore("reis_office", base_dir=tmp_path), http=_FakeHttp(200))
    fwd._telegram_enabled = True
    monkeypatch.setattr("core.event_webhooks.tg.send", lambda *a, **k: (True, None))

    run = ew.ForwarderRun(client_id="reis_office")
    fire = ew.WebhookFire(event=EVENT_POSITIVE_REPLY, contact_id="c9", contact_email="ping@x.com",
                          contact_name="P", campaign_id="s1", campaign_name="X",
                          channel="", target="", payload={"contact": {}})
    fwd._consider(fire, run)
    fwd._deliver_all(run, page=None, csrf="")

    assert fire.delivered is True
    assert fwd.store.already_fired(EVENT_POSITIVE_REPLY, "ping@x.com")


def test_email_channel_sends_one_digest_and_dedups(tmp_path, monkeypatch):
    replies = [
        {"contact_id": "c1", "name": "Lead1", "email": "l1@acme.com", "seq": "[LH] X",
         "seq_id": "seq1", "subject": "s", "date": "d", "body": "Quero saber mais"},
        {"contact_id": "c2", "name": "Lead2", "email": "l2@acme.com", "seq": "[LH] X",
         "seq_id": "seq1", "subject": "s", "date": "d", "body": "quero agendar"},
    ]
    # sem url → canal email
    cfg = _cfg(notify_email="jose@leadhunter.com.br", reply_url=None)
    fwd = EventWebhookForwarder("ativo_advisory", cfg=cfg,
                                store=FiredStore("ativo_advisory", base_dir=tmp_path),
                                http=_FakeHttp(200))
    _wire(monkeypatch, fwd, replies)

    sent = []
    monkeypatch.setattr(fwd, "_send_digest_email",
                        lambda recipient, fires: (sent.append((recipient, list(fires))) or (True, None)))
    run = fwd.run(page=None, context=None)

    # UM email para o destinatário com os 2 leads
    assert len(sent) == 1
    recipient, fires = sent[0]
    assert recipient == "jose@leadhunter.com.br"
    assert len(fires) == 2
    assert all(f.delivered for f in run.fires)

    # dedup persistido → segunda rodada não reenvia
    fwd2 = EventWebhookForwarder("ativo_advisory", cfg=cfg,
                                 store=FiredStore("ativo_advisory", base_dir=tmp_path),
                                 http=_FakeHttp(200))
    _wire(monkeypatch, fwd2, replies)
    sent2 = []
    monkeypatch.setattr(fwd2, "_send_digest_email",
                        lambda r, f: (sent2.append(r) or (True, None)))
    fwd2.run(page=None, context=None)
    assert sent2 == []


def test_digest_builder_groups_events():
    from core.event_webhooks import _build_digest, WebhookFire
    fires = [
        WebhookFire(event=EVENT_POSITIVE_REPLY, contact_id="c1", contact_email="a@x.com",
                    contact_name="Ana", campaign_id="s1", campaign_name="[LH] X",
                    payload={"reply": {"snippet": "quero muito"}}),
        WebhookFire(event=EVENT_OPENED_MULTIPLE, contact_id="c2", contact_email="b@x.com",
                    contact_name="Bia", campaign_id="s1", campaign_name="[LH] X",
                    payload={"opens": {"distinct_steps": [1, 2], "num_distinct_steps": 2}}),
    ]
    subject, body = _build_digest("ativo_advisory", fires)
    assert "2 novo(s) evento(s)" in subject
    assert "Respostas positivas (1)" in body
    assert "Abriu 2+ steps (1)" in body
    assert "Ana" in body and "Bia" in body
