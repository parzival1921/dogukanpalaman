"""Alan modelleri: koşu kartı, koşan at, sonuç, tahmin ve bahis."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class Runner:
    """Bir koşuda yer alan at."""

    number: int
    horse: str
    jockey: str = ""
    trainer: str = ""
    age: int | None = None
    weight: float | None = None            # kilo (kg)
    handicap: float | None = None          # handikap puanı / rating
    form: list[int] = field(default_factory=list)  # son koşu dereceleri (yeni -> eski)
    draw: int | None = None                # start yeri
    days_since_run: int | None = None
    best_distance: int | None = None       # en iyi derece yaptığı mesafe
    surface_pref: str | None = None        # "kum" | "çim" | "sentetik"
    jockey_win_rate: float | None = None   # 0-1
    agf: float | None = None               # AGF oranı (%)
    odds: float | None = None              # ganyan (ondalık, 1 TL'ye ödenen)
    scratched: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "number": self.number,
            "horse": self.horse,
            "jockey": self.jockey,
            "trainer": self.trainer,
            "age": self.age,
            "weight": self.weight,
            "handicap": self.handicap,
            "form": list(self.form),
            "draw": self.draw,
            "days_since_run": self.days_since_run,
            "best_distance": self.best_distance,
            "surface_pref": self.surface_pref,
            "jockey_win_rate": self.jockey_win_rate,
            "agf": self.agf,
            "odds": self.odds,
            "scratched": self.scratched,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Runner":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class RaceCard:
    """Tek bir koşunun programı."""

    id: str
    track: str                 # hipodrom
    race_no: int
    race_date: str             # YYYY-MM-DD (yerel yarış günü)
    post_time: datetime        # koşunun start saati (UTC, tz-aware)
    distance: int | None = None
    surface: str | None = None
    race_class: str | None = None
    purse: float | None = None
    runners: list[Runner] = field(default_factory=list)

    @property
    def active_runners(self) -> list[Runner]:
        return [r for r in self.runners if not r.scratched]


@dataclass
class RaceResult:
    """Koşu sonucu ve resmi ödemeler."""

    race_id: str
    finish_order: list[int]                       # bitiş sırası (at numaraları)
    win_dividends: dict[int, float] = field(default_factory=dict)    # ganyan ödemesi
    place_dividends: dict[int, float] = field(default_factory=dict)  # plase ödemesi
    cancelled: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "race_id": self.race_id,
            "finish_order": list(self.finish_order),
            "win_dividends": {str(k): v for k, v in self.win_dividends.items()},
            "place_dividends": {str(k): v for k, v in self.place_dividends.items()},
            "cancelled": self.cancelled,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RaceResult":
        return cls(
            race_id=data["race_id"],
            finish_order=[int(x) for x in data.get("finish_order", [])],
            win_dividends={int(k): float(v) for k, v in (data.get("win_dividends") or {}).items()},
            place_dividends={int(k): float(v) for k, v in (data.get("place_dividends") or {}).items()},
            cancelled=bool(data.get("cancelled", False)),
        )


@dataclass
class RunnerAssessment:
    """Modelin bir at için ürettiği değerlendirme."""

    number: int
    horse: str
    jockey: str
    odds: float | None
    model_prob: float
    market_prob: float
    edge: float          # model_prob - market_prob
    ev: float            # 1 TL bahis başına beklenen kâr
    score: float
    features: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "number": self.number,
            "horse": self.horse,
            "jockey": self.jockey,
            "odds": self.odds,
            "model_prob": round(self.model_prob, 4),
            "market_prob": round(self.market_prob, 4),
            "edge": round(self.edge, 4),
            "ev": round(self.ev, 4),
            "score": round(self.score, 4),
            "features": {k: round(v, 3) for k, v in self.features.items()},
        }


@dataclass
class BetProposal:
    """Oynanmaya değer bulunan bahis."""

    race_id: str
    number: int
    horse: str
    bet_type: str        # "win" | "place"
    odds: float
    model_prob: float
    market_prob: float
    edge: float
    ev: float
    kelly: float
    stake: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "race_id": self.race_id,
            "number": self.number,
            "horse": self.horse,
            "bet_type": self.bet_type,
            "odds": self.odds,
            "model_prob": round(self.model_prob, 4),
            "market_prob": round(self.market_prob, 4),
            "edge": round(self.edge, 4),
            "ev": round(self.ev, 4),
            "kelly": round(self.kelly, 4),
            "stake": round(self.stake, 2),
        }
