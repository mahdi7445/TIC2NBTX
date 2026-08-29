# -*- coding: utf-8 -*-
"""
پارسر پیام‌های کانال تلگرام مشترک (خروجی bot.py و candle_engine.py).

⚠️ نکته‌ی حیاتی: این پارسر متن پیام‌ها را دقیقاً بر اساس فرمت فعلیِ توابع
format_entry_message / format_stop_message / format_breakeven_message /
format_sl_after_t2_message / format_sl_after_t3_message /
format_runner_stop_message / format_rr_exit_message / format_forced_close_message
(در bot.py و candle_engine.py) تشخیص می‌دهد. اگر متن آن توابع در آینده تغییر
کند، این پارسر باید هم‌زمان به‌روزرسانی شود - وگرنه به‌صورت خاموش شکست
می‌خورد (برای همین has_unparsed_alert در executor.py وجود دارد).

درصدهای بستن پله‌ای (از متن پیام‌های bot.py استخراج شده‌اند):
    Target 1: 20%   (باقی‌مانده بعد از T1: 80%)
    Target 2: 30%   (باقی‌مانده بعد از T2: 50%)
    Target 3: 15%   (باقی‌مانده بعد از T3: 35%)
    Target 4: 10%   (باقی‌مانده بعد از T4 = رانر: 25%)
    Runner:   25%   (با تریلینگ استاپ ۱.۵R پشت سقف - بستن نهایی)
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

# اگر ربات‌ها اعداد را با کاما جدا می‌کنند (مثلاً 63,400.5) این regex هم آن‌ها
# را می‌پذیرد. format_signal_price دقیق ربات را ندیدیم، پس محافظه‌کارانه
# کاما و اعشار هر دو پشتیبانی می‌شوند.
_NUM = r"[\d,]+(?:\.\d+)?"

_HEADER_ENTRY = re.compile(
    r"^(?P<emoji>🟢|🔴)\s*(?P<side>LONG|SHORT)\s*—\s*(?P<symbol>\S+)\s+(?P<tf>\S+)",
    re.MULTILINE,
)
_ENTRY_PRICE = re.compile(rf"Entry:\s*(?:<b>)?({_NUM})", re.IGNORECASE)
_STOP_PRICE = re.compile(rf"❌\s*Stop:\s*(?:<b>)?({_NUM})", re.IGNORECASE)
_TARGET_PRICE = re.compile(rf"🎯\s*Target\s*(\d)\s*:\s*(?:<b>)?({_NUM})")

_HEADER_TARGET_HIT = re.compile(
    r"^✅\s*Target\s*(?P<level>\d)\s*HIT\s*\([^)]*\)\s*—\s*(?P<symbol>\S+)\s+(?P<tf>\S+)",
    re.MULTILINE,
)
_HEADER_STOP = re.compile(
    r"^❌\s*STOP\s*HIT\s*—\s*(?P<symbol>\S+)\s+(?P<tf>\S+)", re.MULTILINE
)
_HEADER_BREAKEVEN = re.compile(
    r"^⚪\s*BREAKEVEN\s*—\s*(?P<symbol>\S+)\s+(?P<tf>\S+)", re.MULTILINE
)
_HEADER_SL_AFTER_T2 = re.compile(
    r"^🔒\s*STOP AFTER TARGET 2\s*—\s*(?P<symbol>\S+)\s+(?P<tf>\S+)", re.MULTILINE
)
_HEADER_SL_AFTER_T3 = re.compile(
    r"^🔒\s*STOP AFTER TARGET 3\s*—\s*(?P<symbol>\S+)\s+(?P<tf>\S+)", re.MULTILINE
)
_HEADER_RUNNER_CLOSED = re.compile(
    r"^🏁\s*RUNNER CLOSED\s*—\s*(?P<symbol>\S+)\s+(?P<tf>\S+)", re.MULTILINE
)
_HEADER_FORCED_CLOSE = re.compile(
    r"^⚠️\s*TRADE CLOSED\s*—\s*(?P<symbol>\S+)\s+(?P<tf>\S+)", re.MULTILINE
)

# درصد حجمی که در هر رویداد باید "اضافه" بسته شود (نسبت به حجم اولیه‌ی پوزیشن)
TARGET_CLOSE_PCT = {1: 0.20, 2: 0.30, 3: 0.15, 4: 0.10}
RUNNER_CLOSE_PCT = 0.25  # کل مانده بعد از تارگت ۴


def _num(s: str) -> float:
    return float(s.replace(",", ""))


@dataclass
class ParsedSignal:
    kind: str  # "entry"
    symbol: str
    timeframe: str
    side: str  # "LONG" | "SHORT"
    entry: float
    stop: float
    targets: dict  # {1: price, 2: price, 3: price, 4: price}


@dataclass
class ParsedEvent:
    kind: str  # "target_hit" | "stop" | "breakeven" | "sl_after_t2" | "sl_after_t3" | "runner_closed" | "forced_close"
    symbol: str
    timeframe: str
    level: Optional[int] = None  # فقط برای target_hit


def parse_message(text: str) -> Optional[object]:
    """
    یک پیام کانال را پارس می‌کند و ParsedSignal یا ParsedEvent برمی‌گرداند.
    اگر پیام هیچ‌کدام از الگوهای شناخته‌شده نبود، None برمی‌گرداند - این باید
    توسط فراخواننده به‌عنوان "پیام ناشناخته" لاگ/هشدار شود، نه نادیده گرفته شود.
    """

    m = _HEADER_ENTRY.search(text)
    if m:
        entry_m = _ENTRY_PRICE.search(text)
        stop_m = _STOP_PRICE.search(text)
        if not entry_m or not stop_m:
            return None  # هدر پیدا شد ولی بدنه ناقص است -> مشکوک، هشدار بده
        targets = {}
        for lvl_s, price_s in _TARGET_PRICE.findall(text):
            targets[int(lvl_s)] = _num(price_s)
        if len(targets) < 4:
            return None
        return ParsedSignal(
            kind="entry",
            symbol=m.group("symbol"),
            timeframe=m.group("tf"),
            side=m.group("side"),
            entry=_num(entry_m.group(1)),
            stop=_num(stop_m.group(1)),
            targets=targets,
        )

    m = _HEADER_TARGET_HIT.search(text)
    if m:
        return ParsedEvent(
            kind="target_hit",
            symbol=m.group("symbol"),
            timeframe=m.group("tf"),
            level=int(m.group("level")),
        )

    m = _HEADER_STOP.search(text)
    if m:
        return ParsedEvent(kind="stop", symbol=m.group("symbol"), timeframe=m.group("tf"))

    m = _HEADER_BREAKEVEN.search(text)
    if m:
        return ParsedEvent(kind="breakeven", symbol=m.group("symbol"), timeframe=m.group("tf"))

    m = _HEADER_SL_AFTER_T2.search(text)
    if m:
        return ParsedEvent(kind="sl_after_t2", symbol=m.group("symbol"), timeframe=m.group("tf"))

    m = _HEADER_SL_AFTER_T3.search(text)
    if m:
        return ParsedEvent(kind="sl_after_t3", symbol=m.group("symbol"), timeframe=m.group("tf"))

    m = _HEADER_RUNNER_CLOSED.search(text)
    if m:
        return ParsedEvent(kind="runner_closed", symbol=m.group("symbol"), timeframe=m.group("tf"))

    m = _HEADER_FORCED_CLOSE.search(text)
    if m:
        return ParsedEvent(kind="forced_close", symbol=m.group("symbol"), timeframe=m.group("tf"))

    return None
