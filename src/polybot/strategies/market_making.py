"""Inventory-aware market making.

We continuously quote a two-sided market inside the current book, earning the
spread when both sides fill. The only real risk in MM is inventory: if the
market keeps trading against us we accumulate a losing position. So quotes are
*skewed* against inventory — the more YES we hold, the lower (more aggressive
to sell) we price our ask and the more passive our bid, pushing us back toward
flat. Hard inventory caps stop us from quoting a side once we're too long/short.

This is off by default (`market_making.enabled`) because it needs resting
orders and careful monitoring; smart-money and arbitrage are the primary
earners. When on, it only quotes markets with enough 24h volume to mean-revert.
"""
from __future__ import annotations

from ..clients.clob import ClobClient
from ..config import MarketMakingConfig
from ..core.models import Market, Order, OrderType, Side
from ..core.portfolio import Portfolio
from ..utils.logging import get_logger
from ..utils.math import clamp, round_to_tick
from .base import Signal, Strategy

log = get_logger("strat.mm")


class MarketMakingStrategy(Strategy):
    name = "market_making"

    def __init__(self, cfg: MarketMakingConfig, clob: ClobClient,
                 portfolio: Portfolio, markets: dict[str, Market]):
        self.cfg = cfg
        self.clob = clob
        self.pf = portfolio
        self.markets = markets   # condition_id -> Market

    def generate(self) -> list[Signal]:
        signals: list[Signal] = []
        if not self.cfg.enabled:
            return signals
        for cid in self.cfg.markets:
            market = self.markets.get(cid)
            if market is None or market.closed or len(market.tokens) < 1:
                continue
            if market.volume_24h < self.cfg.min_market_volume_24h:
                continue
            token = market.tokens[0]  # quote the YES leg
            try:
                book = self.clob.order_book(token.token_id)
            except Exception as e:
                log.debug("book fetch failed %s: %s", token.token_id[:10], e)
                continue
            mid = book.mid
            if mid is None:
                continue

            pos = self.pf.position(token.token_id)
            inv_notional = (pos.size * mid) if pos else 0.0
            inv_ratio = clamp(inv_notional / self.cfg.max_inventory_usdc, -1.0, 1.0)

            half = self.cfg.target_spread / 2.0
            # Skew: long inventory shifts the whole quote down (sell cheaper,
            # buy more reluctantly), short inventory shifts it up.
            skew = inv_ratio * self.cfg.skew_strength * half
            bid_px = round_to_tick(mid - half - skew, market.min_tick)
            ask_px = round_to_tick(mid + half - skew, market.min_tick)

            size = self.cfg.quote_size_usdc / mid

            # Stop quoting a side once inventory cap is breached on that side.
            if inv_notional < self.cfg.max_inventory_usdc:
                signals.append(Signal(
                    order=Order(token_id=token.token_id, side=Side.BUY,
                                price=bid_px, size=size, order_type=OrderType.GTC,
                                market_condition_id=cid, strategy=self.name),
                    fair=mid, size_by_kelly=False,
                    rationale=f"MM bid (inv_ratio={inv_ratio:.2f})",
                    meta={"quote": "bid", "inv_ratio": inv_ratio},
                ))
            if pos and pos.size > 0 and inv_notional > -self.cfg.max_inventory_usdc:
                signals.append(Signal(
                    order=Order(token_id=token.token_id, side=Side.SELL,
                                price=ask_px, size=min(size, pos.size),
                                order_type=OrderType.GTC,
                                market_condition_id=cid, strategy=self.name),
                    fair=mid, size_by_kelly=False,
                    rationale=f"MM ask (inv_ratio={inv_ratio:.2f})",
                    meta={"quote": "ask", "inv_ratio": inv_ratio},
                ))
        return signals
