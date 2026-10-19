"""Portfolio state — what we hold, what we have working, and realised PnL.

Persisted to disk so a restart doesn't lose track of open positions. In
dry-run mode the same object simulates fills, which makes paper-trading and
live trading share one code path.
"""
from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from ..core.models import Order, Position, Side
from ..utils.logging import get_logger

log = get_logger("core.portfolio")


class Portfolio:
    def __init__(self, bankroll: float, state_path: str | None = None):
        self.bankroll = bankroll
        self.realised_pnl = 0.0
        self.positions: dict[str, Position] = {}   # keyed by token_id
        self.cash = bankroll
        self._state_path = Path(state_path) if state_path else None
        self._load()

    # ---- exposure accounting --------------------------------------------
    def exposure(self, token_id: str, mark: float) -> float:
        pos = self.positions.get(token_id)
        return pos.size * mark if pos else 0.0

    def total_exposure(self, marks: dict[str, float]) -> float:
        return sum(
            p.size * marks.get(tid, p.avg_price)
            for tid, p in self.positions.items()
        )

    def position(self, token_id: str) -> Position | None:
        return self.positions.get(token_id)

    # ---- apply fills -----------------------------------------------------
    def apply_fill(self, order: Order, fill_price: float, fill_size: float) -> None:
        """Update cash and positions from a (real or simulated) fill."""
        pos = self.positions.get(order.token_id)
        if order.side is Side.BUY:
            self.cash -= fill_price * fill_size
            if pos is None:
                self.positions[order.token_id] = Position(
                    token_id=order.token_id,
                    outcome="",
                    size=fill_size,
                    avg_price=fill_price,
                    market_condition_id=order.market_condition_id,
                )
            else:
                total = pos.size + fill_size
                pos.avg_price = (pos.avg_price * pos.size + fill_price * fill_size) / total
                pos.size = total
        else:  # SELL / close
            self.cash += fill_price * fill_size
            if pos is not None:
                realised = (fill_price - pos.avg_price) * min(fill_size, pos.size)
                self.realised_pnl += realised
                pos.size -= fill_size
                if pos.size <= 1e-9:
                    del self.positions[order.token_id]
        self._save()

    # ---- persistence -----------------------------------------------------
    def _save(self) -> None:
        if not self._state_path:
            return
        data = {
            "bankroll": self.bankroll,
            "cash": self.cash,
            "realised_pnl": self.realised_pnl,
            "positions": {k: asdict(v) for k, v in self.positions.items()},
        }
        try:
            self._state_path.write_text(json.dumps(data, indent=2))
        except OSError as e:  # pragma: no cover
            log.warning("could not persist state: %s", e)

    def _load(self) -> None:
        if not self._state_path or not self._state_path.exists():
            return
        try:
            data = json.loads(self._state_path.read_text())
        except (OSError, json.JSONDecodeError) as e:  # pragma: no cover
            log.warning("could not load state: %s", e)
            return
        self.cash = data.get("cash", self.bankroll)
        self.realised_pnl = data.get("realised_pnl", 0.0)
        self.positions = {
            k: Position(**v) for k, v in data.get("positions", {}).items()
        }
        log.info("restored %d positions, realised_pnl=%.2f",
                 len(self.positions), self.realised_pnl)
