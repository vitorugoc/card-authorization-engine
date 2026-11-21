"""Runtime configuration: read once from the environment, validated at boot."""

import os
from dataclasses import dataclass
from functools import lru_cache


@dataclass(frozen=True)
class Settings:
    database_url: str
    webhook_secret: str  # HMAC key shared with the card processor
    ops_token: str  # bearer token for the ops routes


_ENV = {
    "database_url": "DATABASE_URL",
    "webhook_secret": "WEBHOOK_SECRET",
    "ops_token": "OPS_TOKEN",
}


@lru_cache
def get_settings() -> Settings:
    """Cached; also the FastAPI dependency, so tests can override it."""
    missing = [var for var in _ENV.values() if not os.environ.get(var)]
    if missing:
        raise RuntimeError(f"missing required config: {', '.join(missing)}")
    return Settings(**{field: os.environ[var] for field, var in _ENV.items()})
