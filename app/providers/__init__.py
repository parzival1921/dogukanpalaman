"""Veri sağlayıcılar."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from ..config import Settings, settings as default_settings
from .base import RaceProvider  # noqa: F401
from .demo import DemoProvider
from .enriched import EnrichedProvider
from .http_json import HttpJsonProvider
from .nalsesleri import NalsesleriClient


def build_provider(anchor: datetime, cfg: Settings | None = None) -> RaceProvider:
    """PROVIDER ortam değişkenine göre sağlayıcıyı oluşturur.

    demo -> yerleşik simülatör
    http/tjk/<diğer> -> PROVIDER_CONFIG dosyasındaki eşlemeyi kullanan JSON adaptörü
    """
    cfg = cfg or default_settings
    provider = _build_base(anchor, cfg)
    if cfg.history_enabled:
        # Program/oran kaynağı ne olursa olsun, eksik form alanları at geçmişinden dolar.
        return EnrichedProvider(
            provider,
            NalsesleriClient(
                base_url=cfg.history_api_url,
                cache_dir=cfg.history_cache_dir,
                ttl_hours=cfg.history_ttl_hours,
            ),
        )
    return provider


def _build_base(anchor: datetime, cfg: Settings) -> RaceProvider:
    if cfg.provider == "demo":
        return DemoProvider(anchor, cfg)

    config_path = Path(cfg.provider_config)
    if not config_path.exists():
        raise FileNotFoundError(
            f"Sağlayıcı eşleme dosyası bulunamadı: {config_path}. "
            "PROVIDER_CONFIG ile yolu belirtin veya PROVIDER=demo kullanın."
        )
    return HttpJsonProvider.from_file(config_path)
