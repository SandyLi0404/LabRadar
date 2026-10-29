"""Runtime configuration.

Thresholds that shape what the user sees -- the activity-shift sensitivity in
particular -- live here rather than in code, so they can be tuned against real
data without a redeploy.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    database_url: str = "postgresql+psycopg://labradar:labradar@localhost:5432/labradar"

    # Required. The client raises on startup if this is unset; see sources/openalex.py
    # for why a key rather than the old mailto convention.
    openalex_api_key: str = ""

    # Stop a run while there is still allowance left to diagnose with.
    openalex_reserve_usd: float = 0.01

    # An activity_shift event fires when the last-year count moves by at least
    # MIN_DELTA papers *and* at least MIN_PCT of its previous value. Without
    # both, ordinary indexing jitter would emit an event for everyone weekly.
    activity_shift_min_delta: int = 2
    activity_shift_min_pct: float = 0.25

    request_delay_seconds: float = 0.2
    user_agent: str = "LabRadar/0.1 (+research aggregator)"

    api_page_size: int = 25
    api_max_page_size: int = 200

    # Where the static frontend lives. Left unset it is discovered; set
    # FRONTEND_DIR to override when the layout is unusual.
    frontend_dir: Path | None = None


@lru_cache
def get_settings() -> Settings:
    return Settings()
