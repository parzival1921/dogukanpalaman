"""Smart-money mirroring.

Thesis: a small set of wallets on Polymarket are persistently right. Their
trades are public on-chain the moment they land. If we can (a) identify those
wallets by realised performance and (b) react within seconds, we inherit
their edge without needing their information — we just need their signal and
more speed than a human.

Pipeline each tick:
  1. maintain a follow-list (configured wallets + auto-discovered top wallets)
  2. pull each followed wallet's most recent trades via the Data API
  3. for any *fresh* buy (within max_lag) at a sane price we haven't copied,
     emit a copy signal. Our fair value is the price the smart wallet paid —
     i.e. we trust their entry — and the risk engine Kelly-sizes from there.

We deliberately do NOT copy sells blindly (exits are noisier and often about
their portfolio, not the market); we instead close our own copy when it hits
our own take-profit/invalidation handled by the engine.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

from ..clients.data_api import DataApiClient
from ..config import SmartMoneyConfig
from ..core.models import Order, OrderType, Side
from ..utils.logging import get_logger
from ..utils.math import clamp
from .base import Signal, Strategy

log = get_logger("strat.smartmoney")


class SmartMoneyStrategy(Strategy):
    name = "smart_money"

    def __init__(self, cfg: SmartMoneyConfig, data: DataApiClient,
                 seed_markets: list[str] | None = None):
        self.cfg = cfg
        self.data = data
        self.seed_markets = seed_markets or []
        self.follow: dict[str, dict[str, float]] = {
            w: {"realised_pnl": 0.0, "winrate": 0.0} for w in cfg.wallets
        }
        self._copied: set[str] = set()      # trade fingerprints already acted on
        self._last_discovery = 0.0

    # ---- wallet discovery ------------------------------------------------
    def refresh_follow_list(self, force: bool = False) -> None:
        if not self.cfg.auto_discover:
            return
        # Re-rank wallets at most once every 6h — discovery is expensive.
        if not force and time.time() - self._last_discovery < 6 * 3600:
            return
        self._last_discovery = time.time()
        if not self.seed_markets:
            log.debug("no seed markets for discovery yet")
            return
        try:
            ranked = self.data.discover_smart_wallets(
                self.seed_markets,
                min_pnl=self.cfg.min_wallet_pnl,
                min_winrate=self.cfg.min_wallet_winrate,
                lookback_days=self.cfg.lookback_days,
            )
        except Exception as e:
            log.warning("discovery failed: %s", e)
            return
        for wallet, stats in ranked[:50]:
            self.follow[wallet] = stats
        log.info("following %d smart-money wallets", len(self.follow))

    # ---- signal generation ----------------------------------------------
    def generate(self) -> list[Signal]:
        signals: list[Signal] = []
        if not self.cfg.enabled or not self.follow:
            return signals
        now = datetime.now(timezone.utc)
        for wallet, stats in list(self.follow.items()):
            try:
                recent = self.data.trades(wallet=wallet, limit=25)
            except Exception as e:
                log.debug("trade fetch failed for %s: %s", wallet[:10], e)
                continue
            for tr in recent:
                if tr.side is not Side.BUY:
                    continue
                lag = (now - tr.timestamp).total_seconds()
                if lag > self.cfg.max_lag_seconds:
                    continue
                if tr.price <= 0 or tr.price > self.cfg.max_follow_price:
                    continue
                fp = f"{wallet}:{tr.token_id}:{int(tr.timestamp.timestamp())}"
                if fp in self._copied:
                    continue
                self._copied.add(fp)

                copy_size = max(1.0, tr.size * self.cfg.copy_ratio)
                # trust their entry as fair; nudge slightly toward their price
                fair = clamp(tr.price + 0.0, 0.01, 0.99)
                conf = _confidence(stats)
                log.info("COPY %s bought %s @ %.3f (pnl=$%.0f wr=%.2f lag=%.0fs)",
                         wallet[:8], tr.outcome, tr.price,
                         stats.get("realised_pnl", 0), stats.get("winrate", 0), lag)
                signals.append(Signal(
                    order=Order(
                        token_id=tr.token_id,
                        side=Side.BUY,
                        # cross up to one tick to actually get filled
                        price=min(self.cfg.max_follow_price, round(tr.price + 0.01, 3)),
                        size=copy_size,
                        order_type=OrderType.GTC,
                        market_condition_id=tr.condition_id,
                        strategy=self.name,
                    ),
                    fair=fair,
                    size_by_kelly=True,
                    rationale=(f"mirror {wallet[:8]} (pnl=${stats.get('realised_pnl',0):.0f}, "
                               f"wr={stats.get('winrate',0):.2f}, conf={conf:.2f})"),
                    meta={"wallet": wallet, "confidence": conf, "lag": lag},
                ))
        # keep the dedupe set from growing without bound
        if len(self._copied) > 10000:
            self._copied = set(list(self._copied)[-5000:])
        return signals


def _confidence(stats: dict[str, float]) -> float:
    """Map a wallet's track record to a 0..1 confidence used for logging and
    optional size scaling. Rewards both profitability and consistency."""
    pnl = stats.get("realised_pnl", 0.0)
    wr = stats.get("winrate", 0.0)
    pnl_score = clamp(pnl / 50000.0, 0.0, 1.0)
    wr_score = clamp((wr - 0.5) / 0.3, 0.0, 1.0)
    return 0.5 * pnl_score + 0.5 * wr_score
