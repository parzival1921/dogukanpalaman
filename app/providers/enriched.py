"""Bir sağlayıcının koşu kartlarını at geçmişiyle zenginleştiren sarmalayıcı.

Günün programını veren kaynak genellikle sadece "kim koşuyor + ganyan" verir;
modelin ihtiyaç duyduğu form, dinlenme süresi, en iyi mesafe, pist tercihi ve
jokey isabet oranı ayrı bir geçmiş kaynağından gelir (nalsesleri.com API'si gibi).

Yalnızca **boş gelen** alanlar doldurulur: temel sağlayıcı bir değer verdiyse
ona dokunulmaz.
"""

from __future__ import annotations

import logging
from datetime import date

from ..models import RaceCard, RaceResult
from . import nalsesleri
from .base import RaceProvider

log = logging.getLogger("atyarisi.enrich")


class EnrichedProvider:
    """Temel sağlayıcı + geçmiş verisiyle özellik tamamlama."""

    def __init__(self, base: RaceProvider, client: nalsesleri.NalsesleriClient) -> None:
        self.base = base
        self.client = client
        self.name = f"{getattr(base, 'name', 'base')}+gecmis"

    async def fetch_race_cards(self, day: date) -> list[RaceCard]:
        cards = await self.base.fetch_race_cards(day)
        if not cards:
            return cards

        names = [r.horse for card in cards for r in card.runners if r.horse]
        try:
            histories = await self.client.histories(names)
        except Exception as exc:      # geçmiş alınamazsa program yine de kullanılabilir
            log.warning("at geçmişi alınamadı: %s", exc)
            return cards

        all_runs = [run for runs in histories.values() for run in runs]
        rates = nalsesleri.jockey_win_rates(all_runs)
        filled = 0

        for card in cards:
            for runner in card.runners:
                runs = histories.get(runner.horse) or []
                if runs:
                    profile = nalsesleri.runner_profile(runs, day)
                    for field, value in profile.items():
                        if value in (None, [], "") or getattr(runner, field) not in (None, [], ""):
                            continue
                        setattr(runner, field, value)
                        filled += 1
                if runner.jockey_win_rate is None and runner.jockey in rates:
                    runner.jockey_win_rate = rates[runner.jockey]
                    filled += 1

        log.info("geçmişten %d alan dolduruldu (%d at)", filled, len(set(names)))
        return cards

    async def fetch_odds(self, race: RaceCard) -> dict[int, float]:
        return await self.base.fetch_odds(race)

    async def fetch_result(self, race: RaceCard) -> RaceResult | None:
        return await self.base.fetch_result(race)

    async def aclose(self) -> None:
        await self.base.aclose()
        await self.client.aclose()
