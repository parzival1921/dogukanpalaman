"""Stratejiyi demo simülatöründe geriye dönük test eder.

Kullanım:
    python -m tools.backtest [koşu_sayısı] [gün_sayısı]

Her koşu için start anındaki oranlarla tahmin üretilir, bahisler seçilir ve
gerçek sonuca göre kapatılır — canlı akışın aynısı, sadece hızlandırılmış.
Çıktı: bahis sayısı, isabet oranı, ROI, kasa ve model/piyasa Brier skoru
(düşük olan daha iyi tahmin ediyor demektir).

Tüm ayarlar ortam değişkenleriyle değiştirilebilir, örneğin:
    MARKET_BLEND=0.6 MIN_EV=0.15 python -m tools.backtest 400 5
"""

from __future__ import annotations

import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from app.config import settings
from app.engine import betting, predictor
from app.engine import settle as settle_engine
from app.providers.demo import DemoProvider


def main(races_per_day: int = 200, days: int = 4) -> int:
    cfg = replace(
        settings,
        demo_races_per_day=races_per_day,
        demo_race_interval_minutes=4.0,
    )

    bankroll = cfg.bankroll_start
    staked = profit = 0.0
    bets_placed = wins = 0
    model_brier = market_brier = 0.0
    graded = 0
    skipped_high_ev = 0

    for day in range(days):
        provider = DemoProvider(
            datetime.now(timezone.utc) + timedelta(days=day * 9),
            replace(cfg, demo_seed=cfg.demo_seed + day * 7),
        )
        for race_no in range(1, races_per_day + 1):
            race = provider._build_race(race_no)
            quotes = provider._odds_at(race, race.post_time)
            for runner in race.runners:
                runner.odds = quotes.get(runner.number)

            assessments = predictor.assess_race(race, cfg)
            if not assessments:
                continue

            result = provider.result_for(race)
            winner = result.finish_order[0]
            for a in assessments:
                actual = 1.0 if a.number == winner else 0.0
                model_brier += (a.model_prob - actual) ** 2
                market_brier += (a.market_prob - actual) ** 2
                graded += 1

            proposals = betting.select_bets(race.id, assessments, bankroll, cfg)
            if not proposals and max(a.ev for a in assessments) > cfg.max_ev:
                skipped_high_ev += 1

            for bet in proposals:
                outcome = settle_engine.evaluate(
                    bet_type=bet.bet_type,
                    number=bet.number,
                    stake=bet.stake,
                    odds_at_lock=bet.odds,
                    result=result,
                    field_size=len(race.active_runners),
                )
                staked += bet.stake
                profit += outcome.profit
                bankroll += outcome.profit
                bets_placed += 1
                wins += outcome.status == "won"

    races_total = races_per_day * days
    print(f"koşu               : {races_total}")
    print(f"bahis              : {bets_placed}  (koşu başına {bets_placed / races_total:.2f})")
    print(f"isabet             : %{100 * wins / max(bets_placed, 1):.1f}  ({wins}/{bets_placed})")
    print(f"oynanan / kâr      : {staked:,.0f} TL / {profit:+,.0f} TL")
    print(f"ROI                : %{100 * profit / max(staked, 1):+.1f}")
    print(f"kasa               : {cfg.bankroll_start:,.0f} → {bankroll:,.0f} TL")
    print(f"Brier model/piyasa : {model_brier / max(graded, 1):.4f} / {market_brier / max(graded, 1):.4f}")
    print(f"MAX_EV ile elenen  : {skipped_high_ev} koşu")
    return 0


if __name__ == "__main__":
    args = [int(x) for x in sys.argv[1:3]]
    raise SystemExit(main(*args) if args else main())
