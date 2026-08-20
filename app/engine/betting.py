"""Bahis seçimi ve stake hesabı (kısmi Kelly).

"En karlı tahmin" = en yüksek olasılık değil, en yüksek beklenen değer (EV).
EV = p * oran - 1  (1 TL'lik bahis başına beklenen kâr)
"""

from __future__ import annotations

from ..config import Settings, settings as default_settings
from ..models import BetProposal, RunnerAssessment


def kelly_fraction(prob: float, odds: float) -> float:
    """Tam Kelly oranı: (p*b - q) / b, b = oran - 1."""
    b = odds - 1.0
    if b <= 0:
        return 0.0
    q = 1.0 - prob
    return max(0.0, (prob * b - q) / b)


def _round_stake(amount: float, step: float) -> float:
    if step <= 0:
        return round(amount, 2)
    return round(round(amount / step) * step, 2)


def select_bets(
    race_id: str,
    assessments: list[RunnerAssessment],
    bankroll: float,
    cfg: Settings | None = None,
) -> list[BetProposal]:
    """EV eşiğini geçen atları kârlılığa göre sıralayıp stake atar."""
    cfg = cfg or default_settings
    candidates: list[BetProposal] = []

    for a in assessments:
        if a.odds is None or a.odds < cfg.min_odds or a.odds > cfg.max_odds:
            continue
        if a.model_prob < cfg.min_probability:
            continue
        if a.ev < cfg.min_ev:
            continue
        if a.ev > cfg.max_ev:
            # "Fazla iyi" EV neredeyse her zaman veri hatasıdır (bayat/yanlış
            # ayrıştırılmış oran); gerçek bir avantaj değil. Oynanmaz.
            continue

        k = kelly_fraction(a.model_prob, a.odds)
        if k <= 0:
            continue

        stake_pct = min(k * cfg.kelly_fraction, cfg.max_stake_pct)
        stake = _round_stake(bankroll * stake_pct, cfg.stake_rounding)
        if stake < cfg.min_stake:
            continue

        candidates.append(
            BetProposal(
                race_id=race_id,
                number=a.number,
                horse=a.horse,
                bet_type="win",
                odds=a.odds,
                model_prob=a.model_prob,
                market_prob=a.market_prob,
                edge=a.edge,
                ev=a.ev,
                kelly=k,
                stake=stake,
            )
        )

    candidates.sort(key=lambda b: b.ev, reverse=True)
    selected = candidates[: max(cfg.max_bets_per_race, 0)]

    # Koşu başına toplam risk tavanı: bankroll'un max_stake_pct * 1.5 katı.
    cap = bankroll * cfg.max_stake_pct * 1.5
    total = sum(b.stake for b in selected)
    if total > cap and total > 0:
        scale = cap / total
        rescaled = []
        for b in selected:
            b.stake = _round_stake(b.stake * scale, cfg.stake_rounding)
            if b.stake >= cfg.min_stake:
                rescaled.append(b)
        selected = rescaled

    # Bankroll'dan fazlasını riske atma.
    available = bankroll
    final: list[BetProposal] = []
    for b in selected:
        if b.stake <= available:
            final.append(b)
            available -= b.stake
    return final
