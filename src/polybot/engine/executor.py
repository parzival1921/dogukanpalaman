"""Order executor.

Single choke point for turning an approved Order into a fill. In dry-run mode
it simulates a fill at the order price against the portfolio, so paper and
live trading share one path and the same PnL accounting. In live mode it
delegates signing/submission to the CLOB client and records the fill.
"""
from __future__ import annotations

from datetime import datetime, timezone

from ..clients.clob import ClobClient
from ..core.models import Fill, Order
from ..core.portfolio import Portfolio
from ..utils.logging import get_logger

log = get_logger("engine.executor")


class Executor:
    def __init__(self, clob: ClobClient, portfolio: Portfolio, dry_run: bool = True):
        self.clob = clob
        self.pf = portfolio
        self.dry_run = dry_run
        self.fills: list[Fill] = []

    def execute(self, order: Order) -> Fill | None:
        if self.dry_run:
            return self._simulate(order)
        return self._live(order)

    def _simulate(self, order: Order) -> Fill:
        # Optimistic but honest: assume full fill at the quoted price. Real
        # results differ (partial fills, slippage), which is why dry-run is a
        # sanity check, not a performance promise.
        self.pf.apply_fill(order, order.price, order.size)
        fill = Fill(
            order_client_id=order.client_id or "",
            token_id=order.token_id,
            side=order.side,
            price=order.price,
            size=order.size,
            timestamp=datetime.now(timezone.utc),
        )
        self.fills.append(fill)
        log.info("[PAPER] %s %.2f %s @ %.3f  (strategy=%s)",
                 order.side.value, order.size, order.token_id[:10],
                 order.price, order.strategy)
        return fill

    def _live(self, order: Order) -> Fill | None:
        try:
            resp = self.clob.place_order(order)
        except Exception as e:
            log.error("order rejected: %s", e)
            return None
        # py-clob-client returns the matched size when marketable; for resting
        # GTC orders the fill arrives asynchronously via websockets (not wired
        # here). We record the immediately-matched portion conservatively.
        matched = float(resp.get("takingAmount", 0) or 0) if isinstance(resp, dict) else 0.0
        filled_size = order.size if resp.get("success") and matched else matched
        if filled_size <= 0:
            log.info("order resting (no immediate fill): %s", resp)
            return None
        self.pf.apply_fill(order, order.price, filled_size)
        fill = Fill(
            order_client_id=str(resp.get("orderID", "")),
            token_id=order.token_id,
            side=order.side,
            price=order.price,
            size=filled_size,
            timestamp=datetime.now(timezone.utc),
        )
        self.fills.append(fill)
        return fill
