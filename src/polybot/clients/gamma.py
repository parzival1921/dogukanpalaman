"""Gamma API client — market discovery and metadata.

Gamma (gamma-api.polymarket.com) is Polymarket's read-only metadata service:
it lists markets, questions, outcomes, token ids and liquidity/volume stats.
We use it to decide *what* to look at; order-book pricing comes from the CLOB
client.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Iterable

import httpx

from ..core.models import Market, Token
from ..utils.logging import get_logger

log = get_logger("clients.gamma")

GAMMA_BASE = "https://gamma-api.polymarket.com"


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _as_list(value: Any) -> list[Any]:
    """Gamma returns some array fields as JSON-encoded strings."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else [parsed]
        except json.JSONDecodeError:
            return [value]
    return [value]


class GammaClient:
    def __init__(self, base_url: str = GAMMA_BASE, timeout: float = 15.0):
        self._http = httpx.Client(base_url=base_url, timeout=timeout)

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "GammaClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _market_from_json(self, m: dict[str, Any]) -> Market | None:
        token_ids = _as_list(m.get("clobTokenIds"))
        outcomes = _as_list(m.get("outcomes"))
        prices = _as_list(m.get("outcomePrices"))
        if not token_ids or len(token_ids) != len(outcomes):
            return None
        tokens: list[Token] = []
        for i, tid in enumerate(token_ids):
            price = 0.0
            if i < len(prices):
                try:
                    price = float(prices[i])
                except (TypeError, ValueError):
                    price = 0.0
            tokens.append(Token(token_id=str(tid), outcome=str(outcomes[i]), price=price))
        return Market(
            condition_id=str(m.get("conditionId") or m.get("condition_id") or ""),
            question=str(m.get("question", "")),
            slug=str(m.get("slug", "")),
            tokens=tokens,
            active=bool(m.get("active", True)),
            closed=bool(m.get("closed", False)),
            end_date=_parse_dt(m.get("endDate")),
            min_tick=float(m.get("orderPriceMinTickSize", 0.001) or 0.001),
            min_size=float(m.get("orderMinSize", 5.0) or 5.0),
            neg_risk=bool(m.get("negRisk", False)),
            volume_24h=float(m.get("volume24hr", 0.0) or 0.0),
            liquidity=float(m.get("liquidity", 0.0) or 0.0),
        )

    def fetch_markets(
        self,
        *,
        active: bool = True,
        closed: bool = False,
        limit: int = 500,
        min_volume_24h: float = 0.0,
        order: str = "volume24hr",
    ) -> list[Market]:
        """Page through active markets, newest/most-liquid first."""
        out: list[Market] = []
        offset = 0
        page = 100
        while len(out) < limit:
            params = {
                "active": str(active).lower(),
                "closed": str(closed).lower(),
                "limit": page,
                "offset": offset,
                "order": order,
                "ascending": "false",
            }
            resp = self._http.get("/markets", params=params)
            resp.raise_for_status()
            batch = resp.json()
            if not batch:
                break
            for m in batch:
                market = self._market_from_json(m)
                if market and market.volume_24h >= min_volume_24h:
                    out.append(market)
            if len(batch) < page:
                break
            offset += page
        log.info("fetched %d markets (min_vol=%.0f)", len(out), min_volume_24h)
        return out[:limit]

    def fetch_market(self, condition_id: str) -> Market | None:
        resp = self._http.get("/markets", params={"condition_ids": condition_id})
        resp.raise_for_status()
        data = resp.json()
        if not data:
            return None
        return self._market_from_json(data[0])

    def neg_risk_groups(self, markets: Iterable[Market]) -> dict[str, list[Market]]:
        """Group neg-risk markets that share a mutually-exclusive event.

        Markets in the same event (e.g. "who wins the election") carry the
        same negRiskMarketID in Gamma; we approximate grouping by slug prefix
        when that field is absent. Returns {group_key: [markets]}.
        """
        groups: dict[str, list[Market]] = {}
        for m in markets:
            if not m.neg_risk:
                continue
            key = m.slug.rsplit("-", 1)[0] if "-" in m.slug else m.slug
            groups.setdefault(key, []).append(m)
        return {k: v for k, v in groups.items() if len(v) > 1}
