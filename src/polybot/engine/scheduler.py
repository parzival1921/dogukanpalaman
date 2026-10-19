"""The orchestrator — wires market data, strategies, risk and execution into
one loop.

Each tick:
  1. refresh the tradeable market universe (periodically, not every tick)
  2. ask each enabled strategy for signals
  3. run every signal through the risk engine (sizing + veto)
  4. execute approved orders (paper or live)
  5. persist state

The loop is deliberately boring and sequential: correctness and auditable
risk control matter far more than shaving milliseconds, and every order has
exactly one path to the exchange.
"""
from __future__ import annotations

import signal as _signal
import time
from datetime import date

from ..clients.clob import ClobClient
from ..clients.data_api import DataApiClient
from ..clients.gamma import GammaClient
from ..config import Config
from ..core.portfolio import Portfolio
from ..core.risk import RiskEngine
from ..strategies.arbitrage import ArbitrageStrategy
from ..strategies.base import Strategy
from ..strategies.market_making import MarketMakingStrategy
from ..strategies.smart_money import SmartMoneyStrategy
from ..utils.logging import get_logger
from .executor import Executor

log = get_logger("engine")


class Engine:
    MARKET_REFRESH_SECONDS = 300

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.gamma = GammaClient()
        self.clob = ClobClient(secrets=cfg.secrets)
        self.data = DataApiClient()

        self.portfolio = Portfolio(cfg.risk.bankroll_usdc, cfg.engine.state_path)
        self.risk = RiskEngine(cfg.risk, self.portfolio)
        self.executor = Executor(self.clob, self.portfolio, dry_run=cfg.engine.dry_run)

        self.markets: list = []
        self.markets_by_cid: dict = {}
        self._last_market_refresh = 0.0
        self._today = date.today()
        self._running = False

        self.strategies: list[Strategy] = []
        self.smart_money: SmartMoneyStrategy | None = None

    # ---- setup -----------------------------------------------------------
    def _refresh_markets(self) -> None:
        self.markets = self.gamma.fetch_markets(
            active=True, closed=False, limit=500,
            min_volume_24h=self.cfg.market_making.min_market_volume_24h
            if self.cfg.market_making.enabled else 1000.0,
        )
        self.markets_by_cid = {m.condition_id: m for m in self.markets}
        self._last_market_refresh = time.time()

    def _build_strategies(self) -> None:
        self.strategies = []
        seed = [m.condition_id for m in self.markets[:40]]

        if self.cfg.smart_money.enabled:
            self.smart_money = SmartMoneyStrategy(
                self.cfg.smart_money, self.data, seed_markets=seed
            )
            self.smart_money.refresh_follow_list(force=True)
            self.strategies.append(self.smart_money)

        if self.cfg.arbitrage.enabled:
            self.strategies.append(
                ArbitrageStrategy(self.cfg.arbitrage, self.gamma,
                                  self.clob, self.markets)
            )

        if self.cfg.market_making.enabled:
            self.strategies.append(
                MarketMakingStrategy(self.cfg.market_making, self.clob,
                                     self.portfolio, self.markets_by_cid)
            )
        log.info("active strategies: %s",
                 ", ".join(s.name for s in self.strategies) or "(none)")

    # ---- main loop -------------------------------------------------------
    def _marks(self) -> dict[str, float]:
        """Best-effort marks for exposure accounting (use last known prices)."""
        return {t.token_id: t.price for m in self.markets for t in m.tokens if t.price}

    def tick(self) -> None:
        # daily circuit-breaker reset
        if date.today() != self._today:
            self._today = date.today()
            self.risk.new_day()
            log.info("new trading day — risk baseline reset")

        if time.time() - self._last_market_refresh > self.MARKET_REFRESH_SECONDS:
            self._refresh_markets()
            # rebind strategies that hold a market snapshot
            for s in self.strategies:
                if isinstance(s, ArbitrageStrategy):
                    s.markets = self.markets
                if isinstance(s, MarketMakingStrategy):
                    s.markets = self.markets_by_cid
            if self.smart_money:
                self.smart_money.seed_markets = [m.condition_id for m in self.markets[:40]]
                self.smart_money.refresh_follow_list()

        marks = self._marks()
        for strat in self.strategies:
            try:
                signals = strat.generate()
            except Exception as e:
                log.warning("strategy %s raised: %s", strat.name, e)
                continue
            for sig in signals:
                decision = self.risk.evaluate(
                    sig.order, fair=sig.fair, marks=marks,
                    size_by_kelly=sig.size_by_kelly,
                )
                if not decision.approved or decision.order is None:
                    log.debug("vetoed [%s] %s: %s", strat.name,
                              sig.rationale, decision.reason)
                    continue
                log.info("SIGNAL [%s] %s | %s", strat.name,
                         sig.rationale, decision.reason)
                self.executor.execute(decision.order)

        equity = self.portfolio.cash + self.portfolio.total_exposure(marks)
        log.info("equity=$%.2f cash=$%.2f realised=$%.2f positions=%d",
                 equity, self.portfolio.cash, self.portfolio.realised_pnl,
                 len(self.portfolio.positions))

    def run(self) -> None:
        self._refresh_markets()
        self._build_strategies()
        self._running = True

        def _stop(*_: object) -> None:
            log.info("shutdown requested")
            self._running = False

        _signal.signal(_signal.SIGINT, _stop)
        _signal.signal(_signal.SIGTERM, _stop)

        mode = "DRY-RUN (paper)" if self.cfg.engine.dry_run else "LIVE"
        log.info("engine starting in %s mode, bankroll=$%.2f",
                 mode, self.cfg.risk.bankroll_usdc)
        while self._running:
            start = time.time()
            try:
                self.tick()
            except Exception as e:  # never die on a single bad tick
                log.exception("tick failed: %s", e)
            elapsed = time.time() - start
            time.sleep(max(0.0, self.cfg.engine.poll_interval_seconds - elapsed))
        log.info("engine stopped. realised PnL=$%.2f", self.portfolio.realised_pnl)
