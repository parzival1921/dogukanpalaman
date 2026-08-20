"""Uygulama servisleri: senkronizasyon, kilitleme, sonuçlandırma, dashboard."""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import date, datetime, timedelta, timezone
from typing import Any

from . import db
from .config import Settings, settings as default_settings
from .engine import betting, predictor, settle as settle_engine
from .models import RaceCard, RaceResult, Runner, RunnerAssessment
from .providers.base import RaceProvider

log = logging.getLogger("atyarisi")


# --------------------------------------------------------------------------
# dönüşümler
# --------------------------------------------------------------------------

def race_to_row(race: RaceCard) -> dict[str, Any]:
    return {
        "id": race.id,
        "track": race.track,
        "race_no": race.race_no,
        "race_date": race.race_date,
        "post_time": db.iso(race.post_time),
        "distance": race.distance,
        "surface": race.surface,
        "race_class": race.race_class,
        "purse": race.purse,
        "runners_json": json.dumps([r.to_dict() for r in race.runners], ensure_ascii=False),
    }


def row_to_race(row: sqlite3.Row) -> RaceCard:
    return RaceCard(
        id=row["id"],
        track=row["track"],
        race_no=row["race_no"],
        race_date=row["race_date"],
        post_time=db.parse_iso(row["post_time"]),
        distance=row["distance"],
        surface=row["surface"],
        race_class=row["race_class"],
        purse=row["purse"],
        runners=[Runner.from_dict(d) for d in json.loads(row["runners_json"] or "[]")],
    )


# --------------------------------------------------------------------------
# senkronizasyon
# --------------------------------------------------------------------------

async def sync_races(provider: RaceProvider, days: int = 2, cfg: Settings | None = None) -> int:
    """Bugün ve (varsayılan) yarın için koşu programını çeker."""
    cfg = cfg or default_settings
    today = datetime.now(timezone.utc).date()
    count = 0
    for offset in range(days):
        day = today + timedelta(days=offset)
        try:
            cards = await provider.fetch_race_cards(day)
        except Exception as exc:  # sağlayıcı hatası döngüyü durdurmasın
            log.warning("program çekilemedi (%s): %s", day, exc)
            continue
        for card in cards:
            existing = db.get_race(card.id)
            if existing and existing["status"] in ("finished", "cancelled"):
                continue
            db.upsert_race(race_to_row(card))
            count += 1
    return count


async def refresh_odds(provider: RaceProvider, cfg: Settings | None = None) -> int:
    """Yaklaşan koşuların ganyan oranlarını günceller."""
    cfg = cfg or default_settings
    now = datetime.now(timezone.utc)
    horizon = now + timedelta(hours=cfg.lookahead_hours)
    updated = 0

    for row in db.list_races(after=now - timedelta(minutes=1), before=horizon):
        if row["status"] in ("finished", "cancelled"):
            continue
        race = row_to_race(row)
        try:
            quotes = await provider.fetch_odds(race)
        except Exception as exc:
            log.warning("oran çekilemedi (%s): %s", race.id, exc)
            continue
        if not quotes:
            continue
        for runner in race.runners:
            if runner.number in quotes:
                runner.odds = float(quotes[runner.number])
        db.upsert_race(race_to_row(race))
        db.record_odds(race.id, {int(k): float(v) for k, v in quotes.items()})
        updated += 1
    return updated


# --------------------------------------------------------------------------
# tahmin + kilitleme
# --------------------------------------------------------------------------

def assess(race: RaceCard, cfg: Settings | None = None) -> list[RunnerAssessment]:
    return predictor.assess_race(race, cfg)


def preview(race: RaceCard, cfg: Settings | None = None) -> dict[str, Any]:
    """Kilitlenmemiş koşu için canlı (henüz bağlayıcı olmayan) tahmin."""
    cfg = cfg or default_settings
    assessments = assess(race, cfg)
    proposals = betting.select_bets(race.id, assessments, db.bankroll(), cfg)
    return {
        "assessments": [a.to_dict() for a in assessments],
        "bets": [b.to_dict() for b in proposals],
    }


