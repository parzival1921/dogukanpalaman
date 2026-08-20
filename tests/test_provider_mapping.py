"""Eşleme (mapping) katmanı: ham JSON -> domain nesneleri ve otomatik eşleme çıkarımı."""

from __future__ import annotations

import json

import pytest

from app.providers.http_json import HttpJsonProvider
from tools import infer_mapping

PROGRAM = {
    "status": "ok",
    "data": {
        "kosular": [
            {
                "kosuKodu": "20260821-IST-1",
                "hipodromAdi": "İstanbul",
                "kosuNo": 1,
                "kosuSaati": "21/08/2026 14:30",
                "mesafe": "1400",
                "pist": "Kum",
                "kosuCinsi": "Şartlı 5",
                "ikramiye": "180000",
                "atlar": [
                    {
                        "atNo": 1, "atAdi": "RÜZGAR KIZI", "jokeyAdi": "A. ÇELİK",
                        "antrenorAdi": "K. DOĞAN", "yas": 4, "kilo": "58,5",
                        "handikapPuani": "76", "son6": "1-3-2-1-4", "startYeri": 3,
                        "sonKosuGun": 21, "ganyan": "3,45", "agf1": "22,4", "kosmaz": False,
                    },
                    {
                        "atNo": 2, "atAdi": "BOZKIR ASLANI", "jokeyAdi": "M. KAYA",
                        "antrenorAdi": "L. ŞAHİN", "yas": 5, "kilo": "56,0",
                        "handikapPuani": "71", "son6": "4-2-5-3-3", "startYeri": 1,
                        "sonKosuGun": 34, "ganyan": "6,10", "agf1": "11,8", "kosmaz": True,
                    },
                ],
            }
        ]
    },
}

ODDS = {"ganyanlar": [{"atNo": 1, "ganyan": "3,20"}, {"atNo": 2, "ganyan": "7,45"}]}

RESULTS = {
    "sonuc": {
        "atlar": [{"atNo": 2, "derece": 1}, {"atNo": 1, "derece": 2}],
        "ganyanOdemeleri": {"2": "7,45"},
        "iptal": False,
    }
}


def build_provider() -> HttpJsonProvider:
    """Çıkarım aracının ürettiği eşlemeyi doğrudan kullanır (ikisi birlikte test edilir)."""
    config = infer_mapping.infer_race_cards(PROGRAM)
    return HttpJsonProvider(
        {
            "name": "test",
            "base_url": "https://test.local",
            "race_cards": config,
            "odds": {"items": "ganyanlar", "map": {
                "number": {"path": "atNo", "type": "int"},
                "odds": {"path": "ganyan", "type": "float"},
            }},
            "results": {"root": "sonuc", "items": "atlar", "runner_map": {
                "number": {"path": "atNo", "type": "int"},
                "position": {"path": "derece", "type": "int"},
            }, "map": {
                "win_dividends": {"path": "ganyanOdemeleri", "type": "map_number_float"},
                "cancelled": {"path": "iptal", "type": "bool", "default": False},
            }},
        }
    )


def test_inferred_mapping_finds_paths_and_types():
    section = infer_mapping.infer_race_cards(PROGRAM)
    assert section["items"] == "data.kosular"
    assert section["runners"]["items"] == "atlar"
    assert section["map"]["id"] == "kosuKodu"
    assert section["map"]["post_time"]["format"] == "%d/%m/%Y %H:%M"
    assert section["runners"]["map"]["odds"] == {"path": "ganyan", "type": "float"}
    assert section["runners"]["map"]["form"]["type"] == "int_list"


def test_parse_race_cards_converts_turkish_formats():
    card = build_provider().parse_race_cards(PROGRAM)[0]

    assert card.id == "20260821-IST-1"
    assert card.track == "İstanbul"
    assert card.distance == 1400
    # 14:30 Europe/Istanbul (UTC+3) -> 11:30 UTC
    assert card.post_time.hour == 11 and card.post_time.minute == 30

    first = card.runners[0]
    assert first.weight == pytest.approx(58.5)      # "58,5" ondalık virgül
    assert first.form == [1, 3, 2, 1, 4]            # "1-3-2-1-4"
    assert first.odds == pytest.approx(3.45)
    assert first.scratched is False

    # Koşmayacak at değerlendirmeye girmez.
    assert [r.number for r in card.active_runners] == [1]


def test_parse_odds_and_result():
    provider = build_provider()
    card = provider.parse_race_cards(PROGRAM)[0]

    assert provider.parse_odds(ODDS) == {1: 3.20, 2: 7.45}

    result = provider.parse_result(RESULTS, card)
    assert result is not None
    assert result.finish_order == [2, 1]            # dereceye göre sıralanır
    assert result.win_dividends == {2: 7.45}
    assert result.cancelled is False


def test_missing_sections_are_tolerated():
    provider = HttpJsonProvider({"name": "bos", "base_url": "https://x.test"})
    assert provider.parse_race_cards(PROGRAM) == []
    assert provider.parse_odds(ODDS) == {}


def test_rows_without_post_time_are_skipped():
    provider = build_provider()
    broken = json.loads(json.dumps(PROGRAM))
    broken["data"]["kosular"][0]["kosuSaati"] = "saat yok"
    assert provider.parse_race_cards(broken) == []


def test_normalize_folds_turkish_characters():
    assert infer_mapping.normalize("koşuSaati") == "kosusaati"
    assert infer_mapping.normalize("AT_NO") == "atno"
    assert infer_mapping.score("atNo", ["atno"]) == 100
    assert infer_mapping.score("takim", ["atno"]) == 0
