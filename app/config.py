"""Uygulama ayarları (ortam değişkenlerinden okunur)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _env_str(key: str, default: str) -> str:
    value = os.environ.get(key)
    return value if value not in (None, "") else default


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.environ[key])
    except (KeyError, ValueError):
        return default


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.environ[key])
    except (KeyError, ValueError):
        return default


def _env_bool(key: str, default: bool) -> bool:
    raw = os.environ.get(key)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on", "evet")


@dataclass(frozen=True)
class Settings:
    # --- altyapı ---
    database_path: str
    provider: str
    provider_config: str
    timezone: str

    # --- zamanlama ---
    sync_interval_seconds: int
    lock_minutes: float
    settle_delay_minutes: float
    lookahead_hours: float

    # --- tahmin modeli ---
    market_blend: float
    softmax_temperature: float
    max_prob_ratio: float

    # --- bahis / bankroll ---
    bankroll_start: float
    kelly_fraction: float
    min_ev: float
    max_ev: float
    min_probability: float
    min_odds: float
    max_odds: float
    max_bets_per_race: int
    max_stake_pct: float
    min_stake: float
    stake_rounding: float
    place_bets_enabled: bool

    # --- demo sağlayıcı ---
    demo_first_post_minutes: float
    demo_race_interval_minutes: float
    demo_races_per_day: int
    demo_seed: int
    demo_market_bias: float
    demo_overround: float

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            database_path=_env_str("DATABASE_PATH", str(BASE_DIR / "data" / "atyarisi.db")),
            provider=_env_str("PROVIDER", "demo").lower(),
            provider_config=_env_str("PROVIDER_CONFIG", str(BASE_DIR / "config" / "provider.tjk.json")),
            timezone=_env_str("TIMEZONE", "Europe/Istanbul"),
            sync_interval_seconds=_env_int("SYNC_INTERVAL_SECONDS", 20),
            lock_minutes=_env_float("LOCK_MINUTES", 5.0),
            settle_delay_minutes=_env_float("SETTLE_DELAY_MINUTES", 2.0),
            lookahead_hours=_env_float("LOOKAHEAD_HOURS", 12.0),
            market_blend=_env_float("MARKET_BLEND", 0.40),
            softmax_temperature=_env_float("SOFTMAX_TEMPERATURE", 1.1),
            max_prob_ratio=_env_float("MAX_PROB_RATIO", 3.0),
            bankroll_start=_env_float("BANKROLL_START", 1000.0),
            kelly_fraction=_env_float("KELLY_FRACTION", 0.25),
            min_ev=_env_float("MIN_EV", 0.08),
            max_ev=_env_float("MAX_EV", 1.00),
            min_probability=_env_float("MIN_PROBABILITY", 0.05),
            min_odds=_env_float("MIN_ODDS", 1.40),
            max_odds=_env_float("MAX_ODDS", 30.0),
            max_bets_per_race=_env_int("MAX_BETS_PER_RACE", 2),
            max_stake_pct=_env_float("MAX_STAKE_PCT", 0.04),
            min_stake=_env_float("MIN_STAKE", 5.0),
            stake_rounding=_env_float("STAKE_ROUNDING", 1.0),
            place_bets_enabled=_env_bool("PLACE_BETS_ENABLED", False),
            demo_first_post_minutes=_env_float("DEMO_FIRST_POST_MINUTES", 6.0),
            demo_race_interval_minutes=_env_float("DEMO_RACE_INTERVAL_MINUTES", 8.0),
            demo_races_per_day=_env_int("DEMO_RACES_PER_DAY", 9),
            demo_seed=_env_int("DEMO_SEED", 20260820),
            demo_market_bias=_env_float("DEMO_MARKET_BIAS", 0.22),
            demo_overround=_env_float("DEMO_OVERROUND", 1.18),
        )


settings = Settings.from_env()
