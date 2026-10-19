#!/usr/bin/env python3
"""polybot entry point.

Usage:
    python -m scripts.run --config config.yaml
    python -m scripts.run --live            # disable dry-run (requires keys)
    python -m scripts.run --scan-wallets    # one-off smart-money discovery
    python -m scripts.run --scan-arb        # one-off arbitrage scan, no trading

Live trading is opt-in and refuses to start without a configured private key.
Paper (dry-run) is the default so you can watch the system reason before any
real money is at risk.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# allow running from the repo root without installing
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from polybot.clients.clob import ClobClient          # noqa: E402
from polybot.clients.data_api import DataApiClient    # noqa: E402
from polybot.clients.gamma import GammaClient         # noqa: E402
from polybot.config import Config                      # noqa: E402
from polybot.engine.scheduler import Engine            # noqa: E402
from polybot.strategies.arbitrage import ArbitrageStrategy  # noqa: E402
from polybot.utils.logging import configure, get_logger    # noqa: E402

log = get_logger("run")


def scan_wallets(cfg: Config) -> None:
    gamma, data = GammaClient(), DataApiClient()
    markets = gamma.fetch_markets(limit=40, min_volume_24h=20000)
    seed = [m.condition_id for m in markets]
    ranked = data.discover_smart_wallets(
        seed,
        min_pnl=cfg.smart_money.min_wallet_pnl,
        min_winrate=cfg.smart_money.min_wallet_winrate,
        lookback_days=cfg.smart_money.lookback_days,
    )
    print(f"\nTop smart-money wallets (last {cfg.smart_money.lookback_days}d):\n")
    for i, (w, s) in enumerate(ranked[:25], 1):
        print(f"{i:2d}. {w}  pnl=${s['realised_pnl']:>10,.0f}  "
              f"winrate={s['winrate']:.1%}  trades={int(s['trades'])}")
    gamma.close(); data.close()


def scan_arb(cfg: Config) -> None:
    gamma, clob = GammaClient(), ClobClient()
    markets = gamma.fetch_markets(limit=300, min_volume_24h=1000)
    strat = ArbitrageStrategy(cfg.arbitrage, gamma, clob, markets)
    signals = strat.generate()
    if not signals:
        print("No arbitrage opportunities above threshold right now.")
    else:
        print(f"\nFound {len(signals)} arbitrage legs:\n")
        for s in signals:
            print(f"  {s.order.side.value} {s.order.token_id[:12]} "
                  f"@ {s.order.price:.3f}  — {s.rationale}")
    gamma.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="polybot — Polymarket trading system")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--live", action="store_true",
                    help="disable dry-run and place REAL orders")
    ap.add_argument("--scan-wallets", action="store_true")
    ap.add_argument("--scan-arb", action="store_true")
    args = ap.parse_args()

    cfg = Config.load(args.config)
    configure(cfg.engine.log_level)

    if args.scan_wallets:
        scan_wallets(cfg); return 0
    if args.scan_arb:
        scan_arb(cfg); return 0

    if args.live:
        if not cfg.secrets.can_trade:
            log.error("--live requires POLY_PRIVATE_KEY in the environment")
            return 2
        cfg.engine.dry_run = False
        log.warning("LIVE MODE — real orders will be placed")

    Engine(cfg).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
