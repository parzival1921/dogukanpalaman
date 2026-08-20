"""Koşan atlardan sayısal özellik (feature) çıkarımı.

Her özellik koşu içinde göreli olarak yorumlanır; ham değerler `predictor` içinde
z-skora çevrilip ağırlıklandırılır.
"""

from __future__ import annotations

from ..models import RaceCard, Runner

# Son koşu derecelerinin puanlanması (1. = 1.0, geriye doğru azalır).
_FINISH_POINTS = {1: 1.00, 2: 0.72, 3: 0.52, 4: 0.36, 5: 0.24, 6: 0.16}
_FINISH_DEFAULT = 0.08

# Form ağırlıkları: en yeni koşu en ağır.
_RECENCY = [1.0, 0.82, 0.67, 0.55, 0.45, 0.37]


def form_score(runner: Runner) -> float:
    """Son koşulardaki derecelerin recency-ağırlıklı ortalaması (0-1)."""
    if not runner.form:
        return 0.30  # koşu geçmişi yoksa nötr-altı
    total = weight_sum = 0.0
    for idx, position in enumerate(runner.form[: len(_RECENCY)]):
        w = _RECENCY[idx]
        total += w * _FINISH_POINTS.get(int(position), _FINISH_DEFAULT)
        weight_sum += w
    return total / weight_sum if weight_sum else 0.30


def rating_score(runner: Runner) -> float:
    """Handikap puanı; yoksa formdan türetilmiş kaba bir tahmin."""
    if runner.handicap is not None:
        return float(runner.handicap)
    return 40.0 + 40.0 * form_score(runner)


def jockey_score(runner: Runner) -> float:
    """Jokey kazanma yüzdesi (0-1). Bilinmiyorsa lig ortalaması."""
    if runner.jockey_win_rate is None:
        return 0.10
    return max(0.0, min(1.0, float(runner.jockey_win_rate)))


def weight_score(runner: Runner, race: RaceCard) -> float:
    """Taşınan kiloya göre avantaj: koşu ortalamasının altı pozitif."""
    weights = [r.weight for r in race.active_runners if r.weight is not None]
    if runner.weight is None or not weights:
        return 0.0
    avg = sum(weights) / len(weights)
    return avg - float(runner.weight)  # kg cinsinden avantaj


def distance_fit(runner: Runner, race: RaceCard) -> float:
    """Mesafe uyumu: en iyi mesafesine yakınlık (1.0 = tam uyum)."""
    if race.distance is None or runner.best_distance is None:
        return 0.5
    delta = abs(race.distance - runner.best_distance)
    # 400 m sapma uyumu sıfıra yaklaştırır.
    return max(0.0, 1.0 - delta / 800.0)


def surface_fit(runner: Runner, race: RaceCard) -> float:
    if not race.surface or not runner.surface_pref:
        return 0.5
    return 1.0 if _norm_surface(runner.surface_pref) == _norm_surface(race.surface) else 0.15


def _norm_surface(value: str) -> str:
    v = value.strip().lower()
    if v.startswith("ç") or v.startswith("c") or "turf" in v or "grass" in v:
        return "cim"
    if "sent" in v or "poly" in v or "tapeta" in v:
        return "sentetik"
    return "kum"


def freshness(runner: Runner) -> float:
    """Dinlenme süresi eğrisi: ~14-40 gün ideal, çok kısa/çok uzun ceza."""
    days = runner.days_since_run
    if days is None:
        return 0.5
    if days < 7:
        return 0.25
    if days <= 40:
        return 1.0 - abs(days - 24) / 60.0
    if days <= 90:
        return 0.55 - (days - 40) / 250.0
    return 0.20


def draw_bias(runner: Runner, race: RaceCard) -> float:
    """Start yeri avantajı: kısa kum koşularında içteki kulvarlar avantajlı."""
    if runner.draw is None:
        return 0.0
    field_size = max(len(race.active_runners), 1)
    inside_edge = (field_size - runner.draw + 1) / field_size  # 1.0 = en iç
    short_dirt = (race.distance or 1600) <= 1400 and _norm_surface(race.surface or "kum") == "kum"
    return (inside_edge - 0.5) * (1.0 if short_dirt else 0.25)


def age_fit(runner: Runner, race: RaceCard) -> float:
    """Yaş uyumu: 4-6 yaş zirve kabul edilir; genç atlar uzun mesafede dezavantajlı."""
    if runner.age is None:
        return 0.5
    peak = 5.0
    score = 1.0 - abs(runner.age - peak) / 6.0
    if (race.distance or 1600) >= 2000 and runner.age <= 3:
        score -= 0.15
    return max(0.0, min(1.0, score))


def extract(runner: Runner, race: RaceCard) -> dict[str, float]:
    """Bir at için tüm ham özellikleri döndürür."""
    return {
        "form": form_score(runner),
        "rating": rating_score(runner),
        "jockey": jockey_score(runner),
        "weight": weight_score(runner, race),
        "distance": distance_fit(runner, race),
        "surface": surface_fit(runner, race),
        "freshness": freshness(runner),
        "draw": draw_bias(runner, race),
        "age": age_fit(runner, race),
    }
