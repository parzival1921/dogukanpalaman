"""Sağlayıcı eşleme dosyasını canlı API'ye karşı doğrular.

Kullanım:
    python -m tools.check_provider config/provider.tjk.json [YYYY-MM-DD]

Program, oran ve sonuç uçlarını sırayla dener; her adımda kaç kayıt
çözümlendiğini ve ilk kaydın nasıl eşlendiğini yazar. Eşleme yanlışsa hangi
alanın boş geldiği hemen görülür.
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import date, datetime

from app.providers.http_json import HttpJsonProvider


async def main(config_path: str, day: date) -> int:
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

    card = cards[0]
    print(f"  ilk koşu: {card.track} {card.race_no}. koşu · start {card.post_time.isoformat()} "
          f"· {card.distance} m · {len(card.runners)} at")
    if card.runners:
        print("  ilk at:", json.dumps(card.runners[0].to_dict(), ensure_ascii=False))
        missing = [k for k, v in card.runners[0].to_dict().items() if v in (None, "", [])]
        if missing:
            print(f"  ⚠ boş gelen alanlar: {', '.join(missing)}")

    try:
        quotes = await provider.fetch_odds(card)
        print(f"✓ oran ucu: {len(quotes)} at için oran ({dict(list(quotes.items())[:4])} …)")
    except Exception as exc:
        print(f"✕ oran çekilemedi: {type(exc).__name__}: {exc}")

    try:
        result = await provider.fetch_result(card)
        if result is None:
            print("… sonuç ucu boş döndü (koşu henüz koşulmamış olabilir)")
        else:
            print(f"✓ sonuç ucu: bitiş sırası {result.finish_order[:5]} · "
                  f"ganyan {result.win_dividends}")
    except Exception as exc:
        print(f"✕ sonuç çekilemedi: {type(exc).__name__}: {exc}")

    await provider.aclose()
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        raise SystemExit(2)
    target_day = (
        datetime.strptime(sys.argv[2], "%Y-%m-%d").date()
        if len(sys.argv) > 2
        else date.today()
    )
    raise SystemExit(asyncio.run(main(sys.argv[1], target_day)))
