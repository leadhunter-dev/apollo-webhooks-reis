"""Classificação de texto de resposta (PT+EN), extraída de ResponseHandler do
projeto Apollo Ops. Função pura, sem dependência de banco — só o necessário
pra decidir se uma resposta é `interested` (vira o evento `positive_reply`).
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# Keyword tables (PT + EN) — order inside each list does not affect priority
# ---------------------------------------------------------------------------

RESPONSE_KEYWORDS: dict[str, list[str]] = {
    "removal_requested": [
        "me remova", "remova meu", "não me contate", "pare de me enviar",
        "remove me", "unsubscribe", "opt out", "opt-out", "stop emailing",
        "stop contacting", "take me off", "please remove", "do not contact",
        "remove from", "desinscrever",
    ],
    "interested": [
        "tenho interesse", "quero saber mais", "pode me ligar", "vamos marcar",
        "me manda mais", "me conta mais", "vamos conversar", "quero agendar",
        "qual produto", "quais produto", "qual modelo", "qual equipamento",
        "qual seria o orçamento", "qual orçamento", "qual o preço", "qual o valor",
        "me passe", "podemos conversar", "pode me passar", "tem interesse",
        "interessante", "interessada", "interessado",
        "interested", "let's connect", "tell me more", "sounds good",
        "let's talk", "i'd like to learn", "send me more", "please send",
        "yes", "sure, let's", "absolutely", "definitely interested",
        "descreve melhor", "conta mais", "o que faz", "o que fazem",
        "qual a solução", "como funciona",
    ],
    "not_interested": [
        "não tenho interesse", "não preciso", "não é o momento", "sem interesse",
        "não nos encaixa", "não se aplica", "não é para nós",
        "not interested", "no thanks", "no thank you", "not now",
        "not relevant", "not a fit", "don't need", "doesn't apply",
        "pass on this", "no need", "already have", "not looking",
    ],
    "ooo": [
        "out of office", "fora do escritório", "de férias", "férias",
        "estarei ausente", "em viagem", "retorno em", "estou ausente",
        "on vacation", "on leave", "annual leave", "will be back",
        "currently away", "out until", "back on", "back in the office",
        "auto reply", "automatic reply", "autoreply",
    ],
    "referral": [
        "fale com", "falar com", "contate", "contatar", "entre em contato com",
        "responsável é", "responsavel e", "quem cuida", "quem decide",
        "speak to", "contact ", "reach out to", "talk to",
        "favor dirigir", "dirija", "encaminhar para", "encaminhei para",
        "ccing", "cc:", "favor falar com", "minha colega", "meu colega",
        "minha gerente", "meu gerente", "minha diretora", "meu diretor",
    ],
}

# Priority order — first match wins
CATEGORY_PRIORITY = ["removal_requested", "referral", "interested", "not_interested", "ooo"]


def categorize_reply(text: str) -> str:
    """Categoriza o texto de uma resposta por keyword matching.

    Prioridade: removal_requested > referral > interested > not_interested > ooo.
    Case-insensitive. Retorna a categoria batida ou "unknown".

    Tratamento especial: um match de `interested` que só existe porque é
    substring de um keyword de `not_interested` também batido não conta como
    sinal genuíno de interesse (ex.: "yes" dentro de "not interested... yes?").
    """
    lower = text.lower()

    matched: set[str] = set()
    for category in CATEGORY_PRIORITY:
        for kw in RESPONSE_KEYWORDS[category]:
            if kw in lower:
                matched.add(category)
                break

    if "interested" in matched and "not_interested" in matched:
        ni_kws_in_text = [kw for kw in RESPONSE_KEYWORDS["not_interested"] if kw in lower]
        genuinely_interested = any(
            kw in lower and not any(kw in ni_kw for ni_kw in ni_kws_in_text)
            for kw in RESPONSE_KEYWORDS["interested"]
        )
        if not genuinely_interested:
            matched.discard("interested")

    for category in CATEGORY_PRIORITY:
        if category in matched:
            return category
    return "unknown"
