"""Small, dependency-free numeric helpers used across strategies.

Kept separate from strategy logic so they can be unit-tested in isolation
and reused by the risk engine.
"""
from __future__ import annotations

import math


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def round_to_tick(price: float, tick: float) -> float:
    """Round a price to the market's tick grid, staying inside (0, 1)."""
    if tick <= 0:
        return price
    rounded = round(price / tick) * tick
    # Polymarket prices live strictly inside (0, 1).
    return clamp(rounded, tick, 1.0 - tick)


def implied_prob(price: float) -> float:
    """A Polymarket share price *is* the implied probability (0..1)."""
    return clamp(price, 0.0, 1.0)


def edge(fair: float, price: float, side_is_buy: bool) -> float:
    """Expected value per share, in USDC.

    Buying a YES share at `price` pays out 1 if the event resolves true.
    If our fair estimate of that probability is `fair`, the per-share EV is
    (fair - price) when buying and (price - fair) when selling.
    """
    return (fair - price) if side_is_buy else (price - fair)


def kelly_fraction(prob: float, price: float) -> float:
    """Kelly-optimal fraction of bankroll for a binary payout market.

    A YES share bought at `price` returns (1/price - 1) : 1 on win and -1 on
    loss. Kelly f* = (b*p - q) / b, with b = (1-price)/price, q = 1-prob.
    Returns 0 when there is no edge. Callers should still apply a fractional
    Kelly multiplier and hard caps — full Kelly is far too aggressive in
    practice given estimation error in `prob`.
    """
    price = clamp(price, 1e-4, 1 - 1e-4)
    prob = clamp(prob, 0.0, 1.0)
    b = (1.0 - price) / price
    q = 1.0 - prob
    f = (b * prob - q) / b
    return max(0.0, f)


def annualise(return_pct: float, days: float) -> float:
    """Convert a period return into an annualised figure (compounded)."""
    if days <= 0:
        return 0.0
    return (1.0 + return_pct) ** (365.0 / days) - 1.0


def ewma(prev: float | None, sample: float, alpha: float) -> float:
    """Exponentially-weighted moving average update."""
    if prev is None:
        return sample
    return alpha * sample + (1.0 - alpha) * prev


def sharpe(returns: list[float], risk_free: float = 0.0) -> float:
    """Sharpe ratio of a list of per-period returns (not annualised)."""
    if len(returns) < 2:
        return 0.0
    mean = sum(returns) / len(returns)
    var = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    sd = math.sqrt(var)
    if sd == 0:
        return 0.0
    return (mean - risk_free) / sd
