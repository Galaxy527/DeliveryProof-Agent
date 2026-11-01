"""Load only documented local settings from .env without overriding process values."""
import os
from pathlib import Path


ALLOWED = {
    "DELIVERYPROOF_ADMIN_PASSWORD", "DELIVERYPROOF_PUBLIC_URL",
    "DELIVERYPROOF_AUTO_EMAIL", "DELIVERYPROOF_SMTP_HOST", "DELIVERYPROOF_SMTP_PORT",
    "DELIVERYPROOF_SMTP_USER", "DELIVERYPROOF_SMTP_PASSWORD", "DELIVERYPROOF_SMTP_FROM",
    "DELIVERYPROOF_AUTO_CALL", "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN",
    "TWILIO_FROM_NUMBER",
}


def load_local_env(root: Path) -> None:
    path = root / ".env"
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" not in line or line.lstrip().startswith("#"):
            continue
        name, value = line.split("=", 1)
        name = name.strip()
        if name in ALLOWED and value.strip():
            os.environ.setdefault(name, value.strip().strip('"').strip("'"))
