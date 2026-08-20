"""Kazanma olasılığı modeli.

Akış:
  ham özellikler -> koşu içi z-skor -> ağırlıklı toplam -> softmax -> model olasılığı
  piyasa oranından (ganyan) overround temizlenmiş piyasa olasılığı
  ikisinin harmanı -> nihai olasılık, edge ve EV
"""

from __future__ import annotations

import math

from ..config import Settings, settings as default_settings
from ..models import RaceCard, Runner, RunnerAssessment
from . import features

# Özellik ağırlıkları (z-skor uzayında).
WEIGHTS: dict[str, float] = {
    "form": 1.00,
    "rating": 0.85,
    "jockey": 0.55,
    "weight": 0.30,
    "distance": 0.45,
    "surface": 0.40,
    "freshness": 0.25,
    "draw": 0.20,
    "age": 0.15,
}


def zscores(values: list[float]) -> list[float]:
    n = len(values)
    if n == 0:
        return []
    mean = sum(values) / n
    var = sum((v - mean) ** 2 for v in values) / n
    sd = math.sqrt(var)
    if sd < 1e-9:
        return [0.0] * n
    return [(v - mean) / sd for v in values]


def _softmax(scores: list[float], temperature: float) -> list[float]:
    if not scores:
        return []
    t = max(temperature, 1e-3)
    top = max(scores)
    exps = [math.exp((s - top) / t) for s in scores]
    total = sum(exps)
    return [e / total for e in exps]


def market_probabilities(runners: list[Runner]) -> list[float]:
    """Ganyan oranlarından overround'u temizlenmiş piyasa olasılıkları.

    Oran yoksa AGF yüzdesine, o da yoksa eşit dağılıma düşer.
    """
    n = len(runners)
    if n == 0:
        return []

    raw = [1.0 / r.odds if r.odds and r.odds > 1.0 else None for r in runners]
    if any(v is not None for v in raw):
        known = [v for v in raw if v is not None]
        fallback = (sum(known) / len(known)) if known else 1.0 / n
        raw = [v if v is not None else fallback for v in raw]
    else:
        agf = [r.agf for r in runners]
        if any(a is not None and a > 0 for a in agf):
            raw = [float(a) if a else 0.5 for a in agf]
        else:
            raw = [1.0 / n] * n

    total = sum(raw)
    if total <= 0:
        return [1.0 / n] * n
    return [v / total for v in raw]


def shrink_to_market(
    probs: list[float], market: list[float], max_ratio: float
) -> list[float]:
    """Emniyet freni: modelin piyasadan ayrışmasını sınırlar.

    Sezgisel bir model tek bir bozuk özellik yüzünden bir ata piyasanın 10 katı
    olasılık biçebilir; bu da saçma EV ve fazla stake demektir. Burada model ile
    piyasa arasında logaritmik bir karışım yapılır ve karışım katsayısı, en büyük
    `p / piyasa` oranı `max_ratio`yu aşmayacak şekilde ikili aramayla küçültülür.
    Model piyasadan zaten makul ölçüde ayrılıyorsa hiçbir şey değişmez.
    """
    n = len(probs)
    if n == 0:
        return []
    ratio_cap = max(max_ratio, 1.0)

    def mix(lam: float) -> list[float]:
        vals = [
            max(p, 1e-12) ** lam * max(k, 1e-12) ** (1.0 - lam)
            for p, k in zip(probs, market)
        ]
        total = sum(vals) or 1.0
        return [v / total for v in vals]

    def worst(vals: list[float]) -> float:
        return max(v / max(k, 1e-12) for v, k in zip(vals, market))

    if worst(probs) <= ratio_cap:
        return probs

    low, high = 0.0, 1.0
    for _ in range(40):
        mid = (low + high) / 2
        if worst(mix(mid)) > ratio_cap:
            high = mid
        else:
            low = mid
    return mix(low)


def assess_race(race: RaceCard, cfg: Settings | None = None) -> list[RunnerAssessment]:
    """Koşudaki her at için model/piyasa olasılığı, edge ve EV hesaplar."""
    cfg = cfg or default_settings
    runners = race.active_runners
    if not runners:
        return []

    raw = [features.extract(r, race) for r in runners]
    z_by_feature = {name: zscores([f[name] for f in raw]) for name in WEIGHTS}

    raw_scores = [
        sum(WEIGHTS[name] * z_by_feature[name][idx] for name in WEIGHTS)
        for idx in range(len(runners))
    ]
    # Bileşik skor yeniden standartlaştırılır: aksi halde softmax'ın keskinliği
    # ağırlık toplamına ve at sayısına göre değişir ve olasılıklar aşırı sivri
    # çıkar. Standartlaştırıldığında sıcaklık (temperature) anlamlı bir kalibrasyon
    # parametresi olur; 1.1 civarı gerçek yarışlardaki dağılıma yakın sonuç verir
    # (favori ~%30, en dip at ~%2).
    scores = zscores(raw_scores)

    model_probs = _softmax(scores, cfg.softmax_temperature)
    market_probs = market_probabilities(runners)

    blend = min(max(cfg.market_blend, 0.0), 1.0)
    blended = [(1 - blend) * m + blend * k for m, k in zip(model_probs, market_probs)]
    total = sum(blended) or 1.0
    blended = [b / total for b in blended]
    blended = shrink_to_market(blended, market_probs, cfg.max_prob_ratio)

    assessments: list[RunnerAssessment] = []
    for idx, runner in enumerate(runners):
        p = blended[idx]
        odds = runner.odds
        ev = (p * odds - 1.0) if odds and odds > 1.0 else -1.0
        assessments.append(
            RunnerAssessment(
                number=runner.number,
                horse=runner.horse,
                jockey=runner.jockey,
                odds=odds,
                model_prob=p,
                market_prob=market_probs[idx],
                edge=p - market_probs[idx],
                ev=ev,
                score=raw_scores[idx],
                features={name: z_by_feature[name][idx] for name in WEIGHTS},
            )
        )

    assessments.sort(key=lambda a: a.model_prob, reverse=True)
    return assessments
