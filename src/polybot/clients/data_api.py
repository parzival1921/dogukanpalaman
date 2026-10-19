"""Data API client — on-chain activity and the smart-money feed.

The Data API (data-api.polymarket.com) exposes realised trade history and
current positions per wallet, all derived from public Polygon on-chain data.
This is the backbone of smart-money tracking: there is nothing private or
privileged here — we simply read the same chain everyone can, rank wallets by
realised performance, and watch the best ones' fills as they happen.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx

from ..core.models import Side, WalletActivity
from ..utils.logging import get_logger

log = get_logger("clients.data")

DATA_BASE = "https://data-api.polymarket.com"


class DataApiClient:
    def __init__(self, base_url: str = DATA_BASE, timeout: float = 20.0):
        self._http = httpx.Client(base_url=base_url, timeout=timeout)

    def close(self) -> None:
        self._http.close()

    def positions(self, wallet: str) -> list[dict[str, Any]]:
        """Current open positions for a wallet (markets, sizes, avg price)."""
        resp = self._http.get("/positions", params={"user": wallet, "limit": 500})
        resp.raise_for_status()
        return resp.json()

    def value(self, wallet: str) -> float:
        """Current mark-to-market portfolio value of a wallet in USDC."""
        resp = self._http.get("/value", params={"user": wallet})
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list) and data:
            return float(data[0].get("value", 0.0))
        if isinstance(data, dict):
            return float(data.get("value", 0.0))
        return 0.0

    def trades(
        self,
        *,
        wallet: str | None = None,
        market: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[WalletActivity]:
        """Recent trades, optionally filtered to a wallet and/or market."""
        params: dict[str, Any] = {"limit": limit, "offset": offset,
                                  "takerOnly": "false"}
        if wallet:
            params["user"] = wallet
        if market:
            params["market"] = market
        resp = self._http.get("/trades", params=params)
        resp.raise_for_status()
        out: list[WalletActivity] = []
        for t in resp.json():
            try:
                ts = datetime.fromtimestamp(
                    int(t["timestamp"]), tz=timezone.utc
                )
            except (KeyError, ValueError, TypeError):
                ts = datetime.now(timezone.utc)
            side = Side.BUY if str(t.get("side", "BUY")).upper() == "BUY" else Side.SELL
            out.append(
                WalletActivity(
                    wallet=str(t.get("proxyWallet") or t.get("user") or ""),
                    condition_id=str(t.get("conditionId", "")),
                    token_id=str(t.get("asset", "")),
                    outcome=str(t.get("outcome", "")),
                    side=side,
                    price=float(t.get("price", 0.0) or 0.0),
                    size=float(t.get("size", 0.0) or 0.0),
                    timestamp=ts,
                )
            )
        return out

    def wallet_pnl(self, wallet: str, lookback_days: int = 90) -> dict[str, float]:
        """Estimate realised PnL and win-rate for a wallet from its trades.

        This is a pragmatic FIFO estimate over the lookback window — good
        enough to *rank* wallets for discovery, not an accounting ledger.
        Resolved-market payouts are approximated by matching opposing trades;
        unmatched size is marked at last trade price.
        """
        cutoff = datetime.now(timezone.utc).timestamp() - lookback_days * 86400
        trades: list[WalletActivity] = []
        offset = 0
        while True:
            batch = self.trades(wallet=wallet, limit=500, offset=offset)
            if not batch:
                break
            trades.extend(batch)
            if batch[-1].timestamp.timestamp() < cutoff or len(batch) < 500:
                break
            offset += 500

        # FIFO match buys against sells per token to estimate realised PnL.
        from collections import defaultdict, deque

        lots: dict[str, deque[tuple[float, float]]] = defaultdict(deque)
        realised = 0.0
        wins = 0
        closes = 0
        for tr in sorted(trades, key=lambda x: x.timestamp):
            book = lots[tr.token_id]
            if tr.side is Side.BUY:
                book.append((tr.price, tr.size))
            else:
                remaining = tr.size
                while remaining > 1e-9 and book:
                    buy_px, buy_sz = book[0]
                    matched = min(remaining, buy_sz)
                    pnl = (tr.price - buy_px) * matched
                    realised += pnl
                    closes += 1
                    if pnl > 0:
                        wins += 1
                    remaining -= matched
                    if matched >= buy_sz - 1e-9:
                        book.popleft()
                    else:
                        book[0] = (buy_px, buy_sz - matched)
        winrate = (wins / closes) if closes else 0.0
        return {"realised_pnl": realised, "winrate": winrate, "trades": len(trades)}

    def discover_smart_wallets(
        self,
        seed_markets: list[str],
        *,
        min_pnl: float = 5000.0,
        min_winrate: float = 0.58,
        lookback_days: int = 90,
        max_candidates: int = 200,
    ) -> list[tuple[str, dict[str, float]]]:
        """Find high-performing wallets by sampling recent trades in liquid
        markets, then scoring each unique wallet by realised PnL and win-rate.
        Returns a list of (wallet, stats) sorted by PnL, best first.
        """
        seen: set[str] = set()
        for cid in seed_markets:
            for tr in self.trades(market=cid, limit=200):
                if tr.wallet:
                    seen.add(tr.wallet)
            if len(seen) >= max_candidates:
                break
        log.info("scoring %d candidate wallets", len(seen))
        scored: list[tuple[str, dict[str, float]]] = []
        for w in seen:
            stats = self.wallet_pnl(w, lookback_days=lookback_days)
            if stats["realised_pnl"] >= min_pnl and stats["winrate"] >= min_winrate:
                scored.append((w, stats))
        scored.sort(key=lambda x: x[1]["realised_pnl"], reverse=True)
        log.info("found %d qualifying smart-money wallets", len(scored))
        return scored
