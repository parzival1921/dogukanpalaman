"""Sonuçlandırma: kilitli bahsin kazanıp kazanmadığı ve kazanç tutarı."""

from __future__ import annotations

from dataclasses import dataclass

from ..models import RaceResult


@dataclass
class BetOutcome:
    status: str            # won | lost | void
    payout: float          # geri dönen toplam tutar (stake dahil)
    profit: float          # net kâr/zarar
    finish_pos: int | None
    dividend: float | None  # gerçekleşen ödeme oranı (1 TL'ye)


def place_slots(field_size: int) -> int:
    """Plase ödemesi alan at sayısı (TJK kuralı: 8+ atlı koşuda ilk 3)."""
    if field_size >= 8:
        return 3
    if field_size >= 5:
        return 2
    return 1


def evaluate(
    *,
    bet_type: str,
    number: int,
    stake: float,
    odds_at_lock: float,
    result: RaceResult,
    field_size: int,
) -> BetOutcome:
    """Bir bahsi resmi sonuca göre değerlendirir.

    Ödeme oranı olarak önce resmi temettü (ganyan/plase) kullanılır; sağlayıcı
    temettü vermiyorsa kilit anındaki oran esas alınır.
    """
    if result.cancelled or not result.finish_order:
        return BetOutcome(status="void", payout=round(stake, 2), profit=0.0, finish_pos=None, dividend=None)

    try:
        finish_pos = result.finish_order.index(number) + 1
    except ValueError:
        # At koşuya çıkmadı (koşudan çekildi) -> iade.
        return BetOutcome(status="void", payout=round(stake, 2), profit=0.0, finish_pos=None, dividend=None)

    if bet_type == "win":
        won = finish_pos == 1
        dividend = result.win_dividends.get(number) or odds_at_lock
    elif bet_type == "place":
        won = finish_pos <= place_slots(field_size)
        dividend = result.place_dividends.get(number)
        if dividend is None:
            # Plase temettüsü yoksa ganyanın kabaca üçte biri kadar varsayılır.
            dividend = max(1.05, 1.0 + (odds_at_lock - 1.0) / 3.0)
    else:
        raise ValueError(f"bilinmeyen bahis tipi: {bet_type}")

    if won:
        payout = round(stake * dividend, 2)
        return BetOutcome(
            status="won",
            payout=payout,
            profit=round(payout - stake, 2),
            finish_pos=finish_pos,
            dividend=round(dividend, 2),
        )

    return BetOutcome(
        status="lost",
        payout=0.0,
        profit=round(-stake, 2),
        finish_pos=finish_pos,
        dividend=None,
    )
