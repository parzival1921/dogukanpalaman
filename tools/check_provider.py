"""Sağlayıcı eşleme dosyasını doğrular — canlı API'ye ya da kaydedilmiş bir yanıta karşı.

Canlı:
    python -m tools.check_provider config/provider.tjk.json [YYYY-MM-DD]

Çevrimdışı (ağ gerekmez — tarayıcıdan kaydettiğiniz JSON ile):
    python -m tools.check_provider config/provider.tjk.json \
        --sample program.json [--sample-odds ganyan.json] [--sample-results sonuc.json]

Her adımda kaç kayıt çözümlendiğini ve ilk kaydın nasıl eşlendiğini yazar; eşleme
yanlışsa hangi alanın boş geldiği hemen görülür.
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import date, datetime
from pathlib import Path

from app.models import RaceCard
from app.providers.http_json import HttpJsonProvider


def _report_card(card: RaceCard) -> None:
    print(f"  ilk koşu: {card.track} {card.race_no}. koşu · start {card.post_time.isoformat()} "
          f"· {card.distance} m · {len(card.runners)} at")
    if not card.runners:
        print("  ⚠ at listesi boş: runners.items yolunu kontrol edin")
        return
    print("  ilk at:", json.dumps(card.runners[0].to_dict(), ensure_ascii=False))
    missing = [k for k, v in card.runners[0].to_dict().items() if v in (None, "", [])]
    if missing:
        print(f"  ⚠ boş gelen alanlar: {', '.join(missing)}")
    if card.runners[0].odds is None:
        print("  ⚠ ganyan oranı yok: EV hesaplanamaz, ayrı bir oran ucu eşlemeniz gerekir")


def check_offline(config_path: str, program: str, odds: str | None, results: str | None) -> int:
    provider = HttpJsonProvider.from_file(config_path)
    print(f"→ sağlayıcı: {provider.name}  (çevrimdışı örnek doğrulama)")

    payload = json.loads(Path(program).read_text(encoding="utf-8"))
    cards = provider.parse_race_cards(payload)
    print(f"{'✓' if cards else '✕'} program: {len(cards)} koşu çözümlendi")
    if not cards:
        print("  ipucu: 'items' yolunu ve 'map.post_time' / 'map.id' alanlarını kontrol edin")
        print("  ipucu: eşlemeyi otomatik çıkarmak için: python -m tools.infer_mapping " + program)
        return 1
    _report_card(cards[0])

    if odds:
        quotes = provider.parse_odds(json.loads(Path(odds).read_text(encoding="utf-8")))
        print(f"{'✓' if quotes else '✕'} oran   : {len(quotes)} at için oran {dict(list(quotes.items())[:4])}")

    if results:
        result = provider.parse_result(json.loads(Path(results).read_text(encoding="utf-8")), cards[0])
        if result is None:
            print("✕ sonuç : çözümlenemedi — 'items' / 'runner_map.position' alanlarını kontrol edin")
        else:
            print(f"✓ sonuç : bitiş sırası {result.finish_order[:5]} · ganyan temettü {result.win_dividends}")
    return 0


async def check_live(config_path: str, day: date) -> int:
    provider = HttpJsonProvider.from_file(config_path)
    print(f"→ sağlayıcı: {provider.name}  ({provider.config.get('base_url')})")

    try:
        cards = await provider.fetch_race_cards(day)
    except Exception as exc:
        print(f"✕ program çekilemedi: {type(exc).__name__}: {exc}")
        await provider.aclose()
        return 1

    print(f"✓ {day} için {len(cards)} koşu çözümlendi")
    if not cards:
        print("  ipucu: 'items' yolu ve 'params' değerlerini gerçek JSON yanıtıyla karşılaştırın")
        await provider.aclose()
        return 1
    _report_card(cards[0])

    try:
        quotes = await provider.fetch_odds(cards[0])
        print(f"✓ oran ucu: {len(quotes)} at için oran ({dict(list(quotes.items())[:4])} …)")
    except Exception as exc:
        print(f"✕ oran çekilemedi: {type(exc).__name__}: {exc}")

    try:
        result = await provider.fetch_result(cards[0])
        if result is None:
            print("… sonuç ucu boş döndü (koşu henüz koşulmamış olabilir)")
        else:
            print(f"✓ sonuç ucu: bitiş sırası {result.finish_order[:5]} · ganyan {result.win_dividends}")
    except Exception as exc:
        print(f"✕ sonuç çekilemedi: {type(exc).__name__}: {exc}")

    await provider.aclose()
    return 0


def _flag(argv: list[str], name: str) -> str | None:
    return argv[argv.index(name) + 1] if name in argv else None


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2

    config_path = argv[0]
    sample = _flag(argv, "--sample")
    if sample:
        return check_offline(config_path, sample, _flag(argv, "--sample-odds"), _flag(argv, "--sample-results"))

    positional = [a for a in argv[1:] if not a.startswith("--")]
    target_day = datetime.strptime(positional[0], "%Y-%m-%d").date() if positional else date.today()
    return asyncio.run(check_live(config_path, target_day))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
