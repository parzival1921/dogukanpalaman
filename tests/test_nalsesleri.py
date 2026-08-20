"""nalsesleri.com geçmiş verisi: ayrıştırma, özellik çıkarımı, koşu kurulumu, zenginleştirme."""

from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from app.models import RaceCard, Runner
from app.providers import nalsesleri as ns
from app.providers.enriched import EnrichedProvider

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "nalsesleri_history.json").read_text(encoding="utf-8"))
RUNS = ns.parse_history(FIXTURE)


def test_parse_history_orders_newest_first_and_types_fields():
    assert len(RUNS) == 5
    assert RUNS[0].race_date == date(2024, 12, 1)
    assert RUNS[-1].race_date == date(2024, 8, 15)

    newest = RUNS[0]
    assert newest.race_key == "2024-12-01-123-1"
    assert newest.surface == "Çim"
    assert newest.weight == pytest.approx(55.5)
    assert newest.seconds == pytest.approx(94.56)
    assert newest.finished is True

    scratched = next(r for r in RUNS if r.race_date == date(2024, 9, 20))
    assert scratched.non_runner is True
    assert scratched.finished is False
    assert scratched.odds is None          # 0 -> None


def test_parse_history_rejects_error_payloads():
    assert ns.parse_history({"status": "error", "message": "No data found"}) == []
    assert ns.parse_history(None) == []
    assert ns.parse_history({"status": "success", "data": [{"race_date": "yok"}]}) == []


@pytest.mark.parametrize(
    "value,expected",
    [("1:34.56", 94.56), ("58.20", 58.2), ("58,20", 58.2), ("2:01", 121.0), ("", None), (None, None), ("abc", None)],
)
def test_parse_seconds(value, expected):
    result = ns.parse_seconds(value)
    if expected is None:
        assert result is None
    else:
        assert result == pytest.approx(expected)


def test_form_skips_non_runners():
    assert ns.form_positions(RUNS) == [1, 3, 8, 2]      # 2024-09-20 koşmadı
    assert ns.form_positions(RUNS, limit=2) == [1, 3]


def test_days_since_run_ignores_future_races():
    assert ns.days_since_run(RUNS, date(2024, 12, 20)) == 19
    # 2024-11-15'te en son koşu 2024-11-10'dur (1 Aralık henüz olmamıştır).
    assert ns.days_since_run([r for r in RUNS if r.race_date <= date(2024, 11, 15)], date(2024, 11, 15)) == 5


def test_best_distance_and_surface_preference():
    assert ns.best_distance(RUNS) == 1600           # 1. olduğu koşu
    assert ns.preferred_surface(RUNS) == "Çim"      # kumda 8. olmuş


def test_jockey_win_rate_is_shrunk_towards_league_average():
    rates = ns.jockey_win_rates(RUNS)
    # Ali Veli 1 koşu / 0 galibiyet -> %0 değil, lig ortalamasına yakın
    assert 0.05 < rates["Ali Veli"] < ns.LEAGUE_WIN_RATE
    # John Doe 3 koşu / 1 galibiyet -> %33 değil, aşağı çekilmiş
    assert ns.LEAGUE_WIN_RATE < rates["John Doe"] < 0.25


def test_reconstruct_races_has_no_look_ahead():
    races = ns.reconstruct_races({"Lightning Bolt": RUNS}, min_field_size=1)
    by_id = {card.id: (card, result) for card, result in races}

    # En eski koşuda geçmiş yok -> form boş, dinlenme süresi bilinmiyor.
    oldest, _ = by_id["2024-08-15-123-5"]
    assert oldest.runners[0].form == []
    assert oldest.runners[0].days_since_run is None
    assert oldest.runners[0].jockey_win_rate is None

    # En yeni koşuda yalnızca ÖNCEKİ koşular kullanılır (kendi derecesi olan 1 yok).
    newest, result = by_id["2024-12-01-123-1"]
    assert newest.runners[0].form == [3, 8, 2]
    assert newest.runners[0].days_since_run == 21
    assert result.finish_order == [5]
    assert result.win_dividends == {5: 3.5}


def test_reconstruct_races_filters_short_fields():
    assert ns.reconstruct_races({"Lightning Bolt": RUNS}, min_field_size=5) == []


def test_reconstruct_merges_horses_of_the_same_race():
    rival_rows = [
        dict(row, horse_name="Rakip At", horse_number=9, result=2, jockey="Ali Veli")
        for row in FIXTURE["data"]
        if row["race_date"] == "2024-12-01"
    ]
    histories = {
        "Lightning Bolt": RUNS,
        "Rakip At": ns.parse_history({"status": "success", "data": rival_rows}),
    }
    races = ns.reconstruct_races(histories, min_field_size=2)
    card, result = next((c, r) for c, r in races if c.id == "2024-12-01-123-1")

    assert sorted(r.number for r in card.runners) == [5, 9]
    assert result.finish_order == [5, 9]        # dereceye göre


# --------------------------------------------------------------------------
# zenginleştirme
# --------------------------------------------------------------------------

class FakeBase:
    name = "sahte"

    def __init__(self, card: RaceCard) -> None:
        self.card = card

    async def fetch_race_cards(self, day):
        return [self.card]

    async def fetch_odds(self, race):
        return {1: 2.5}

    async def fetch_result(self, race):
        return None

    async def aclose(self):
        return None


class FakeClient:
    def __init__(self, data):
        self._data = data          # `histories` adı metotla çakışmasın
        self.asked: list[str] = []

    async def histories(self, names):
        self.asked = list(names)
        return self._data

    async def aclose(self):
        return None


def build_card() -> RaceCard:
    return RaceCard(
        id="r1", track="Test", race_no=1, race_date="2024-12-10",
        post_time=datetime(2024, 12, 10, 12, 0, tzinfo=timezone.utc),
        distance=1600, surface="Çim",
        runners=[
            Runner(number=1, horse="Lightning Bolt", jockey="John Doe", odds=2.5),
            Runner(number=2, horse="Bilinmeyen At", jockey="Yeni Jokey", odds=8.0, form=[4, 5]),
        ],
    )


def test_enrichment_fills_only_missing_fields():
    card = build_card()
    provider = EnrichedProvider(FakeBase(card), FakeClient({"Lightning Bolt": RUNS, "Bilinmeyen At": []}))

    enriched = asyncio.run(provider.fetch_race_cards(date(2024, 12, 10)))[0]
    first, second = enriched.runners

    assert first.form == [1, 3, 8, 2]
    assert first.days_since_run == 9
    assert first.best_distance == 1600
    assert first.surface_pref == "Çim"
    assert first.handicap == pytest.approx(80.0)
    assert first.jockey_win_rate is not None
    assert first.odds == pytest.approx(2.5)        # temel sağlayıcının verdiği değer korunur

    # Geçmişi olmayan at: temel sağlayıcının verdiği form ezilmez.
    assert second.form == [4, 5]
    assert second.days_since_run is None


def test_enrichment_survives_history_failure():
    class Failing(FakeClient):
        async def histories(self, names):
            raise RuntimeError("api down")

    card = build_card()
    provider = EnrichedProvider(FakeBase(card), Failing({}))
    enriched = asyncio.run(provider.fetch_race_cards(date(2024, 12, 10)))
    assert enriched[0].runners[0].odds == pytest.approx(2.5)   # program yine kullanılabilir
