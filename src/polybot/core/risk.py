"""Risk engine — the hard gate every order passes through.

No strategy places an order directly. Each proposes an order plus its own
estimate of fair value; the risk engine decides the final size (fractional
Kelly, capped by per-market and total-exposure limits) and can veto the
trade entirely (daily-loss circuit breaker, price sanity, bankroll). This is
the single place where "how much do we bet" is answered, so position sizing
can be reasoned about and tested in isolation.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..config import RiskConfig
from ..core.models import Order, Side
from ..core.portfolio import Portfolio
from ..utils.logging import get_logger
from ..utils.math import clamp, kelly_fraction

log = get_logger("core.risk")


@dataclass
class RiskDecision:
    approved: bool
    order: Order | None
    reason: str = ""


class RiskEngine:
    def __init__(self, cfg: RiskConfig, portfolio: Portfolio):
        self.cfg = cfg
        self.pf = portfolio
        self._day_start_equity = portfolio.bankroll + portfolio.realised_pnl

    def new_day(self) -> None:
        """Reset the daily-loss circuit breaker baseline."""
        self._day_start_equity = self.pf.bankroll + self.pf.realised_pnl

    def _halted(self) -> bool:
        equity = self.pf.bankroll + self.pf.realised_pnl
        dd = (self._day_start_equity - equity) / max(self._day_start_equity, 1e-9)
        return dd >= self.cfg.max_daily_loss_pct

    def evaluate(
        self,
        order: Order,
        *,
        fair: float,
        marks: dict[str, float] | None = None,
        size_by_kelly: bool = True,
    ) -> RiskDecision:
        """Approve, resize, or reject a proposed order.

        `fair` is the strategy's probability estimate for the token (0..1).
        For arbitrage the caller passes size_by_kelly=False because the edge
        is structural (locked at execution), not probabilistic.
        """
        marks = marks or {}

        if self._halted():
            return RiskDecision(False, None, "daily loss limit hit — trading halted")

        if not (0.0 < order.price < 1.0):
            return RiskDecision(False, None, f"price {order.price} outside (0,1)")

        # Sells only reduce risk; allow them up to the size we hold.
        if order.side is Side.SELL:
            pos = self.pf.position(order.token_id)
            if not pos or pos.size <= 0:
                return RiskDecision(False, None, "no position to sell")
            size = min(order.size, pos.size)
            return RiskDecision(True, _resized(order, size), "sell/close approved")

        # ---- BUY sizing -------------------------------------------------
        bankroll = self.pf.bankroll
        max_pos_notional = bankroll * self.cfg.max_position_pct
        max_notional = clamp(max_pos_notional, 0.0, self.cfg.max_order_usdc)

        if size_by_kelly:
            f = kelly_fraction(fair, order.price) * self.cfg.kelly_fraction
            if f <= 0:
                return RiskDecision(False, None, "no positive Kelly edge")
            kelly_notional = bankroll * f
            target_notional = min(kelly_notional, max_notional)
        else:
            target_notional = min(order.notional, max_notional)

        # Respect how much more we can add to this specific market.
        held = self.pf.exposure(order.token_id, marks.get(order.token_id, order.price))
        room_in_market = max(0.0, max_pos_notional - held)
        target_notional = min(target_notional, room_in_market)

        # Respect total portfolio exposure cap.
        total = self.pf.total_exposure(marks)
        total_cap = bankroll * self.cfg.max_total_exposure_pct
        target_notional = min(target_notional, max(0.0, total_cap - total))

        # Respect available cash.
        target_notional = min(target_notional, self.pf.cash)

        if target_notional < self.cfg.min_order_usdc:
            return RiskDecision(
                False, None,
                f"sized below minimum (${target_notional:.2f} < "
                f"${self.cfg.min_order_usdc:.2f})",
            )

        size = target_notional / order.price
        log.debug("risk OK: %s notional=$%.2f size=%.2f",
                  order.token_id[:10], target_notional, size)
        return RiskDecision(True, _resized(order, size), "approved")


def _resized(order: Order, size: float) -> Order:
    return Order(
        token_id=order.token_id,
        side=order.side,
        price=order.price,
        size=round(size, 2),
        order_type=order.order_type,
        market_condition_id=order.market_condition_id,
        client_id=order.client_id,
        strategy=order.strategy,
    )
