"""Unit tests for the pure numeric helpers and the risk engine.

These never touch the network — they validate the money-at-risk logic, which
is the part that must be correct.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from polybot.config import RiskConfig                     # noqa: E402
from polybot.core.models import Order, Side                # noqa: E402
from polybot.core.portfolio import Portfolio               # noqa: E402
from polybot.core.risk import RiskEngine                   # noqa: E402
from polybot.utils.math import (                           # noqa: E402
    clamp, edge, kelly_fraction, round_to_tick, sharpe,
)


def test_round_to_tick_stays_in_bounds():
    assert round_to_tick(0.5, 0.01) == 0.5
    assert 0 < round_to_tick(0.0001, 0.01) < 1
    assert 0 < round_to_tick(0.9999, 0.01) < 1


def test_kelly_zero_without_edge():
    # fair == price → no edge → no bet
    assert kelly_fraction(0.5, 0.5) == 0.0
    # negative edge → clamped to zero
    assert kelly_fraction(0.4, 0.6) == 0.0


def test_kelly_positive_with_edge():
    f = kelly_fraction(0.7, 0.5)   # we think 70%, market prices 50%
    assert 0 < f <= 1


def test_edge_sign():
    assert edge(0.7, 0.5, side_is_buy=True) > 0      # cheap YES
    assert edge(0.3, 0.5, side_is_buy=True) < 0      # overpriced YES
    assert edge(0.3, 0.5, side_is_buy=False) > 0     # good to sell


def test_clamp():
    assert clamp(5, 0, 1) == 1
    assert clamp(-5, 0, 1) == 0
    assert clamp(0.5, 0, 1) == 0.5


def test_sharpe_zero_for_flat():
    assert sharpe([0.0, 0.0, 0.0]) == 0.0
    assert sharpe([0.01, 0.01, 0.01]) == 0.0  # no variance


def _engine(bankroll=1000.0, **over):
    cfg = RiskConfig(bankroll_usdc=bankroll, **over)
    pf = Portfolio(bankroll, state_path=None)
    return RiskEngine(cfg, pf), pf


def test_risk_rejects_bad_price():
    eng, _ = _engine()
    o = Order("tok", Side.BUY, price=1.5, size=10)
    d = eng.evaluate(o, fair=0.9)
    assert not d.approved


def test_risk_rejects_without_edge():
    eng, _ = _engine()
    o = Order("tok", Side.BUY, price=0.5, size=10)
    d = eng.evaluate(o, fair=0.5)        # no edge
    assert not d.approved


def test_risk_caps_position_size():
    # 5% of 1000 = $50 max per market; Kelly may want more but is capped.
    eng, _ = _engine(max_position_pct=0.05, kelly_fraction=1.0, max_order_usdc=1000)
    o = Order("tok", Side.BUY, price=0.5, size=100000)
    d = eng.evaluate(o, fair=0.9)
    assert d.approved
    assert d.order.notional <= 50.0 + 1e-6


def test_risk_allows_sell_of_held_position():
    eng, pf = _engine()
    buy = Order("tok", Side.BUY, price=0.5, size=20)
    pf.apply_fill(buy, 0.5, 20)
    sell = Order("tok", Side.SELL, price=0.6, size=20)
    d = eng.evaluate(sell, fair=0.6)
    assert d.approved
    assert d.order.size == 20


def test_daily_loss_halts_trading():
    eng, pf = _engine(max_daily_loss_pct=0.10)
    pf.realised_pnl = -150  # 15% drawdown on a $1000 bankroll
    o = Order("tok", Side.BUY, price=0.5, size=10)
    d = eng.evaluate(o, fair=0.9)
    assert not d.approved
    assert "halt" in d.reason.lower()


def test_portfolio_pnl_accounting():
    pf = Portfolio(1000.0, state_path=None)
    pf.apply_fill(Order("t", Side.BUY, 0.40, 100), 0.40, 100)   # spend $40
    assert pf.cash == 960.0
    pf.apply_fill(Order("t", Side.SELL, 0.55, 100), 0.55, 100)  # get $55
    assert round(pf.realised_pnl, 2) == 15.0
    assert "t" not in pf.positions
