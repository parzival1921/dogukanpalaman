"""Arka plan döngüsü: program çek → oran güncelle → 5 dk kala kilitle → sonuçlandır."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from . import db, service
from .config import Settings, settings as default_settings
from .providers import build_provider
from .providers.base import RaceProvider

log = logging.getLogger("atyarisi.scheduler")

ANCHOR_KEY = "provider_anchor"


def provider_anchor() -> datetime:
    """Sağlayıcının zaman çıpası; ilk çalıştırmada kaydedilir ve sabit kalır."""
    stored = db.get_meta(ANCHOR_KEY)
    if stored:
        return db.parse_iso(stored)
    anchor = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    db.set_meta(ANCHOR_KEY, db.iso(anchor))
    return anchor


class Scheduler:
    def __init__(self, cfg: Settings | None = None, provider: RaceProvider | None = None) -> None:
        self.cfg = cfg or default_settings
        self.provider = provider or build_provider(provider_anchor(), self.cfg)
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self.last_tick: datetime | None = None
        self.last_error: str | None = None

    async def tick(self) -> dict[str, int]:
        """Tek bir döngü adımı. Hatalar yakalanır, döngü ölmez."""
        summary = {"races": 0, "odds": 0, "locked": 0, "settled": 0}
        try:
            summary["races"] = await service.sync_races(self.provider, cfg=self.cfg)
            summary["odds"] = await service.refresh_odds(self.provider, cfg=self.cfg)
            summary["locked"] = len(service.lock_due_predictions(cfg=self.cfg))
            summary["settled"] = len(await service.settle_finished(self.provider, cfg=self.cfg))
            self.last_error = None
        except Exception as exc:  # pragma: no cover - savunma amaçlı
            self.last_error = str(exc)
            log.exception("döngü adımı başarısız")
        self.last_tick = datetime.now(timezone.utc)
        return summary

    async def _run(self) -> None:
        while not self._stop.is_set():
            await self.tick()
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.cfg.sync_interval_seconds)
            except asyncio.TimeoutError:
                continue

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(self._run(), name="atyarisi-scheduler")
            log.info("zamanlayıcı başladı (her %s sn)", self.cfg.sync_interval_seconds)

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError):  # pragma: no cover
                self._task.cancel()
        await self.provider.aclose()
        log.info("zamanlayıcı durdu")
