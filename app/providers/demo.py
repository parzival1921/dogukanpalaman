"""Demo (simülasyon) sağlayıcısı.

Gerçek bir API'ye erişim olmadan tüm döngüyü — program çekme, oran hareketi,
5 dk kala kilitleme, koşu sonucu ve ödeme — uçtan uca çalıştırmak için kullanılır.
Tamamen deterministiktir: aynı tohum (seed) aynı günü üretir.

Koşular uygulama ilk açıldığı ana göre planlanır (varsayılan: ilk koşu 6 dk sonra,
sonrakiler 8'er dk arayla), böylece kilitleme ve sonuçlandırma birkaç dakika
içinde canlı olarak izlenebilir.
"""

from __future__ import annotations

import math
import random
import zlib
from datetime import date, datetime, timedelta, timezone

from ..config import Settings, settings as default_settings
from ..engine import features
from ..engine.predictor import zscores
from ..models import RaceCard, RaceResult, Runner

TRACKS = ["İstanbul-Veliefendi", "Ankara-75. Yıl", "İzmir-Şirinyer", "Bursa-Osmangazi"]

HORSE_NAMES = [
    "Rüzgar Kızı", "Bozkır Aslanı", "Yıldırım Bey", "Gece Yarısı", "Ateş Böceği",
    "Kara Şimşek", "Deli Poyraz", "Sultan Murat", "Beyaz Kartal", "Alaca Şafak",
    "Fırtına Oğlu", "Zümrüt Ova", "Toros Ejderi", "Mavi Vadi", "Serin Rüzgar",
    "Altın Nal", "Ege Yıldızı", "Kayseri Gülü", "Sarı Zeybek", "Umut Işığı",
    "Hızlı Tay", "Anadolu Beyi", "Çelik Kanat", "Gümüş Yele", "Kırmızı Şahin",
    "Adalet Han", "Bora Efe", "Işık Hanım", "Meltem Kız", "Volkan Ateş",
]

JOCKEYS = [
    "A. Çelik", "M. Kaya", "S. Akdi", "H. Karataş", "E. Yıldırım", "B. Demir",
    "N. Yılmaz", "Ö. Öztürk", "G. Kurt", "V. Sarı", "R. Aydın", "T. Polat",
]

TRAINERS = ["Ç. Erkan", "İ. Aksoy", "K. Doğan", "L. Şahin", "P. Uçar", "Z. Tuna"]

SURFACES = ["kum", "çim"]
DISTANCES = [1200, 1400, 1600, 1800, 2000, 2400]
CLASSES = ["Maiden", "KV-4", "KV-3", "Şartlı-5", "Handikap-16", "Handikap-19", "Grup-3"]


def _seed(*parts: object) -> int:
    """Süreçler arası kararlı tohum (PYTHONHASHSEED'den etkilenmez)."""
    return zlib.crc32("|".join(str(p) for p in parts).encode("utf-8"))


