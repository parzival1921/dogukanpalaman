"""Strategy interface.

A strategy's only job is to turn market state into *signals* — proposed
orders paired with a fair-value estimate and a rationale. Strategies never
touch the exchange or the portfolio directly; the engine runs each signal
through the risk engine and the executor. This keeps strategies pure,
testable, and impossible to let bypass risk limits.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from ..core.models import Order


@dataclass
class Signal:
    order: Order
    fair: float                  # strategy's probability estimate (0..1)
    rationale: str = ""
    size_by_kelly: bool = True   # arbitrage sets this False (structural edge)
    meta: dict = field(default_factory=dict)


class Strategy(ABC):
    name: str = "base"

    @abstractmethod
    def generate(self) -> list[Signal]:
        """Produce zero or more signals for this tick. Must not raise on
        transient data errors — log and return what you have."""
        raise NotImplementedError
