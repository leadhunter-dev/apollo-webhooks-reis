import os
from dotenv import load_dotenv

load_dotenv()

# Database
APOLLO_DB_PATH: str = os.getenv("APOLLO_DB_PATH", "./apollo_ops.db")

# Directories
SESSIONS_DIR: str = os.getenv("SESSIONS_DIR", "./.sessions")
DATA_DIR: str = os.getenv("DATA_DIR", "./data")

# SMTP
SMTP_HOST: str = os.getenv("SMTP_HOST", "")
SMTP_PORT: int = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER: str = os.getenv("SMTP_USER", "")
SMTP_PASS: str = os.getenv("SMTP_PASS", "")
SENDER_NAME: str = os.getenv("SENDER_NAME", "")

# Alerts
ALERT_WEBHOOK_URL: str = os.getenv("ALERT_WEBHOOK_URL", "")
ALERT_EMAIL_TO: str = os.getenv("ALERT_EMAIL_TO", "")

# Telegram
TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "")

# Apollo.io API pública — chave por conta (account_id -> api_key). Permite ler
# relatório/aberturas/respostas sem sessão de browser. Ver core/apollo_api.py.
APOLLO_API_KEYS: dict = {
    "ativo_advisory": os.getenv("APOLLO_API_KEY_ATIVO_ADVISORY", ""),
    "reis_office": os.getenv("APOLLO_API_KEY_REIS_OFFICE", ""),
}


def apollo_api_key(account: str) -> str:
    return APOLLO_API_KEYS.get(account, "") or os.getenv(f"APOLLO_API_KEY_{account.upper()}", "")

# Apollo.io
APOLLO_IO_URL: str = os.getenv("APOLLO_IO_URL", "https://app.apollo.io/")


def chrome_launch_args() -> list:
    """Args do Chrome para automação Apollo.

    Por padrão NÃO rouba foco: janela fora da tela principal. O login manual
  ainda funciona — use APOLLO_BRING_TO_FRONT=1 se quiser a janela na frente.
    """
    args = ["--disable-blink-features=AutomationControlled"]
    if _env_truthy("APOLLO_BRING_TO_FRONT"):
        return args
    pos = os.getenv("APOLLO_WINDOW_POSITION", "-2400,80")
    size = os.getenv("APOLLO_WINDOW_SIZE", "1100,750")
    args.append(f"--window-position={pos}")
    args.append(f"--window-size={size}")
    return args


def should_bring_browser_to_front() -> bool:
    """True só quando o operador pediu explicitamente (login manual)."""
    return _env_truthy("APOLLO_BRING_TO_FRONT")


def _env_truthy(name: str) -> bool:
    return os.getenv(name, "").lower() in ("1", "true", "yes")

# Ports
DASHBOARD_PORT: int = int(os.getenv("DASHBOARD_PORT", "8080"))
BRIDGE_PORT: int = int(os.getenv("BRIDGE_PORT", "9000"))

# Clients
import json as _json
from pathlib import Path as _Path

CLIENTS_DIR: str = os.getenv("CLIENTS_DIR", "./clients")


def get_client_apollo_config(client_id: str) -> dict:
    """Load per-client Apollo config from clients/{client_id}.json."""
    path = _Path(CLIENTS_DIR) / f"{client_id}.json"
    if not path.exists():
        raise FileNotFoundError(f"Apollo client config not found: {path}")
    with open(path, "r", encoding="utf-8") as f:
        return _json.load(f)
