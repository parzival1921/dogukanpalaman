"""Tahmin modeli, bahis seçimi ve sonuçlandırma testleri."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from app.config import settings
from app.engine import betting, predictor, settle
from app.models import RaceCard, RaceResult, Runner


def make_race(**overrides) -> RaceCard:
    runners = [
        Runner(number=1, horse="Favori", jockey="A", handicap=88, form=[1, 1, 2, 1, 3],
               weight=58, odds=2.0, draw=1, days_since_run=21, best_distance=1600,
               surface_pref="kum", jockey_win_rate=0.28),
        Runner(number=2, horse="Orta", jockey="B", handicap=72, form=[3, 4, 2, 5, 4],
               weight=56, odds=5.0, draw=2, days_since_run=25, best_distance=1600,
               surface_pref="kum", jockey_win_rate=0.14),
        Runner(number=3, horse="Zayıf", jockey="C", handicap=55, form=[8, 7, 9, 6, 8],
               weight=54, odds=20.0, draw=3, days_since_run=60, best_distance=2400,
               surface_pref="çim", jockey_win_rate=0.04),
        Runner(number=4, horse="Sürpriz", jockey="D", handicap=80, form=[2, 1, 3, 2, 1],
               weight=55, odds=12.0, draw=4, days_since_run=24, best_distance=1600,
               surface_pref="kum", jockey_win_rate=0.21),
    ]
    return RaceCard(
        id=overrides.get("id", "r1"),
        track="Test",
        race_no=1,
        race_date="2026-08-20",
        post_time=datetime.now(timezone.utc) + timedelta(minutes=10),
        distance=1600,
        surface="kum",
        race_class="Handikap",
        runners=overrides.get("runners", runners),
    )


def test_probabilities_sum_to_one():
    race = make_race()
    assessments = predictor.assess_race(race)
    total = sum(a.model_prob for a in assessments)
    assert len(assessments) == 4
    assert total == pytest.approx(1.0, abs=1e-9)


def test_market_probabilities_strip_overround():
    runners = [Runner(number=1, horse="a", odds=2.0), Runner(number=2, horse="b", odds=2.0)]
    probs = predictor.market_probabilities(runners)
    assert sum(probs) == pytest.approx(1.0)
    assert probs[0] == pytest.approx(0.5)


def test_stronger_horse_gets_higher_probability():
    race = make_race()
    by_number = {a.number: a for a in predictor.assess_race(race)}
    assert by_number[1].model_prob > by_number[3].model_prob
    # Formu iyi ama oranı yüksek at, piyasadan daha yüksek olasılık almalı.
    assert by_number[4].model_prob > by_number[4].market_prob


def test_scratched_runners_excluded():
    race = make_race()
    race.runners[2].scratched = True
    assessments = predictor.assess_race(race)
    assert {a.number for a in assessments} == {1, 2, 4}


def test_ev_matches_definition():
    race = make_race()
    for a in predictor.assess_race(race):
        assert a.ev == pytest.approx(a.model_prob * a.odds - 1.0)


def test_kelly_zero_when_no_edge():
    # p * oran = 1 -> avantaj yok
    assert betting.kelly_fraction(0.5, 2.0) == pytest.approx(0.0)
    assert betting.kelly_fraction(0.6, 2.0) > 0
    assert betting.kelly_fraction(0.3, 2.0) == 0.0


def test_select_bets_respects_thresholds():
    race = make_race()
    assessments = predictor.assess_race(race)
    # max_ev burada yükseltiliyor: fikstürdeki 4 no'lu at bilerek çok yanlış
    # fiyatlanmış; "fazla iyi EV" filtresi ayrı bir testte doğrulanıyor.
    cfg = replace(settings, min_ev=0.05, max_ev=3.0, max_bets_per_race=3, min_stake=1.0)
    bets = betting.select_bets(race.id, assessments, 1000.0, cfg)

    assert bets, "en az bir değerli bahis bulunmalı"
    assert all(b.ev >= cfg.min_ev for b in bets)
    assert all(b.stake > 0 for b in bets)
    # EV'ye göre azalan sırada
    assert [b.ev for b in bets] == sorted([b.ev for b in bets], reverse=True)


def test_select_bets_empty_when_threshold_high():
    race = make_race()
    assessments = predictor.assess_race(race)
    cfg = replace(settings, min_ev=5.0)
    assert betting.select_bets(race.id, assessments, 1000.0, cfg) == []


def test_stake_capped_by_max_pct():
    race = make_race()
    assessments = predictor.assess_race(race)
    cfg = replace(settings, min_ev=0.0, kelly_fraction=1.0, max_stake_pct=0.02,
                  max_bets_per_race=1, min_stake=1.0)
    bets = betting.select_bets(race.id, assessments, 1000.0, cfg)
    assert bets and bets[0].stake <= 1000.0 * 0.02


def test_settle_win_uses_official_dividend():
    result = RaceResult(race_id="r1", finish_order=[4, 1, 2, 3], win_dividends={4: 11.5})
    outcome = settle.evaluate(
        bet_type="win", number=4, stake=50.0, odds_at_lock=12.0,
        result=result, field_size=4,
    )
    assert outcome.status == "won"
    assert outcome.payout == pytest.approx(575.0)
    assert outcome.profit == pytest.approx(525.0)
    assert outcome.finish_pos == 1


def test_settle_loss():
    result = RaceResult(race_id="r1", finish_order=[1, 2, 3, 4])
    outcome = settle.evaluate(
        bet_type="win", number=4, stake=50.0, odds_at_lock=12.0,
        result=result, field_size=4,
    )
    assert outcome.status == "lost"
    assert outcome.profit == pytest.approx(-50.0)
    assert outcome.finish_pos == 4


def test_settle_void_when_runner_missing_or_cancelled():
    result = RaceResult(race_id="r1", finish_order=[1, 2, 3])
    outcome = settle.evaluate(
        bet_type="win", number=9, stake=40.0, odds_at_lock=6.0,
        result=result, field_size=3,
    )
    assert outcome.status == "void"
    assert outcome.payout == pytest.approx(40.0)
    assert outcome.profit == 0.0

    cancelled = RaceResult(race_id="r1", finish_order=[], cancelled=True)
    outcome2 = settle.evaluate(
        bet_type="win", number=1, stake=40.0, odds_at_lock=6.0,
        result=cancelled, field_size=3,
    )
    assert outcome2.status == "void"


def test_place_slots_follow_field_size():
    assert settle.place_slots(12) == 3
    assert settle.place_slots(8) == 3
    assert settle.place_slots(6) == 2
    assert settle.place_slots(4) == 1


def test_place_bet_pays_on_third():
    result = RaceResult(
        race_id="r1", finish_order=[5, 2, 7, 1], place_dividends={7: 3.2}
    )
    outcome = settle.evaluate(
        bet_type="place", number=7, stake=20.0, odds_at_lock=9.0,
        result=result, field_size=9,
    )
    assert outcome.status == "won"
    assert outcome.payout == pytest.approx(64.0)


def test_probability_clamped_to_market_ratio():
    """Emniyet freni: model olasılığı piyasanınkinden max_prob_ratio kat fazla olamaz."""
    runners = [
        Runner(number=1, horse="Uçuk oran", handicap=95, form=[1, 1, 1, 1, 1],
               weight=50, odds=40.0, jockey_win_rate=0.35, days_since_run=21),
        Runner(number=2, horse="Favori", handicap=50, form=[6, 7, 8, 6, 7],
               weight=60, odds=1.2, jockey_win_rate=0.03, days_since_run=21),
        Runner(number=3, horse="Diğer", handicap=52, form=[5, 6, 5, 7, 6],
               weight=59, odds=8.0, jockey_win_rate=0.05, days_since_run=21),
    ]
    race = make_race(runners=runners)
    cfg = replace(settings, max_prob_ratio=2.0, market_blend=0.0)
    by_number = {a.number: a for a in predictor.assess_race(race, cfg)}

    for a in by_number.values():
        assert a.model_prob <= a.market_prob * 2.0 + 1e-6
    assert sum(a.model_prob for a in by_number.values()) == pytest.approx(1.0)
    # Uçuk oranlı at yine de piyasadan yüksek değerlendirilmeli (yön korunur).
    assert by_number[1].model_prob > by_number[1].market_prob


def test_shrink_is_noop_when_model_is_close_to_market():
    market = [0.5, 0.3, 0.2]
    probs = [0.55, 0.28, 0.17]
    assert predictor.shrink_to_market(probs, market, 3.0) == probs


def test_absurd_ev_is_rejected_as_data_error():
    """Oran bayat/yanlış geldiğinde oluşan uçuk EV oynanmamalı."""
    race = make_race()
    assessments = predictor.assess_race(race)
    top = max(assessments, key=lambda a: a.ev)
    cfg = replace(settings, min_ev=0.0, max_ev=0.0001, min_stake=1.0)
    assert all(b.number != top.number for b in betting.select_bets(race.id, assessments, 1000.0, cfg))
