"""Veri sağlayıcı arayüzü.

Yeni bir kaynak eklemek için bu sınıfı türetip üç metodu doldurmak yeterli.
"""

from __future__ import annotations

from datetime import date
from typing import Protocol, runtime_checkable

from ..models import RaceCard, RaceResult


@runtime_checkable
class RaceProvider(Protocol):
    name: str

    async def fetch_race_cards(self, day: date) -> list[RaceCard]:
        """Verilen güne ait koşu programını (atlar dahil) döndürür."""
        ...

    async def fetch_odds(self, race: RaceCard) -> dict[int, float]:
        """Koşu için güncel ganyan oranlarını {at_no: oran} olarak döndürür."""
        ...

    async def fetch_result(self, race: RaceCard) -> RaceResult | None:
        """Koşu sonucu hazırsa döndürür, değilse None."""
        ...

    async def aclose(self) -> None:
        """Açık kaynakları kapatır."""
        ...