def lock_due_predictions(cfg: Settings | None = None, now: datetime | None = None) -> list[int]:
    """Start'a `LOCK_MINUTES` kalan koşuların tahminini kilitler.

    Kilit penceresi: 0 < (start - şimdi) <= LOCK_MINUTES.
    Kilitlenen tahmin bir daha güncellenmez; stake anında bankroll'dan düşülür.
    """
    cfg = cfg or default_settings
    now = now or datetime.now(timezone.utc)
    window_end = now + timedelta(minutes=cfg.lock_minutes)
    locked: list[int] = []

    for row in db.list_races(after=now, before=window_end):
        if row["status"] != "scheduled" or db.has_prediction(row["id"]):
            continue

        race = row_to_race(row)
        if not race.active_runners:
            continue

        bankroll = db.bankroll()
        assessments = assess(race, cfg)
        proposals = betting.select_bets(race.id, assessments, bankroll, cfg)

        prediction_id = db.insert_prediction(
            race_id=race.id,
            locked_at=now,
            post_time=race.post_time,
            bankroll=bankroll,
            assessments=[a.to_dict() for a in assessments],
            bets=[b.to_dict() for b in proposals],
        )
        db.set_race_status(race.id, "locked")

        total_stake = sum(b.stake for b in proposals)
        if total_stake > 0:
            db.add_ledger(
                "bet_placed",
                -total_stake,
                race_id=race.id,
                note=f"{race.track} {race.race_no}. koşu · {len(proposals)} bahis",
            )
        locked.append(prediction_id)
        log.info(
            "tahmin kilitlendi: %s (%s %s. koşu) · %d bahis · %.2f TL",
            race.id, race.track, race.race_no, len(proposals), total_stake,
        )
    return locked


# --------------------------------------------------------------------------
# sonuçlandırma
# --------------------------------------------------------------------------

async def settle_finished(provider: RaceProvider, cfg: Settings | None = None) -> list[str]:
    """Biten koşuların sonucunu çekip bahisleri kapatır."""
    cfg = cfg or default_settings
    now = datetime.now(timezone.utc)
    settled: list[str] = []

    for pred in db.list_pending_predictions():
        post_time = db.parse_iso(pred["post_time"])
        if now < post_time + timedelta(minutes=cfg.settle_delay_minutes):
            continue

        row = db.get_race(pred["race_id"])
        if row is None:
            continue
        race = row_to_race(row)
        try:
            result = await provider.fetch_result(race)
        except Exception as exc:
            log.warning("sonuç çekilemedi (%s): %s", race.id, exc)
            continue
        if result is None:
            continue

        apply_result(race, result, pred)
        settled.append(race.id)
    return settled


def apply_result(race: RaceCard, result: RaceResult, prediction: sqlite3.Row) -> float:
    """Sonucu kaydeder, bahisleri kapatır, bankroll'u günceller. Net kârı döndürür."""
    db.save_race_result(
        race.id,
        result.to_dict(),
        "cancelled" if result.cancelled else "finished",
    )

    field_size = len(race.active_runners)
    total_profit = 0.0
    total_payout = 0.0

    for bet in db.list_bets(race_id=race.id, status="pending"):
        outcome = settle_engine.evaluate(
            bet_type=bet["bet_type"],
            number=bet["number"],
            stake=float(bet["stake"]),
            odds_at_lock=float(bet["odds_at_lock"]),
            result=result,
            field_size=field_size,
        )
        db.settle_bet(
            bet["id"],
            status=outcome.status,
            payout=outcome.payout,
            profit=outcome.profit,
            finish_pos=outcome.finish_pos,
            dividend=outcome.dividend,
        )
        total_profit += outcome.profit
        total_payout += outcome.payout

    if total_payout > 0:
        db.add_ledger(
            "payout",
            total_payout,
            race_id=race.id,
            note=f"{race.track} {race.race_no}. koşu ödemesi",
        )

    db.close_prediction(
        int(prediction["id"]),
        status="settled",
        profit=round(total_profit, 2),
    )
    log.info("koşu sonuçlandı: %s · net %.2f TL", race.id, total_profit)
    return round(total_profit, 2)


# --------------------------------------------------------------------------
# dashboard verisi
# --------------------------------------------------------------------------

def _fmt_race(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "track": row["track"],
        "race_no": row["race_no"],
        "race_date": row["race_date"],
        "post_time": row["post_time"],
        "distance": row["distance"],
        "surface": row["surface"],
        "race_class": row["race_class"],
        "status": row["status"],
    }


def no_bet_reason(assessments: list[dict[str, Any]], cfg: Settings) -> str:
    """Neden bahis oynanmadığını tek cümlede açıklar."""
    evs = [float(a.get("ev", -1.0)) for a in assessments]
    if not evs:
        return "Değerlendirilecek at yok."
    best = max(evs)
    if best > cfg.max_ev:
        return (
            f"En yüksek EV %{best * 100:.0f} — üst eşiğin (%{cfg.max_ev * 100:.0f}) "
            "üstünde, veri şüpheli sayılıp oynanmadı."
        )
    if best < cfg.min_ev:
        return (
            f"En yüksek EV %{best * 100:.0f} — alt eşiğin (%{cfg.min_ev * 100:.0f}) "
            "altında, değer yok."
        )
    return "Oran/olasılık filtreleri veya asgari stake nedeniyle bahis açılmadı."


