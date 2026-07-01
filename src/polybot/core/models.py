"""Core domain models for the Polymarket trading system.

These are deliberately framework-free dataclasses so that clients,
strategies and the execution engine all speak the same language.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    GTC = "GTC"   # good-till-cancelled (resting limit order)
    FOK = "FOK"   # fill-or-kill (marketable)
    GTD = "GTD"   # good-till-date


@dataclass(frozen=True)
class Token:
    """A single tradeable outcome (YES or NO) of a market."""
    token_id: str
    outcome: str            # e.g. "Yes" / "No"
    price: float = 0.0      # last known midpoint, 0..1


@dataclass
class Market:
    """A Polymarket CLOB market. `condition_id` is the on-chain identity."""
    condition_id: str
    question: str
    slug: str
    tokens: list[Token] = field(default_factory=list)
    active: bool = True
    closed: bool = False
    end_date: Optional[datetime] = None
    min_tick: float = 0.001
    min_size: float = 5.0
    neg_risk: bool = False   # part of a mutually-exclusive multi-outcome group
    volume_24h: float = 0.0
    liquidity: float = 0.0

    def token_by_outcome(self, outcome: str) -> Optional[Token]:
        for t in self.tokens:
            if t.outcome.lower() == outcome.lower():
                return t
        return None

    @property
    def yes(self) -> Optional[Token]:
        return self.token_by_outcome("Yes")

    @property
    def no(self) -> Optional[Token]:
        return self.token_by_outcome("No")


@dataclass
class OrderBookLevel:
    price: float
    size: float


@dataclass
class OrderBook:
    token_id: str
    bids: list[OrderBookLevel] = field(default_factory=list)   # sorted desc by price
    asks: list[OrderBookLevel] = field(default_factory=list)   # sorted asc by price
    timestamp: Optional[datetime] = None

    @property
    def best_bid(self) -> Optional[float]:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> Optional[float]:
        return self.asks[0].price if self.asks else None

    @property
    def mid(self) -> Optional[float]:
        if self.best_bid is not None and self.best_ask is not None:
            return (self.best_bid + self.best_ask) / 2.0
        return None

    @property
    def spread(self) -> Optional[float]:
        if self.best_bid is not None and self.best_ask is not None:
            return self.best_ask - self.best_bid
        return None

    def depth(self, side: Side, price_limit: float) -> float:
        """USDC-notional depth available up to a price limit."""
        total = 0.0
        levels = self.asks if side is Side.BUY else self.bids
        for lvl in levels:
            if side is Side.BUY and lvl.price > price_limit:
                break
            if side is Side.SELL and lvl.price < price_limit:
                break
            total += lvl.price * lvl.size
        return total


@dataclass
class Order:
    token_id: str
    side: Side
    price: float
    size: float                     # number of shares
    order_type: OrderType = OrderType.GTC
    market_condition_id: Optional[str] = None
    client_id: Optional[str] = None
    strategy: Optional[str] = None

    @property
    def notional(self) -> float:
        return self.price * self.size


@dataclass
class Position:
    token_id: str
    outcome: str
    size: float                     # shares held
    avg_price: float
    market_condition_id: Optional[str] = None

    def unrealised(self, mark: float) -> float:
        return (mark - self.avg_price) * self.size


@dataclass
class Fill:
    order_client_id: str
    token_id: str
    side: Side
    price: float
    size: float
    timestamp: datetime
    fee: float = 0.0


@dataclass
class WalletActivity:
    """A trade observed on-chain from another participant (smart-money feed)."""
    wallet: str
    condition_id: str
    token_id: str
    outcome: str
    side: Side
    price: float
    size: float
    timestamp: datetime
