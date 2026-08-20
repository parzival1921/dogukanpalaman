"""Stratejiyi geriye dönük test eder — simülasyonda ya da gerçek veri setinde.

Kullanım:
    python -m tools.backtest [koşu_sayısı] [gün_sayısı]     # demo simülatörü
    python -m tools.backtest --dataset dataset.json         # gerçek veri
                                                            # (bkz. tools.build_dataset)

Her koşu için tahmin üretilir, bahisler seçilir ve gerçek sonuca göre kapatılır —
canlı akışın aynısı, sadece hızlandırılmış. Çıktı: bahis sayısı, isabet oranı, ROI,
kasa ve model/piyasa **Brier skoru** (düşük olan daha iyi tahmin ediyor demektir).
Model piyasayı yenemiyorsa ROI'nin pozitif çıkması şanstır.

Tüm ayarlar ortam değişkenleriyle değiştirilebilir:
    MARKET_BLEND=0.6 MIN_EV=0.15 python -m tools.backtest 400 5
"""

from __future__ import annotations

import json
import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Iterator

from app.config import Settings, settings
from app.engine import betting, predictor
from app.engine import settle as settle_engine
from app.models import RaceCard, RaceResult
from app.providers.demo import DemoProvider
from app.providers.nalsesleri import parse_history, reconstruct_races

RacePair = tuple[RaceCard, RaceResult]


# Sabit çıpa: simülatörün koşu kimlikleri (dolayısıyla oran gürültüsü ve sonuçlar)
# çıpadan türer. `datetime.now()` kullanılırsa her çalıştırma farklı bir örneklem
# üretir ve backtest tekrarlanamaz olur.
BACKTEST_EPOCH = datetime(2026, 1, 1, tzinfo=timezone.utc)


def demo_races(cfg: Settings, races_per_day: int, days: int) -> Iterator[RacePair]:
    """Simülatörden koşu + sonuç çiftleri üretir (aynı tohum -> aynı sonuç)."""
    for day in range(days):
        provider = DemoProvider(
            BACKTEST_EPOCH + timedelta(days=day * 9),
            replace(cfg, demo_seed=cfg.demo_seed + day * 7),
        )
        for race_no in range(1, races_per_day + 1):
            race = provider._build_race(race_no)
            quotes = provider._odds_at(race, race.post_time)
            for runner in race.runners:
                runner.odds = quotes.get(runner.number)
            yield race, provider.result_for(race)


def dataset_races(path: str, min_field: int = 5) -> Iterator[RacePair]:
    """tools.build_dataset ile kaydedilmiş gerçek veriden koşuları yeniden kurar."""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    histories = {
        name: parse_history({"status": "success", "data": rows})
        for name, rows in (payload.get("horses") or {}).items()
    }
    yield from reconstruct_races(histories, min_field_size=min_field)


def evaluate(pairs: Iterable[RacePair], cfg: Settings) -> dict[str, float]:
    bankroll = cfg.bankroll_start
    staked = profit = 0.0
    bets_placed = wins = races_total = 0
    model_brier = market_brier = 0.0
    graded = skipped_high_ev = 0

    for race, result in pairs:
        races_total += 1
        assessments = predictor.assess_race(race, cfg)
        if not assessments or not result.finish_order:
            continue

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

    return {
        "races": races_total, "bets": bets_placed, "wins": wins, "staked": staked,
        "profit": profit, "bankroll": bankroll, "graded": graded,
        "model_brier": model_brier / max(graded, 1),
        "market_brier": market_brier / max(graded, 1),
        "skipped_high_ev": skipped_high_ev,
    }


def report(stats: dict[str, float], cfg: Settings) -> None:
    races = max(stats["races"], 1)
    bets = max(stats["bets"], 1)
    print(f"koşu               : {stats['races']:,}")
    print(f"bahis              : {stats['bets']:,}  (koşu başına {stats['bets'] / races:.2f})")
    print(f"isabet             : %{100 * stats['wins'] / bets:.1f}  ({stats['wins']}/{stats['bets']})")
    print(f"oynanan / kâr      : {stats['staked']:,.0f} TL / {stats['profit']:+,.0f} TL")
    print(f"ROI                : %{100 * stats['profit'] / max(stats['staked'], 1):+.1f}")
    print(f"kasa               : {cfg.bankroll_start:,.0f} → {stats['bankroll']:,.0f} TL")
    print(f"Brier model/piyasa : {stats['model_brier']:.4f} / {stats['market_brier']:.4f}"
          f"  ({'model önde' if stats['model_brier'] < stats['market_brier'] else 'PİYASA ÖNDE — model değer üretmiyor'})")
    print(f"MAX_EV ile elenen  : {stats['skipped_high_ev']} koşu")


def main(argv: list[str]) -> int:
    if "--dataset" in argv:
        path = argv[argv.index("--dataset") + 1]
        min_field = int(argv[argv.index("--min-field") + 1]) if "--min-field" in argv else 5
        pairs = list(dataset_races(path, min_field))
        if not pairs:
            print("veri setinden koşu kurulamadı — tools.build_dataset çıktısını kontrol edin")
            return 1
        print(f"→ gerçek veri: {len(pairs)} koşu ({path})")
        print("  not: piyasa olasılığı koşu sonu ganyanından hesaplanır; canlı akışta")
        print("       kilit anındaki (T-5dk) oran biraz farklı olur.")
        report(evaluate(pairs, settings), settings)
        return 0

    positional = [int(a) for a in argv if a.isdigit()]
    races_per_day = positional[0] if positional else 200
    days = positional[1] if len(positional) > 1 else 4
    cfg = replace(settings, demo_races_per_day=races_per_day, demo_race_interval_minutes=4.0)
    print(f"→ simülasyon: {races_per_day * days} koşu")
    report(evaluate(demo_races(cfg, races_per_day, days), cfg), cfg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
