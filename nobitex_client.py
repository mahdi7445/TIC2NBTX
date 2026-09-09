# -*- coding: utf-8 -*-
"""Nobitex API client for Margin positions.

This module follows the current official API-key authentication and Margin
endpoints documented by Nobitex. It deliberately does not use the Telegram
API; the executor talks only to GitHub and Nobitex.
"""
from __future__ import annotations

import base64
import json
import logging
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Optional
from urllib.parse import urlencode

import requests
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

log = logging.getLogger("nobitex_client")


class NobitexAPIError(Exception):
    def __init__(self, code: str, message: str, raw: dict | None = None, http_status: int | None = None):
        self.code = code
        self.message = message
        self.raw = raw or {}
        self.http_status = http_status
        super().__init__(f"{code}: {message}")


@dataclass
class NobitexConfig:
    public_key: str
    private_key_b64: str
    base_url: str = "https://apiv2.nobitex.ir"
    public_base_url: str = "https://api.nobitex.ir"
    user_agent: str = "TraderBot/TRADE-IS-COOL-NOBITEX-1.0"
    request_timeout: int = 10


class NobitexClient:
    def __init__(self, config: NobitexConfig):
        self.cfg = config
        try:
            raw = base64.urlsafe_b64decode(config.private_key_b64)
            self._private_key = Ed25519PrivateKey.from_private_bytes(raw)
        except Exception as e:
            raise ValueError("NOBITEX_PRIVATE_KEY is not a valid URL-safe base64 Ed25519 private key") from e

        self._session = requests.Session()
        self._session.trust_env = False
        self._session.proxies = {}
        self._session.headers.update({"User-Agent": self.cfg.user_agent})

    @staticmethod
    def _raw_json(body: Optional[dict]) -> str:
        if body is None:
            return ""
        return json.dumps(body, separators=(",", ":"), ensure_ascii=False)

    def _sign(self, timestamp: str, method: str, full_path: str, raw_body: str) -> str:
        payload = f"{timestamp}{method}{full_path}{raw_body}".encode("utf-8")
        sig = self._private_key.sign(payload)
        return base64.urlsafe_b64encode(sig).decode("ascii")

    def _request(self, method: str, path: str, *, query: Optional[dict] = None,
                 json_body: Optional[dict] = None, allow_retry_get: bool = True) -> dict:
        method = method.upper()
        qs = urlencode(query or {}, doseq=True)
        full_path = f"{path}?{qs}" if qs else path
        raw_body = self._raw_json(json_body)

        timestamp = str(int(time.time()))
        signature = self._sign(timestamp, method, full_path, raw_body)
        headers = {
            "Nobitex-Key": self.cfg.public_key,
            "Nobitex-Signature": signature,
            "Nobitex-Timestamp": timestamp,
        }
        if json_body is not None:
            headers["Content-Type"] = "application/json"

        url = self.cfg.base_url.rstrip("/") + path
        attempts = 3 if (allow_retry_get and method == "GET") else 1
        last_error: Exception | None = None

        for attempt in range(attempts):
            try:
                resp = self._session.request(
                    method, url, params=query, data=raw_body if json_body is not None else None,
                    headers=headers, timeout=self.cfg.request_timeout,
                )
                try:
                    data = resp.json()
                except ValueError:
                    raise NobitexAPIError(
                        "HTTPError", f"HTTP {resp.status_code}: {resp.text[:500]}", {}, resp.status_code
                    )

                if data.get("status") == "failed":
                    code = data.get("code", "Unknown")
                    message = data.get("message", "") or data.get("detail", "")
                    if not message and "updatedStatus" in data:
                        message = f"وضعیت سفارش: {data['updatedStatus']}"
                    raise NobitexAPIError(code, message or "Nobitex returned status=failed", data, resp.status_code)

                if resp.status_code >= 400:
                    raise NobitexAPIError(
                        f"HTTP{resp.status_code}", resp.text[:500] or "HTTP error", data, resp.status_code
                    )
                return data
            except (requests.RequestException, NobitexAPIError) as e:
                last_error = e
                # Never blindly repeat a trading POST after a timeout. For GETs,
                # a short retry is safe and useful for transient network errors.
                if method == "GET" and attempt + 1 < attempts:
                    time.sleep(0.7 * (attempt + 1))
                    continue
                if isinstance(e, NobitexAPIError):
                    raise
                raise NobitexAPIError("NetworkError", str(e), {}) from e

        raise NobitexAPIError("NetworkError", str(last_error or "request failed"), {})


    def get_wallets(self, wallet_type: str = "margin", currencies: Optional[str] = None) -> dict:
        query: dict[str, Any] = {"type": wallet_type}
        if currencies:
            query["currencies"] = currencies
        return self._request("GET", "/users/wallets/list", query=query)

    def get_margin_usdt_balance(self) -> Decimal:
        data = self.get_wallets("margin", "usdt")
        for w in data.get("wallets", []):
            if str(w.get("currency", "")).lower() == "usdt":
                return Decimal(str(w.get("activeBalance", w.get("balance", "0"))))
        return Decimal("0")

    def get_spot_usdt_balance(self) -> Decimal:
        data = self.get_wallets("spot", "usdt")
        for w in data.get("wallets", []):
            if str(w.get("currency", "")).lower() == "usdt":
                return Decimal(str(w.get("activeBalance", w.get("balance", "0"))))
        return Decimal("0")

    def transfer_wallet(self, currency: str, amount: str, src: str, dst: str) -> dict:
        body = {"currency": currency.lower(), "amount": amount, "src": src, "dst": dst}
        return self._request("POST", "/wallets/transfer", json_body=body, allow_retry_get=False)

    # ---------------- Margin market / limits ----------------
    def get_margin_markets(self) -> dict:
        return self._request("GET", "/margin/markets/list")

    def get_delegation_limit(self, market: str) -> dict:
        return self._request("GET", "/margin/v2/delegation-limit", query={"market": market.upper()})

    def is_symbol_available(self, src_currency: str, dst_currency: str, side: str = "buy") -> bool:
        markets = self.get_margin_markets().get("markets", {})
        key = f"{src_currency.upper()}{dst_currency.upper()}"
        m = markets.get(key)
        if not m:
            return False
        return bool(m.get("buyEnabled") if side.lower() == "buy" else m.get("sellEnabled"))

    def market_max_leverage(self, src_currency: str, dst_currency: str) -> Decimal | None:
        markets = self.get_margin_markets().get("markets", {})
        m = markets.get(f"{src_currency.upper()}{dst_currency.upper()}")
        if not m or m.get("maxLeverage") is None:
            return None
        return Decimal(str(m["maxLeverage"]))

    # ---------------- Opening ----------------
    def open_position_market(self, *, src_currency: str, dst_currency: str, side: str,
                             amount: str, leverage: str, price: Optional[str] = None) -> dict:
        body = {
            "execution": "market",
            "srcCurrency": src_currency.lower(),
            "dstCurrency": dst_currency.lower(),
            "type": side.lower(),
            "leverage": leverage,
            "amount": amount,
        }
        if price is not None:
            body["price"] = price
        return self._request("POST", "/margin/orders/add", json_body=body, allow_retry_get=False)

    # ---------------- Closing orders attached to a position ----------------
    def place_position_close_limit(self, *, position_id: int, amount: str, price: str) -> dict:
        body = {"execution": "limit", "amount": amount, "price": price}
        return self._request("POST", f"/positions/{position_id}/close", json_body=body, allow_retry_get=False)

    def place_position_close_stop_market(self, *, position_id: int, amount: str, stop_price: str) -> dict:
        body = {"execution": "stop_market", "amount": amount, "stopPrice": stop_price}
        return self._request("POST", f"/positions/{position_id}/close", json_body=body, allow_retry_get=False)

    def place_position_close_oco(self, *, position_id: int, amount: str, price: str,
                                 stop_price: str, stop_limit_price: str) -> dict:
        body = {
            "mode": "oco",
            "amount": amount,
            "price": price,
            "stopPrice": stop_price,
            "stopLimitPrice": stop_limit_price,
        }
        return self._request("POST", f"/positions/{position_id}/close", json_body=body, allow_retry_get=False)

    def close_position_market(self, position_id: int, *, amount: str) -> dict:
        body = {"execution": "market", "amount": amount}
        return self._request("POST", f"/positions/{position_id}/close", json_body=body, allow_retry_get=False)

    # ---------------- Orders / positions ----------------
    def cancel_order(self, order_id: int) -> dict:
        return self._request(
            "POST", "/market/orders/update-status",
            json_body={"order": int(order_id), "status": "canceled"}, allow_retry_get=False,
        )

    def get_order_status(self, order_id: Optional[int] = None, client_order_id: Optional[str] = None) -> dict:
        body: dict[str, Any] = {}
        if order_id is not None:
            body["id"] = int(order_id)
        elif client_order_id:
            body["clientOrderId"] = client_order_id
        else:
            raise ValueError("order_id or client_order_id is required")
        return self._request("POST", "/market/orders/status", json_body=body, allow_retry_get=False)

    def list_orders(self, *, src_currency: Optional[str] = None, dst_currency: Optional[str] = None,
                    status: str = "open", trade_type: str = "margin", details: int = 2) -> dict:
        q: dict[str, Any] = {"status": status, "tradeType": trade_type, "details": details}
        if src_currency:
            q["srcCurrency"] = src_currency.lower()
        if dst_currency:
            q["dstCurrency"] = dst_currency.lower()
        return self._request("GET", "/market/orders/list", query=q)

    def list_positions(self, *, status: str = "active", src_currency: Optional[str] = None,
                       dst_currency: Optional[str] = None) -> dict:
        q: dict[str, Any] = {"status": status}
        if src_currency:
            q["srcCurrency"] = src_currency.lower()
        if dst_currency:
            q["dstCurrency"] = dst_currency.lower()
        return self._request("GET", "/positions/list", query=q)

    def get_position_status(self, position_id: int) -> dict:
        return self._request("GET", f"/positions/{int(position_id)}/status")

    def find_open_position_by_market(self, src_currency: str, dst_currency: str,
                                     side: Optional[str] = None) -> Optional[dict]:
        positions = self.list_positions(status="active", src_currency=src_currency, dst_currency=dst_currency).get("positions", [])
        candidates = [p for p in positions if str(p.get("status", "")).lower() == "open"]
        if side:
            wanted = "buy" if side.upper() == "LONG" else "sell"
            candidates = [p for p in candidates if str(p.get("side", "")).lower() == wanted]
        if not candidates:
            return None
        candidates.sort(key=lambda p: p.get("openedAt") or p.get("createdAt") or "", reverse=True)
        return candidates[0]

    # ---------------- Public market data ----------------
    def get_last_trade_price(self, market: str) -> Decimal:
        # v3 is the current documented public orderbook endpoint and is served
        # from the public API host (not the authenticated apiv2 host).
        resp = self._session.get(
            self.cfg.public_base_url.rstrip("/") + f"/v3/orderbook/{market.upper()}",
            timeout=self.cfg.request_timeout,
        )
        try:
            data = resp.json()
        except ValueError as e:
            raise NobitexAPIError("HTTPError", f"Invalid orderbook response: {resp.text[:300]}", {}) from e
        if resp.status_code >= 400 or data.get("status") != "ok":
            raise NobitexAPIError("HTTPError", f"Orderbook HTTP {resp.status_code}", data, resp.status_code)
        return Decimal(str(data["lastTradePrice"]))
