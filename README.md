# Apollo Webhooks — Reis Office

Motor de webhooks que detecta eventos de campanha no Apollo.io (abriu 2x,
respondeu positivamente) e encaminha pra Leavo, o CRM do cliente. Extraído do
projeto Apollo Ops, contendo só o necessário pra esse fluxo específico.

## O que faz

- Descobre campanhas ATIVAS da conta `reis_office` automaticamente
  (`discover_active: true` em `clients/reis_office_webhooks.json`).
- Detecta dois eventos:
  - **`opened_multiple`** ("abriu 2x") — contato abriu ≥ 2 steps distintos de
    uma sequence. Requer rastreamento de abertura ligado no Apollo.
  - **`positive_reply`** — contato respondeu e o texto classifica como
    `interested` (keyword matching PT+EN, ver `core/reply_classifier.py`).
- Enriquece o payload com nome, sobrenome, empresa, cargo e telefone (pessoal
  — nunca o da recepção da empresa) do lead, quando disponível.
- Envia por `POST` pro webhook da Leavo configurado por evento.
- Deduplica: cada (contato, evento) dispara **uma única vez**, mesmo rodando
  o script várias vezes. Estado em `data/ops/webhook_fired/reis_office.json`.

## Dois caminhos pra ler o Apollo

| | API pública (API key) | Sessão de browser (login) |
|---|---|---|
| Precisa de login? | Não | Sim (manual, salvo em perfil persistente) |
| Detecta "abriu 2x"? | Sim, completo | Sim |
| Classifica resposta como positiva de verdade? | Não — só um aviso genérico de "alguém respondeu" (evento `reply_ping`, sem texto) | Sim — lê o corpo da resposta |
| Revela telefone **novo** de um lead? | Não — só devolve se já tiver sido revelado antes | Sim — abre a ficha do contato, dispara o enriquecimento lazy do próprio Apollo (~8 créditos da conta) |

`run_webhooks.py run` usa a API pública automaticamente se
`APOLLO_API_KEY_REIS_OFFICE` estiver configurada no `.env`; caso contrário,
cai pro caminho de browser.

> **Importante:** a Leavo hoje exige telefone como campo obrigatório no
> webhook de `opened_multiple`. Sem telefone pessoal já revelado no Apollo (ou
> revelado via browser nesta mesma execução), o envio falha com
> `400: "phone is required"`. Isso é uma limitação de dados real, não um bug —
> nem todo lead tem telefone pessoal disponível no Apollo.

## Instalação

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium

cp .env.example .env
# edite .env com a API key da reis_office (Settings → Integrations → API,
# logado na conta certa) e, opcionalmente, credenciais do Telegram
```

## Uso

```bash
# Ver o que dispararia, sem POST nem gravar dedup (rode isso antes sempre)
python3 run_webhooks.py run --dry-run

# Disparar de verdade
python3 run_webhooks.py run

# Ler se o rastreamento de abertura está ligado (necessário p/ "abriu 2x")
python3 run_webhooks.py check-tracking

# Ligar o rastreamento, se estiver desligado
python3 run_webhooks.py enable-tracking
```

O primeiro comando que precisar de browser (`check-tracking`,
`enable-tracking`, ou `run` sem API key configurada) abre uma janela do
Chrome pedindo login manual no Apollo — você tem 5 minutos. Depois disso a
sessão fica salva em `.sessions/reis_office_chrome_profile/` e não pede login
de novo até expirar. Use `APOLLO_BRING_TO_FRONT=1` no ambiente se a janela
abrir em segundo plano e você não conseguir vê-la.

## Rodando em produção (recorrente)

Este pacote não inclui um agendador — é intencional, pra manter o escopo
enxuto. `run_webhooks.py run` é idempotente (seguro rodar de novo, não
duplica envio), então qualquer agendador externo serve: cron, um LaunchAgent
do macOS, um workflow de CI agendado, etc. Recomendação: a cada 10–15 minutos.

## Testes

```bash
pytest tests/ -v
```

Os testes não fazem chamada de rede real — `_wire()` em
`tests/unit/test_event_webhooks.py` troca `capture_csrf`, `categorize_reply`
e o envio de Telegram por dublês.

## Estrutura

```
core/
  event_webhooks.py    # motor — detecção, dedup, montagem de payload, envio
  reply_classifier.py  # categoriza texto de resposta (PT+EN), função pura
  apollo_api.py         # cliente da API pública do Apollo (API key)
  apollo_fetch.py        # cliente da API interna via sessão de browser
  session_manager.py    # login/persistência de sessão do Playwright
  telegram.py            # aviso opcional em tempo real (nunca fonte de verdade)
  config.py               # variáveis de ambiente e config por cliente
clients/
  reis_office.json           # identidade da conta Apollo (own_domains, etc.)
  reis_office_webhooks.json  # URLs de webhook da Leavo + flags de enriquecimento
data/ops/webhook_fired/      # store de deduplicação (gerado em runtime)
run_webhooks.py               # CLI
```

## Configuração (`clients/reis_office_webhooks.json`)

```json
{
  "discover_active": true,
  "enrich_phone": true,
  "telegram_enabled": true,
  "positive_reply": { "enabled": true, "url": "https://api.leavo.ai/..." },
  "opened_multiple": {
    "enabled": true,
    "url": "https://api.leavo.ai/...",
    "min_distinct_steps": 2,
    "require_tracked": true
  }
}
```

- `enrich_phone: true` — antes de disparar `opened_multiple`, tenta enriquecer
  telefone (só no caminho de browser; consome crédito da conta se revelar um
  número novo).
- `require_tracked: true` — só considera aberturas com rastreamento ligado
  (evita falso-negativo silencioso quando o tracking está desligado).
