"""CLOB client — live order books and order placement.

Reads (order books, midpoints, prices) go over plain HTTPS to the public
CLOB REST endpoint. Writes (placing/cancelling orders) are delegated to the
official `py-clob-client`, which handles EIP-712 signing of orders. The
signing client is created lazily and only when real credentials are present,
so the whole module imports fine in dry-run / paper mode with no keys.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx

from ..config import Secrets
from ..core.models import Order, OrderBook, OrderBookLevel, OrderType, Side
from ..utils.logging import get_logger

log = get_logger("clients.clob")

CLOB_BASE = "https://clob.polymarket.com"
POLYGON_CHAIN_ID = 137


class ClobClient:
    def __init__(self, secrets: Secrets | None = None, base_url: str = CLOB_BASE,
                 timeout: float = 15.0):
        self._http = httpx.Client(base_url=base_url, timeout=timeout)
        self._secrets = secrets or Secrets()
        self._signer: Any = None  # py_clob_client.ClobClient, lazily built

    # ---- reads -----------------------------------------------------------
    def order_book(self, token_id: str) -> OrderBook:
        resp = self._http.get("/book", params={"token_id": token_id})
        resp.raise_for_status()
        data = resp.json()
        bids = [
            OrderBookLevel(float(b["price"]), float(b["size"]))
            for b in data.get("bids", [])
        ]
        asks = [
            OrderBookLevel(float(a["price"]), float(a["size"]))
            for a in data.get("asks", [])
        ]
        bids.sort(key=lambda x: x.price, reverse=True)
        asks.sort(key=lambda x: x.price)
        return OrderBook(
            token_id=token_id,
            bids=bids,
            asks=asks,
            timestamp=datetime.now(timezone.utc),
        )

    def midpoint(self, token_id: str) -> float | None:
        resp = self._http.get("/midpoint", params={"token_id": token_id})
        resp.raise_for_status()
        mid = resp.json().get("mid")
        return float(mid) if mid is not None else None

    def price(self, token_id: str, side: Side) -> float | None:
        """Best price at which a taker could immediately trade `side`."""
        resp = self._http.get(
            "/price", params={"token_id": token_id, "side": side.value}
        )
        resp.raise_for_status()
        px = resp.json().get("price")
        return float(px) if px is not None else None

    # ---- signing / writes ------------------------------------------------
    def _ensure_signer(self) -> Any:
        if self._signer is not None:
            return self._signer
        if not self._secrets.can_trade:
            raise RuntimeError("no POLY_PRIVATE_KEY set; cannot place live orders")
        try:
            from py_clob_client.client import ClobClient as _PyClob
            from py_clob_client.clob_types import ApiCreds
        except ImportError as e:  # pragma: no cover
            raise RuntimeError(
                "py-clob-client not installed; run `pip install py-clob-client`"
            ) from e

        creds = None
        if all((self._secrets.clob_api_key, self._secrets.clob_api_secret,
                self._secrets.clob_api_passphrase)):
            creds = ApiCreds(
                api_key=self._secrets.clob_api_key,
                api_secret=self._secrets.clob_api_secret,
                api_passphrase=self._secrets.clob_api_passphrase,
            )
        # signature_type=1 + funder == browser/proxy (Safe) wallet flow.
        signer = _PyClob(
            host=CLOB_BASE,
            key=self._secrets.private_key,
            chain_id=POLYGON_CHAIN_ID,
            creds=creds,
            signature_type=1 if self._secrets.funder_address else 0,
            funder=self._secrets.funder_address,
        )
        if creds is None:
            # Derive (or create) L2 API credentials from the private key.
            signer.set_api_creds(signer.create_or_derive_api_creds())
        self._signer = signer
        log.info("CLOB signer initialised (funder=%s)",
                 self._secrets.funder_address or "EOA")
        return signer

    def place_order(self, order: Order) -> dict[str, Any]:
        """Sign and submit a real order. Raises if no credentials configured."""
        from py_clob_client.clob_types import OrderArgs, OrderType as _OT
        from py_clob_client.order_builder.constants import BUY, SELL

        signer = self._ensure_signer()
        args = OrderArgs(
            token_id=order.token_id,
            price=order.price,
            size=order.size,
            side=BUY if order.side is Side.BUY else SELL,
        )
        signed = signer.create_order(args)
        ot = _OT.GTC if order.order_type is OrderType.GTC else _OT.FOK
        resp = signer.post_order(signed, ot)
        log.info("placed %s %s %.2f@%.3f -> %s", order.side.value,
                 order.token_id[:10], order.size, order.price, resp)
        return resp

    def cancel(self, order_id: str) -> dict[str, Any]:
        signer = self._ensure_signer()
        return signer.cancel(order_id=order_id)

    def cancel_all(self) -> dict[str, Any]:
        signer = self._ensure_signer()
        return signer.cancel_all()

    def close(self) -> None:
        self._http.close()
