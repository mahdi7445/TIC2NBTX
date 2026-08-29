"""
کلاینت نوبیتکس برای معاملات تعهدی (Margin).
پیاده‌سازی دقیقاً بر اساس مستندات apidocs.nobitex.ir (بخش "راهنمای کلید API"
و "معاملات تعهدی") که کاربر فراهم کرده است.

نکات امنیتی مهم:
- NOBITEX_PRIVATE_KEY هرگز نباید در کد یا ریپازیتوری قرار بگیرد؛ فقط از
  متغیر محیطی / GitHub Secrets خوانده می‌شود.
- به‌صورت پیش‌فرض روی testnet کار می‌کند. برای حساب واقعی باید صراحتاً
  base_url و کلیدها را عوض کنید.
"""

from __future__ import annotations

import base64
import json
import time
import logging
from dataclasses import dataclass
from typing import Any, Optional

import requests
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

log = logging.getLogger("nobitex_client")


class NobitexAPIError(Exception):
    """خطای بیزینسی برگردانده‌شده توسط نوبیتکس (status=failed)."""

    def __init__(self, code: str, message: str, raw: dict):
        self.code = code
        self.message = message
        self.raw = raw
        super().__init__(f"{code}: {message}")


@dataclass
class NobitexConfig:
    public_key: str
    private_key_b64: str
    base_url: str = "https://testnetapiv2.nobitex.ir"  # پیش‌فرض: تست‌نت
    user_agent: str = "TraderBot/nobitex-executor-1.0.0"
    request_timeout: int = 10


class NobitexClient:
    def __init__(self, config: NobitexConfig):
        self.cfg = config
        self._private_key = Ed25519PrivateKey.from_private_bytes(
            base64.urlsafe_b64decode(config.private_key_b64)
        )
        self._session = requests.Session()

    # ------------------------------------------------------------------ #
    # امضا و درخواست خام
    # ------------------------------------------------------------------ #
    def _sign(self, timestamp: str, method: str, full_path: str, raw_body: str) -> str:
        payload = f"{timestamp}{method}{full_path}{raw_body}".encode()
        signature = self._private_key.sign(payload)
        return base64.urlsafe_b64encode(signature).decode()

    def _request(
        self,
        method: str,
        path: str,
        *,
        query: Optional[dict] = None,
        json_body: Optional[dict] = None,
    ) -> dict:
        method = method.upper()

        # ساخت full_path دقیقاً همان چیزی که ارسال می‌شود (شامل query string)
        full_path = path
        if query:
            # requests خودش query را می‌سازد؛ برای امضا باید همان رشته را
            # از قبل بسازیم تا با درخواست واقعی یکی باشد.
            from urllib.parse import urlencode

            qs = urlencode(query)
            full_path = f"{path}?{qs}"

        if json_body is not None:
            # جداکننده‌های فشرده دقیقاً مطابق نمونه‌ی مستندات (کاما/دونقطه
            # بدون فاصله) - هرگونه فاصله‌ی اضافه امضا را نامعتبر می‌کند.
            raw_body = json.dumps(json_body, separators=(",", ":"), ensure_ascii=False)
        else:
            raw_body = ""

        timestamp = str(int(time.time()))
        signature = self._sign(timestamp, method, full_path, raw_body)

        headers = {
            "Nobitex-Key": self.cfg.public_key,
            "Nobitex-Signature": signature,
            "Nobitex-Timestamp": timestamp,
            "User-Agent": self.cfg.user_agent,
        }
        if json_body is not None:
            headers["Content-Type"] = "application/json"

        url = self.cfg.base_url + path

        resp = self._session.request(
            method,
            url,
            params=query,
            data=raw_body if json_body is not None else None,
            headers=headers,
            timeout=self.cfg.request_timeout,
        )

        try:
            data = resp.json()
        except ValueError:
            resp.raise_for_status()
            raise NobitexAPIError("HTTPError", f"Non-JSON response: {resp.text[:200]}", {})

        if data.get("status") == "failed":
            raise NobitexAPIError(
                data.get("code", "Unknown"), data.get("message", ""), data
            )

        # برخی خطاهای واقعی HTTP (مثلاً 404 برای پوزیشن ناموجود) هم ممکن
        # است بدنه‌ی JSON معتبر داشته باشند؛ آن‌ها هم بالا already هندل شدند.
        if resp.status_code >= 400 and data.get("status") != "failed":
            resp.raise_for_status()

        return data

    # ------------------------------------------------------------------ #
    # متدهای سطح بالا
    # ------------------------------------------------------------------ #
    def get_margin_markets(self) -> dict:
        """لیست بازارهای پشتیبانی‌شده تعهدی (برای چک کردن وجود نماد)."""
        return self._request(
            "POST", "/margin/markets/list", json_body={"details": True}
        )

    def is_symbol_available(self, src_currency: str, dst_currency: str = "usdt") -> bool:
        markets = self.get_margin_markets().get("markets", {})
        market_key = f"{src_currency.upper()}{dst_currency.upper()}"
        m = markets.get(market_key)
        if not m:
            return False
        return bool(m.get("buyEnabled") or m.get("sellEnabled"))

    def open_position_market(
        self,
        *,
        src_currency: str,
        dst_currency: str,
        side: str,  # "buy" (LONG) یا "sell" (SHORT)
        amount: str,
        leverage: str,
        client_order_id: Optional[str] = None,
    ) -> dict:
        body = {
            "execution": "market",
            "srcCurrency": src_currency.lower(),
            "dstCurrency": dst_currency.lower(),
            "type": side,
            "leverage": leverage,
            "amount": amount,
        }
        if client_order_id:
            body["clientOrderId"] = client_order_id
        return self._request("POST", "/margin/orders/add", json_body=body)

    def list_positions(self, status: str = "active") -> dict:
        return self._request("GET", "/positions/list", query={"status": status})

    def get_position_status(self, position_id: int) -> dict:
        return self._request("GET", f"/positions/{position_id}/status")

    def close_position_market(
        self,
        position_id: int,
        *,
        amount: str,
        client_order_id: Optional[str] = None,
    ) -> dict:
        """
        بستن بخشی یا کامل یک پوزیشن با سفارش Market.
        amount: مقدار بر حسب رمزارز مبدا (همان srcCurrency پوزیشن) -
        نه درصد، نه دلار. برای بستن پله‌ای، amount را برابر همان کسر از
        حجم اولیه‌ی پوزیشن حساب کنید.
        """
        body = {"execution": "market", "amount": amount}
        if client_order_id:
            body["clientOrderId"] = client_order_id
        return self._request(
            "POST", f"/positions/{position_id}/close", json_body=body
        )

    def find_open_position_by_market(
        self, src_currency: str, dst_currency: str
    ) -> Optional[dict]:
        """
        بعد از باز شدن سفارش، این متد را صدا بزنید تا positionId متناظر
        پیدا شود (چون /margin/orders/add فقط order.id را برمی‌گرداند،
        نه positionId).
        """
        positions = self.list_positions(status="active").get("positions", [])
        for p in positions:
            if (
                p.get("srcCurrency", "").lower() == src_currency.lower()
                and p.get("dstCurrency", "").lower() == dst_currency.lower()
                and p.get("status") == "Open"
            ):
                return p
        return None