class DemoProvider:
    """Deterministik yarış günü simülatörü."""

    name = "demo"

    def __init__(self, anchor: datetime, cfg: Settings | None = None) -> None:
        self.cfg = cfg or default_settings
        self.anchor = anchor.astimezone(timezone.utc)
        self._cards: dict[str, RaceCard] = {}
        self._truth: dict[str, dict[int, float]] = {}   # race_id -> {at_no: gerçek olasılık}

    # ------------------------------------------------------------------
    # program
    # ------------------------------------------------------------------
    async def fetch_race_cards(self, day: date) -> list[RaceCard]:
        cards = []
        for race_no in range(1, self.cfg.demo_races_per_day + 1):
            card = self._build_race(race_no)
            # Gerçek bir sağlayıcı gibi davran: yalnızca istenen günün koşuları.
            if card.post_time.date() == day:
                cards.append(card)
        return cards

    def _race_id(self, race_no: int) -> str:
        return f"demo-{self.anchor.strftime('%Y%m%d%H%M')}-{race_no:02d}"

    def _build_race(self, race_no: int) -> RaceCard:
        race_id = self._race_id(race_no)
        if race_id in self._cards:
            return self._cards[race_id]

        rng = random.Random(self.cfg.demo_seed * 1000 + race_no)
        post_time = self.anchor + timedelta(
            minutes=self.cfg.demo_first_post_minutes
            + (race_no - 1) * self.cfg.demo_race_interval_minutes
        )
        distance = rng.choice(DISTANCES)
        surface = rng.choice(SURFACES)
        track = TRACKS[(race_no - 1) % len(TRACKS)] if race_no > 6 else TRACKS[0]

        field_size = rng.randint(7, 12)
        names = rng.sample(HORSE_NAMES, field_size)

        # Atların gözlemlenebilir nitelikleri önce üretilir; "gerçek" güç bunlardan
        # TÜREtilir (tersi değil). Böylece simülasyon kendi içinde tutarlıdır:
        # nitelikler gerçekten önemlidir, model onları okuyabilir, ama gücün bir
        # kısmı hiçbir sütunda görünmeyen etkenlerden gelir.
        runners: list[Runner] = []
        talents: list[float] = []
        for idx in range(field_size):
            number = idx + 1
            talent = rng.gauss(0.0, 1.0)          # gizli yetenek
            talents.append(talent)
            runners.append(
                Runner(
                    number=number,
                    horse=names[idx],
                    jockey=JOCKEYS[(race_no * 3 + idx) % len(JOCKEYS)],
                    trainer=TRAINERS[(race_no + idx) % len(TRAINERS)],
                    age=rng.randint(3, 8),
                    # Handikap mantığı: iyi at daha ağır taşır (ve ağırlık yavaşlatır).
                    weight=round(56.0 + 1.8 * talent + rng.uniform(-1.0, 1.0), 1),
                    handicap=round(68.0 + 11.0 * talent + rng.gauss(0, 2.5), 1),
                    form=self._make_form(rng, talent),
                    draw=number,
                    days_since_run=rng.choice([9, 14, 18, 21, 26, 33, 45, 62, 95]),
                    best_distance=distance + rng.choice([-400, -200, 0, 0, 200, 400]),
                    surface_pref=rng.choice(SURFACES),
                    jockey_win_rate=round(
                        max(0.02, min(0.35, 0.13 + 0.05 * talent + rng.gauss(0, 0.02))), 3
                    ),
                    scratched=rng.random() < 0.04,
                )
            )

        card = RaceCard(
            id=race_id,
            track=track,
            race_no=race_no,
            race_date=post_time.date().isoformat(),
            post_time=post_time,
            distance=distance,
            surface=surface,
            race_class=rng.choice(CLASSES),
            purse=float(rng.randrange(90, 460) * 1000),
            runners=runners,
        )

        truth = self._true_probabilities(card, talents, rng)
        self._cards[race_id] = card
        self._truth[race_id] = truth
        return card

    @staticmethod
    def _true_probabilities(
        card: RaceCard, talents: list[float], rng: random.Random
    ) -> dict[int, float]:
        """Gerçek kazanma olasılıkları: yetenek + koşu koşullarına uyum + gözlenemeyen etkenler.

        Katsayılar modelin ağırlıklarıyla kasıtlı olarak aynı değildir; ayrıca
        `unobserved` terimi hiçbir veri sütununda görünmez. Yani iyi bir model
        gerçeğe yaklaşabilir ama asla tam olarak bilemez.
        """
        active = card.active_runners
        if not active:
            return {}

        weights = [r.weight for r in active if r.weight is not None]
        avg_weight = sum(weights) / len(weights) if weights else 0.0

        utilities: dict[int, float] = {}
        for runner in active:
            talent = talents[runner.number - 1]
            unobserved = rng.gauss(0.0, 0.42)
            util = (
                0.85 * talent
                + 0.13 * (avg_weight - (runner.weight or avg_weight))
                + 0.60 * (features.distance_fit(runner, card) - 0.5)
                + 0.50 * (features.surface_fit(runner, card) - 0.5)
                + 0.45 * (features.freshness(runner) - 0.5)
                + 0.35 * features.draw_bias(runner, card)
                + unobserved
            )
            utilities[runner.number] = math.exp(util)

        total = sum(utilities.values()) or 1.0
        return {number: value / total for number, value in utilities.items()}

    @staticmethod
    def _make_form(rng: random.Random, talent: float) -> list[int]:
        """Yetenekle uyumlu ama gürültülü son 5 koşu derecesi."""
        form = []
        for _ in range(5):
            base = 4.5 - 1.7 * talent
            pos = int(max(1, min(12, round(rng.gauss(base, 2.0)))))
            form.append(pos)
        return form

    # ------------------------------------------------------------------
    # oranlar
    # ------------------------------------------------------------------
    async def fetch_odds(self, race: RaceCard) -> dict[int, float]:
        return self._odds_at(race, datetime.now(timezone.utc))

    # Halkın niteliklere verdiği ağırlıklar: göze çarpan sinyaller (son form,
    # popüler jokey, rating) fazla, koşu koşullarına uyum (mesafe, pist, dinlenme,
    # start yeri) neredeyse hiç dikkate alınmaz. Değerin kaynağı budur.
    PUBLIC_WEIGHTS = {
        "form": 1.35,
        "jockey": 0.85,
        "rating": 0.60,
        "weight": 0.10,
        "distance": 0.05,
        "surface": 0.05,
        "freshness": 0.05,
        "draw": 0.05,
        "age": 0.10,
    }

    def _odds_at(self, race: RaceCard, when: datetime) -> dict[int, float]:
        """Halkın algısından oluşan, zamanla hafifçe kayan pari-mutuel piyasa.

        Oranlar gerçek olasılıktan DEĞİL, halkın aynı nitelikleri okuma biçiminden
        üretilir; üstüne kalıcı bir yanılgı (bias), oyun akışıyla kayma (drift),
        sürprizlerin fazla oynanması (favorite-longshot bias) ve hasılat komisyonu
        (overround) eklenir.
        """
        active = race.active_runners
        if not active or not self._truth.get(race.id):
            return {}

        raw = [features.extract(r, race) for r in active]
        z_by_feature = {
            name: zscores([f[name] for f in raw]) for name in self.PUBLIC_WEIGHTS
        }
        utils = [
            sum(self.PUBLIC_WEIGHTS[name] * z_by_feature[name][idx] for name in self.PUBLIC_WEIGHTS)
            for idx in range(len(active))
        ]
        utils = zscores(utils)

        minutes_bucket = int((when - self.anchor).total_seconds() // 120)
        rng = random.Random(_seed(race.id, minutes_bucket))
        static_rng = random.Random(_seed(race.id, "bias"))

        perceived: dict[int, float] = {}
        for idx, runner in enumerate(active):
            bias = static_rng.gauss(0, self.cfg.demo_market_bias)  # kalıcı yanılgı
            drift = rng.gauss(0, 0.05)                             # oyun akışı
            perceived[runner.number] = math.exp(utils[idx] / 1.15 + bias + drift)

        total = sum(perceived.values()) or 1.0
        shares = {n: v / total for n, v in perceived.items()}

        # Favorite-longshot bias: düşük paylı atlar orantısız fazla oynanır.
        tilted = {
            n: s * math.exp(-0.12 * math.log(max(s, 1e-4)) * 0.25) for n, s in shares.items()
        }
        tilt_total = sum(tilted.values()) or 1.0

        odds = {}
        for number, value in tilted.items():
            implied = (value / tilt_total) * self.cfg.demo_overround
            odds[number] = round(max(1.05, min(99.0, 1.0 / implied)), 2)
        return odds

    # ------------------------------------------------------------------
    # sonuçlar
    # ------------------------------------------------------------------
    async def fetch_result(self, race: RaceCard) -> RaceResult | None:
        now = datetime.now(timezone.utc)
        if now < race.post_time + timedelta(minutes=self.cfg.settle_delay_minutes):
            return None
        return self.result_for(race)

    def result_for(self, race: RaceCard) -> RaceResult:
        """Gerçek olasılıklardan deterministik olarak bitiş sırasını çeker."""
        truth = dict(self._truth.get(race.id) or {})
        if not truth:
            self._build_race(race.race_no)
            truth = dict(self._truth.get(race.id) or {})

        rng = random.Random(_seed(race.id, "result"))
        order: list[int] = []
        pool = dict(truth)
        while pool:
            total = sum(pool.values())
            pick = rng.uniform(0, total)
            acc = 0.0
            chosen = next(iter(pool))
            for number, w in pool.items():
                acc += w
                if pick <= acc:
                    chosen = number
                    break
            order.append(chosen)
            pool.pop(chosen)

        final_odds = self._odds_at(race, race.post_time)
        win_dividends = {order[0]: final_odds.get(order[0], 2.0)}
        place_dividends = {
            n: round(max(1.05, 1.0 + (final_odds.get(n, 2.0) - 1.0) / 3.0), 2)
            for n in order[:3]
        }
        return RaceResult(
            race_id=race.id,
            finish_order=order,
            win_dividends=win_dividends,
            place_dividends=place_dividends,
        )

    async def aclose(self) -> None:  # pragma: no cover - kapatılacak kaynak yok
        return None
