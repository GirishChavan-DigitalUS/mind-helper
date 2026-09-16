"""Configuration loading: secrets from .env, tunables from config.yaml.

config.yaml is reloaded on demand (hot reload) so parameters can be tuned
per trading day without restarting the app. Secrets are read once at startup.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.yaml"


@dataclass(frozen=True)
class Secrets:
    app_key: str
    app_secret: str
    region: str
    account_id: str
    api_endpoint: str
    events_endpoint: str
    webhook_secret: str
    dry_run: bool
    shadow: bool
    host: str
    port: int


def load_secrets() -> Secrets:
    load_dotenv(ROOT / ".env")
    return Secrets(
        app_key=os.getenv("WEBULL_APP_KEY", ""),
        app_secret=os.getenv("WEBULL_APP_SECRET", ""),
        region=os.getenv("WEBULL_REGION", "us"),
        account_id=os.getenv("WEBULL_ACCOUNT_ID", ""),
        api_endpoint=os.getenv("WEBULL_API_ENDPOINT", "api.webull.com"),
        events_endpoint=os.getenv("WEBULL_EVENTS_ENDPOINT", "events-api.webull.com"),
        webhook_secret=os.getenv("WEBHOOK_SECRET", ""),
        dry_run=os.getenv("DRY_RUN", "true").strip().lower() in ("1", "true", "yes"),
        shadow=os.getenv("SHADOW_MODE", "false").strip().lower() in ("1", "true", "yes"),
        host=os.getenv("HOST", "127.0.0.1"),
        port=int(os.getenv("PORT", "8000")),
    )


def load_params(path: Path = CONFIG_PATH) -> dict[str, Any]:
    """Read and parse config.yaml fresh each call (hot reload)."""
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}
