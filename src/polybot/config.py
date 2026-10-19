"""Configuration loading.

Secrets (private key, API creds) come from environment variables only and
are never written to the config file or logged. Everything else — strategy
toggles, risk limits, polling cadence — lives in a YAML file so behaviour
can be tuned without code changes.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover - yaml is a declared dependency
    yaml = None


@dataclass
class RiskConfig:
    bankroll_usdc: float = 1000.0
    max_position_pct: float = 0.05        # per-market cap as fraction of bankroll
    max_total_exposure_pct: float = 0.60  # across all open positions
    kelly_fraction: float = 0.25          # fractional Kelly multiplier
    max_daily_loss_pct: float = 0.10      # halt trading past this drawdown
    min_order_usdc: float = 5.0
    max_order_usdc: float = 250.0


@dataclass
class SmartMoneyConfig:
    enabled: bool = True
    wallets: list[str] = field(default_factory=list)   # explicit follow-list
    auto_discover: bool = True                          # rank wallets by realised PnL
    min_wallet_pnl: float = 5000.0                      # discovery threshold (USDC)
    min_wallet_winrate: float = 0.58
    lookback_days: int = 90
    copy_ratio: float = 0.10          # size we take vs. the leader's size
    max_follow_price: float = 0.90    # don't chase near-resolved outcomes
    max_lag_seconds: int = 120        # ignore trades we saw too late


@dataclass
class ArbitrageConfig:
    enabled: bool = True
    min_edge: float = 0.015           # min summed mispricing to act on (1.5%)
    max_legs: int = 8
    include_neg_risk: bool = True     # exploit neg-risk multi-outcome groups
    taker_fee: float = 0.0            # Polymarket CLOB is currently 0 taker fee


@dataclass
class MarketMakingConfig:
    enabled: bool = False
    markets: list[str] = field(default_factory=list)   # condition_ids to quote
    target_spread: float = 0.02
    quote_size_usdc: float = 20.0
    max_inventory_usdc: float = 100.0
    skew_strength: float = 0.5        # how hard to lean quotes against inventory
    min_market_volume_24h: float = 10000.0


@dataclass
class EngineConfig:
    poll_interval_seconds: float = 5.0
    dry_run: bool = True              # paper mode unless explicitly disabled
    log_level: str = "INFO"
    state_path: str = ".polybot_state.json"


@dataclass
class Secrets:
    private_key: str | None = None
    clob_api_key: str | None = None
    clob_api_secret: str | None = None
    clob_api_passphrase: str | None = None
    funder_address: str | None = None   # proxy/safe wallet holding USDC

    @property
    def can_trade(self) -> bool:
        return bool(self.private_key)

    @classmethod
    def from_env(cls) -> "Secrets":
        return cls(
            private_key=os.getenv("POLY_PRIVATE_KEY"),
            clob_api_key=os.getenv("POLY_CLOB_API_KEY"),
            clob_api_secret=os.getenv("POLY_CLOB_API_SECRET"),
            clob_api_passphrase=os.getenv("POLY_CLOB_PASSPHRASE"),
            funder_address=os.getenv("POLY_FUNDER_ADDRESS"),
        )


@dataclass
class Config:
    risk: RiskConfig = field(default_factory=RiskConfig)
    smart_money: SmartMoneyConfig = field(default_factory=SmartMoneyConfig)
    arbitrage: ArbitrageConfig = field(default_factory=ArbitrageConfig)
    market_making: MarketMakingConfig = field(default_factory=MarketMakingConfig)
    engine: EngineConfig = field(default_factory=EngineConfig)
    secrets: Secrets = field(default_factory=Secrets.from_env)

    @classmethod
    def load(cls, path: str | os.PathLike[str] | None = None) -> "Config":
        data: dict[str, Any] = {}
        if path:
            p = Path(path)
            if p.exists():
                if yaml is None:
                    raise RuntimeError("pyyaml is required to read a config file")
                data = yaml.safe_load(p.read_text()) or {}
        return cls(
            risk=RiskConfig(**data.get("risk", {})),
            smart_money=SmartMoneyConfig(**data.get("smart_money", {})),
            arbitrage=ArbitrageConfig(**data.get("arbitrage", {})),
            market_making=MarketMakingConfig(**data.get("market_making", {})),
            engine=EngineConfig(**data.get("engine", {})),
            secrets=Secrets.from_env(),
        )
