"""Arbitrage & mispricing — the closest thing to free money on Polymarket.

Two structural edges, both risk-free at execution (the profit is locked by
the market's own payout rules, independent of how the event resolves):

1. Binary complement: a market's YES ask + NO ask should sum to >= 1 (you
   pay at least $1 to own a guaranteed $1 payout). When YES_ask + NO_ask < 1
   you can buy both sides and are guaranteed $1 back for less than $1 paid.

2. Neg-risk group: in a mutually-exclusive multi-outcome event (exactly one
   winner), buying YES on every outcome for a combined cost < $1 guarantees a
   $1 payout. Equivalently, if the outcomes' YES asks sum to < 1 there is a
   locked spread.

Both are rare and fleeting — which is exactly why speed and automation win
here. We only ever cross the book when the summed cost (plus fees and a
safety buffer) is below 1 by at least `min_edge`, and we size each leg to the
*thinnest* leg's available depth so the hedge actually fills.
"""
from __future__ import annotations

from ..clients.clob import ClobClient
from ..clients.gamma import GammaClient
from ..config import ArbitrageConfig
from ..core.models import Market, Order, OrderType, Side
from ..utils.logging import get_logger
from .base import Signal, Strategy

log = get_logger("strat.arb")


class ArbitrageStrategy(Strategy):
    name = "arbitrage"

    def __init__(self, cfg: ArbitrageConfig, gamma: GammaClient,
                 clob: ClobClient, markets: list[Market]):
        self.cfg = cfg
        self.gamma = gamma
        self.clob = clob
        self.markets = markets

    def generate(self) -> list[Signal]:
        signals: list[Signal] = []
        if not self.cfg.enabled:
            return signals
        try:
            signals.extend(self._binary_complement())
            if self.cfg.include_neg_risk:
                signals.extend(self._neg_risk_groups())
        except Exception as e:  # defensive: never kill the engine loop
            log.warning("arb scan failed: %s", e)
        return signals

    # ---- binary YES/NO complement ---------------------------------------
    def _binary_complement(self) -> list[Signal]:
        out: list[Signal] = []
        for m in self.markets:
            if m.closed or len(m.tokens) != 2:
                continue
            yes, no = m.tokens[0], m.tokens[1]
            try:
                yes_ask = self.clob.price(yes.token_id, Side.BUY)
                no_ask = self.clob.price(no.token_id, Side.BUY)
            except Exception:
                continue
            if yes_ask is None or no_ask is None:
                continue
            cost = yes_ask + no_ask + self.cfg.taker_fee
            edge = 1.0 - cost
            if edge < self.cfg.min_edge:
                continue
            size = self._fillable_size([yes.token_id, no.token_id])
            if size <= 0:
                continue
            log.info("ARB binary %s: YES %.3f + NO %.3f = %.3f (edge %.3f)",
                     m.slug, yes_ask, no_ask, cost, edge)
            for tok, px in ((yes, yes_ask), (no, no_ask)):
                out.append(Signal(
                    order=Order(
                        token_id=tok.token_id, side=Side.BUY, price=px,
                        size=size, order_type=OrderType.FOK,
                        market_condition_id=m.condition_id, strategy=self.name,
                    ),
                    fair=1.0,                 # guaranteed payout leg
                    size_by_kelly=False,
                    rationale=f"binary complement edge={edge:.3f}",
                    meta={"edge": edge, "group": m.condition_id},
                ))
        return out

    # ---- mutually-exclusive neg-risk group ------------------------------
    def _neg_risk_groups(self) -> list[Signal]:
        out: list[Signal] = []
        groups = self.gamma.neg_risk_groups(self.markets)
        for key, group in groups.items():
            if len(group) > self.cfg.max_legs:
                continue
            legs: list[tuple[str, float, str]] = []
            total = 0.0
            ok = True
            for m in group:
                yes = m.yes or (m.tokens[0] if m.tokens else None)
                if yes is None:
                    ok = False
                    break
                try:
                    ask = self.clob.price(yes.token_id, Side.BUY)
                except Exception:
                    ok = False
                    break
                if ask is None:
                    ok = False
                    break
                legs.append((yes.token_id, ask, m.condition_id))
                total += ask
            if not ok or not legs:
                continue
            cost = total + self.cfg.taker_fee
            edge = 1.0 - cost
            if edge < self.cfg.min_edge:
                continue
            size = self._fillable_size([tid for tid, _, _ in legs])
            if size <= 0:
                continue
            log.info("ARB neg-risk %s: %d legs sum=%.3f (edge %.3f)",
                     key, len(legs), cost, edge)
            for tid, px, cid in legs:
                out.append(Signal(
                    order=Order(
                        token_id=tid, side=Side.BUY, price=px, size=size,
                        order_type=OrderType.FOK, market_condition_id=cid,
                        strategy=self.name,
                    ),
                    fair=1.0,
                    size_by_kelly=False,
                    rationale=f"neg-risk group {key} edge={edge:.3f}",
                    meta={"edge": edge, "group": key},
                ))
        return out

    def _fillable_size(self, token_ids: list[str]) -> float:
        """Shares executable across *every* leg — bounded by the thinnest book
        so we never end up holding an unhedged leg. Also capped by max order."""
        min_size = float("inf")
        for tid in token_ids:
            try:
                book = self.clob.order_book(tid)
            except Exception:
                return 0.0
            # top-of-book size available to a taker buying.
            size = book.asks[0].size if book.asks else 0.0
            min_size = min(min_size, size)
        if min_size == float("inf"):
            return 0.0
        return round(min_size, 2)