def upcoming_races(limit: int = 8, cfg: Settings | None = None) -> list[dict[str, Any]]:
    """Yaklaşan koşular + canlı ya da kilitli tahmin."""
    cfg = cfg or default_settings
    now = datetime.now(timezone.utc)
    horizon = now + timedelta(hours=cfg.lookahead_hours)
    output: list[dict[str, Any]] = []

    for row in db.list_races(after=now, before=horizon):
        if row["status"] in ("finished", "cancelled"):
            continue
        race = row_to_race(row)
        item = _fmt_race(row)
        item["seconds_to_post"] = (race.post_time - now).total_seconds()
        item["seconds_to_lock"] = item["seconds_to_post"] - cfg.lock_minutes * 60
        item["field_size"] = len(race.active_runners)

        pred = db.get_prediction(race.id)
        if pred:
            item["locked"] = True
            item["locked_at"] = pred["locked_at"]
            assessments = json.loads(pred["assessments_json"])
            item["bets"] = [_fmt_bet(b) for b in db.list_bets(race_id=race.id)]
        else:
            item["locked"] = False
            live = preview(race, cfg)
            assessments = live["assessments"]
            item["bets"] = live["bets"]

        item["skip_reason"] = None if item["bets"] else no_bet_reason(assessments, cfg)
        item["assessments"] = assessments[:5]
        output.append(item)
        if len(output) >= limit:
            break
    return output


def _fmt_bet(row: sqlite3.Row) -> dict[str, Any]:
    keys = [
        "id", "race_id", "number", "horse", "bet_type", "odds_at_lock", "model_prob",
        "market_prob", "edge", "ev", "stake", "status", "payout", "profit",
        "finish_pos", "dividend", "settled_at",
    ]
    data = {k: row[k] for k in keys if k in row.keys()}
    for extra in ("track", "race_no", "post_time", "race_date"):
        if extra in row.keys():
            data[extra] = row[extra]
    return data


def settled_results(limit: int = 40) -> list[dict[str, Any]]:
    """Sonuçlanmış tahminler: kazandı/kaybetti ve kazanılan tutar."""
    output = []
    for pred in db.list_predictions(status="settled", limit=limit):
        result = json.loads(pred["result_json"]) if pred["result_json"] else {}
        bets = [_fmt_bet(b) for b in db.list_bets(race_id=pred["race_id"])]
        output.append(
            {
                "race_id": pred["race_id"],
                "track": pred["track"],
                "race_no": pred["race_no"],
                "race_date": pred["race_date"],
                "post_time": pred["post_time"],
                "locked_at": pred["locked_at"],
                "settled_at": pred["settled_at"],
                "profit": pred["profit"],
                "won": any(b["status"] == "won" for b in bets),
                "finish_order": result.get("finish_order", []),
                "bets": bets,
            }
        )
    return output


def stats(cfg: Settings | None = None) -> dict[str, Any]:
    cfg = cfg or default_settings
    bets = db.list_bets(limit=10_000)
    settled = [b for b in bets if b["status"] in ("won", "lost")]
    pending = [b for b in bets if b["status"] == "pending"]

    staked = sum(float(b["stake"]) for b in settled)
    profit = sum(float(b["profit"]) for b in settled)
    wins = sum(1 for b in settled if b["status"] == "won")

    ledger = list(reversed(db.list_ledger(limit=1000)))
    curve = [{"ts": r["ts"], "balance": r["balance"]} for r in ledger]
    if not curve:
        curve = [{"ts": db.iso(db.utcnow()), "balance": cfg.bankroll_start}]

    return {
        "bankroll": db.bankroll(),
        "bankroll_start": cfg.bankroll_start,
        "total_profit": round(profit, 2),
        "total_staked": round(staked, 2),
        "roi": round(profit / staked * 100, 2) if staked else 0.0,
        "bet_count": len(settled),
        "win_count": wins,
        "hit_rate": round(wins / len(settled) * 100, 2) if settled else 0.0,
        "pending_count": len(pending),
        "pending_stake": round(sum(float(b["stake"]) for b in pending), 2),
        "curve": curve,
    }


def dashboard(cfg: Settings | None = None) -> dict[str, Any]:
    cfg = cfg or default_settings
    return {
        "now": db.iso(db.utcnow()),
        "timezone": cfg.timezone,
        "provider": cfg.provider,
        "lock_minutes": cfg.lock_minutes,
        "settings": {
            "min_ev": cfg.min_ev,
            "max_ev": cfg.max_ev,
            "kelly_fraction": cfg.kelly_fraction,
            "market_blend": cfg.market_blend,
            "max_bets_per_race": cfg.max_bets_per_race,
            "max_stake_pct": cfg.max_stake_pct,
        },
        "stats": stats(cfg),
        "upcoming": upcoming_races(cfg=cfg),
        "results": settled_results(),
    }
