"""Uçtan uca akış: program çekme → 5 dk kala kilitleme → sonuçlandırma → kasa."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from app import db, service
from app.config import settings
from app.providers.demo import DemoProvider


def cfg_for(**overrides):
    base = replace(
        settings,
        demo_first_post_minutes=3.0,
        demo_race_interval_minutes=30.0,
        demo_races_per_day=4,
        lock_minutes=5.0,
        min_ev=0.02,
        min_stake=1.0,
    )
    return replace(base, **overrides) if overrides else base


def make_provider(cfg, anchor_offset_minutes: float = 0.0) -> DemoProvider:
    anchor = datetime.now(timezone.utc) + timedelta(minutes=anchor_offset_minutes)
    return DemoProvider(anchor, cfg)


async def prepare(cfg, provider) -> None:
    await service.sync_races(provider, days=1, cfg=cfg)
    await service.refresh_odds(provider, cfg=cfg)


def test_sync_stores_races_and_odds():
    cfg = cfg_for()
    provider = make_provider(cfg)
    asyncio.run(prepare(cfg, provider))

    races = db.list_races()
    assert len(races) == cfg.demo_races_per_day

    race = service.row_to_race(races[0])
    assert race.active_runners
    assert all(r.odds and r.odds > 1.0 for r in race.active_runners)


def test_lock_only_inside_five_minute_window():
    cfg = cfg_for()
    provider = make_provider(cfg)
    asyncio.run(prepare(cfg, provider))

    locked = service.lock_due_predictions(cfg=cfg)
    assert len(locked) == 1, "yalnızca start'a 3 dk kalan koşu kilitlenmeli"

    rows = db.list_races()
    first = rows[0]
    assert first["status"] == "locked"
    assert db.has_prediction(first["id"])
    # Sonraki koşular (30+ dk sonra) kilitlenmemeli.
    assert not db.has_prediction(rows[1]["id"])


def test_lock_is_idempotent_and_frozen_against_odds_moves():
    cfg = cfg_for()
    provider = make_provider(cfg)
    asyncio.run(prepare(cfg, provider))
    service.lock_due_predictions(cfg=cfg)

    row = db.list_races()[0]
    prediction = db.get_prediction(row["id"])
    bets_before = [dict(b) for b in db.list_bets(race_id=row["id"])]

    # Oranlar kilitten sonra sert şekilde değişsin.
    race = service.row_to_race(row)
    for runner in race.runners:
        runner.odds = 1.9
    db.upsert_race(service.race_to_row(race))

    assert service.lock_due_predictions(cfg=cfg) == []
    after = db.get_prediction(row["id"])
    assert after["assessments_json"] == prediction["assessments_json"]
    assert [dict(b) for b in db.list_bets(race_id=row["id"])] == bets_before


def test_locked_prediction_cannot_be_rewritten():
    cfg = cfg_for()
    provider = make_provider(cfg)
    asyncio.run(prepare(cfg, provider))
    service.lock_due_predictions(cfg=cfg)
    row = db.list_races()[0]

    with pytest.raises(sqlite3.IntegrityError):
        with db.cursor() as cur:
            cur.execute(
                "UPDATE predictions SET assessments_json = '[]' WHERE race_id = ?",
                (row["id"],),
            )

    bets = db.list_bets(race_id=row["id"])
    if bets:
        with pytest.raises(sqlite3.IntegrityError):
            with db.cursor() as cur:
                cur.execute("UPDATE bets SET stake = stake + 100 WHERE id = ?", (bets[0]["id"],))


def test_stake_is_debited_at_lock():
    cfg = cfg_for()
    provider = make_provider(cfg)
    asyncio.run(prepare(cfg, provider))
    start = db.bankroll()
    service.lock_due_predictions(cfg=cfg)

    row = db.list_races()[0]
    staked = sum(float(b["stake"]) for b in db.list_bets(race_id=row["id"]))
    assert db.bankroll() == pytest.approx(round(start - staked, 2))


def test_settlement_pays_out_and_records_outcome():
    cfg = cfg_for()
    provider = make_provider(cfg)
    asyncio.run(prepare(cfg, provider))
    start = db.bankroll()
    service.lock_due_predictions(cfg=cfg)

    row = db.list_races()[0]
    race = service.row_to_race(row)
    bets = db.list_bets(race_id=race.id)
    staked = sum(float(b["stake"]) for b in bets)

    result = provider.result_for(race)
    prediction = db.get_prediction(race.id)
    profit = service.apply_result(race, result, prediction)

    settled = db.list_bets(race_id=race.id)
    assert all(b["status"] in ("won", "lost", "void") for b in settled)
    assert db.get_race(race.id)["status"] == "finished"
    assert db.get_prediction(race.id)["status"] == "settled"

    winner = result.finish_order[0]
    for bet in settled:
        expected = "won" if bet["number"] == winner else "lost"
        assert bet["status"] == expected
        if expected == "won":
            assert bet["profit"] > 0
            assert bet["finish_pos"] == 1
        else:
            assert bet["profit"] == pytest.approx(-float(bet["stake"]))

    assert db.bankroll() == pytest.approx(round(start - staked + sum(float(b["payout"]) for b in settled), 2))
    assert profit == pytest.approx(sum(float(b["profit"]) for b in settled))


def test_dashboard_payload_shape():
    cfg = cfg_for()
    provider = make_provider(cfg)
    asyncio.run(prepare(cfg, provider))
    service.lock_due_predictions(cfg=cfg)

    row = db.list_races()[0]
    race = service.row_to_race(row)
    service.apply_result(race, provider.result_for(race), db.get_prediction(race.id))

    data = service.dashboard(cfg)
    assert set(data) >= {"now", "stats", "upcoming", "results", "lock_minutes"}
    assert data["stats"]["bet_count"] >= 0
    assert data["results"], "sonuçlanan koşu listede görünmeli"

    first = data["results"][0]
    assert first["race_id"] == race.id
    assert isinstance(first["won"], bool)
    assert first["finish_order"]

    for item in data["upcoming"]:
        assert "seconds_to_post" in item and "assessments" in item


def test_results_are_deterministic_for_same_race():
    cfg = cfg_for()
    provider = make_provider(cfg)
    asyncio.run(prepare(cfg, provider))
    race = service.row_to_race(db.list_races()[0])

    first = provider.result_for(race)
    second = make_provider(cfg, 0.0)
    # aynı çıpa değil -> farklı koşu kimliği; aynı sağlayıcıda tekrar üretim aynı olmalı
    assert provider.result_for(race).finish_order == first.finish_order
    assert second is not provider


def test_full_scheduler_tick_locks_and_settles():
    """Zamanlayıcı adımı: geçmişte kalan koşuları sonuçlandırır, yaklaşanı kilitler."""
    from app.scheduler import Scheduler

    cfg = cfg_for(demo_first_post_minutes=3.0, demo_race_interval_minutes=6.0,
                  demo_races_per_day=3, settle_delay_minutes=0.0)
    provider = make_provider(cfg)
    scheduler = Scheduler(cfg=cfg, provider=provider)

    summary = asyncio.run(scheduler.tick())
    assert summary["races"] == 3
    assert summary["locked"] == 1
    assert scheduler.last_error is None

    row = db.list_races()[0]
    assert db.has_prediction(row["id"])
    assert json.loads(db.get_prediction(row["id"])["assessments_json"])


def test_no_bet_reason_explains_filters():
    cfg = cfg_for(min_ev=0.10, max_ev=0.50)
    assert "üst eşiğin" in service.no_bet_reason([{"ev": 1.4}], cfg)
    assert "alt eşiğin" in service.no_bet_reason([{"ev": 0.02}], cfg)
    assert service.no_bet_reason([], cfg) == "Değerlendirilecek at yok."


def test_upcoming_includes_skip_reason_when_no_bet():
    cfg = cfg_for(min_ev=9.0)   # hiçbir bahis eşiği geçemez
    provider = make_provider(cfg)
    asyncio.run(prepare(cfg, provider))

    upcoming = service.upcoming_races(cfg=cfg)
    assert upcoming
    assert all(item["bets"] == [] for item in upcoming)
    assert all(item["skip_reason"] for item in upcoming)


def test_model_beats_the_simulated_market():
    """Regresyon koruması: model, simülasyondaki halkın algısından daha iyi tahmin etmeli.

    Demo pazarı, halkın niteliklerin bir kısmını (son form, jokey) fazla, koşu
    koşullarına uyumu ise neredeyse hiç dikkate almaması üzerine kuruludur.
    Modelin Brier skoru piyasanınkinin altında kalmazsa değer üretemiyor demektir.
    """
    from app.engine import predictor

    cfg = cfg_for(demo_races_per_day=150, demo_race_interval_minutes=4.0)
    provider = make_provider(cfg)

    model_brier = market_brier = 0.0
    graded = 0
    for race_no in range(1, cfg.demo_races_per_day + 1):
        race = provider._build_race(race_no)
        quotes = provider._odds_at(race, race.post_time)
        for runner in race.runners:
            runner.odds = quotes.get(runner.number)

        assessments = predictor.assess_race(race, cfg)
        if not assessments:
            continue
        winner = provider.result_for(race).finish_order[0]
        for a in assessments:
            actual = 1.0 if a.number == winner else 0.0
            model_brier += (a.model_prob - actual) ** 2
            market_brier += (a.market_prob - actual) ** 2
            graded += 1

    assert graded > 800
    assert model_brier < market_brier
