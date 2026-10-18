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
from decimal import Decimal
from typing import Optional

# اگر ربات‌ها اعداد را با کاما جدا می‌کنند (مثلاً 63,400.5) این regex هم آن‌ها
# را می‌پذیرد. format_signal_price دقیق ربات را ندیدیم، پس محافظه‌کارانه
# کاما و اعشار هر دو پشتیبانی می‌شوند.
_NUM = r"[\d,]+(?:\.\d+)?"

_HEADER_ENTRY = re.compile(
    r"^(?P<emoji>🟢|🔴)\s*(?P<side>LONG|SHORT)\s*—\s*(?P<symbol>[^\s—]+)(?:[ \t]+(?P<tf>\d{1,3}[MHDW])\b)?",
    re.MULTILINE,
)
_ENTRY_PRICE = re.compile(rf"Entry:\s*(?:<b>)?({_NUM})", re.IGNORECASE)
_STOP_PRICE = re.compile(rf"❌\s*Stop:\s*(?:<b>)?({_NUM})", re.IGNORECASE)
_TARGET_PRICE = re.compile(rf"🎯\s*Target\s*(\d)\s*:\s*(?:<b>)?({_NUM})")

_HEADER_TARGET_HIT = re.compile(
    r"^✅\s*Target\s*(?P<level>\d)\s*HIT\s*\([^)]*\)\s*—\s*(?P<symbol>[^\s—]+)(?:[ \t]+(?P<tf>\d{1,3}[MHDW])\b)?",
    re.MULTILINE,
)
_HEADER_STOP = re.compile(
    r"^❌\s*STOP\s*HIT\s*—\s*(?P<symbol>[^\s—]+)(?:[ \t]+(?P<tf>\d{1,3}[MHDW])\b)?", re.MULTILINE
)
_HEADER_BREAKEVEN = re.compile(
    r"^⚪\s*BREAKEVEN\s*—\s*(?P<symbol>[^\s—]+)(?:[ \t]+(?P<tf>\d{1,3}[MHDW])\b)?", re.MULTILINE
)
_HEADER_SL_AFTER_T2 = re.compile(
    r"^🔒\s*STOP AFTER TARGET 2\s*—\s*(?P<symbol>[^\s—]+)(?:[ \t]+(?P<tf>\d{1,3}[MHDW])\b)?", re.MULTILINE
)
_HEADER_SL_AFTER_T3 = re.compile(
    r"^🔒\s*STOP AFTER TARGET 3\s*—\s*(?P<symbol>[^\s—]+)(?:[ \t]+(?P<tf>\d{1,3}[MHDW])\b)?", re.MULTILINE
)
_HEADER_RUNNER_CLOSED = re.compile(
    r"^🏁\s*RUNNER CLOSED\s*—\s*(?P<symbol>[^\s—]+)(?:[ \t]+(?P<tf>\d{1,3}[MHDW])\b)?", re.MULTILINE
)
_HEADER_FORCED_CLOSE = re.compile(
    r"^⚠️\s*TRADE CLOSED\s*—\s*(?P<symbol>[^\s—]+)(?:[ \t]+(?P<tf>\d{1,3}[MHDW])\b)?", re.MULTILINE
)

# درصد حجمی که در هر رویداد باید "اضافه" بسته شود (نسبت به حجم اولیه‌ی پوزیشن)
TARGET_CLOSE_PCT = {1: 0.20, 2: 0.30, 3: 0.15, 4: 0.10}
RUNNER_CLOSE_PCT = 0.25  # کل مانده بعد از تارگت ۴


# Channel signal IDs (TC-... for the main bot, ALT-... for the altcoin bot).
# The exact position/format inside each channel message was not visible in the
# available logs, so this is deliberately tolerant: first TC-/ALT- token found
# anywhere in the text (optionally after "ID"/"Signal ID"/🆔).
_SIGNAL_ID = re.compile(r"(?<![A-Za-z0-9])((?:TC|ALT)[-_][A-Za-z0-9][A-Za-z0-9_\-]*)", re.IGNORECASE)


def extract_signal_id(text: str) -> Optional[str]:
    """Telegram hashtags cannot contain '-', so the channel now writes '#ALT_000199'. It is normalised to the
    canonical 'ALT-000199' form so an entry and its later events (target hit / breakeven ...) always carry the
    same ID and the executor's 'event belongs to another signal' safety check keeps working."""
    m = _SIGNAL_ID.search(text or "")
    if not m:
        return None
    sid = m.group(1).upper().rstrip("-_")
    prefix = "ALT" if sid.startswith("ALT") else "TC"
    return prefix + "-" + sid[len(prefix) + 1:]


MANUAL_TF = "MANUAL"


def _tf(m) -> str:
    """Timeframe label of a header, or 'MANUAL' when the message names only the symbol. Manual / admin-forwarded
    signals of the main bot are posted without a timeframe ('🔴 SHORT — BTC'), and so are their later events, so the
    same default on both sides gives them the same trade key (BTC_MANUAL)."""
    try:
        tf = m.group("tf")
    except IndexError:
        tf = None
    return (tf or MANUAL_TF).upper()


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
    signal_id: Optional[str] = None  # channel signal ID (TC-/ALT-...) if present in the message
    # --- Strategy-module signals (HDV3 / SWP / TS1 / candle-signal modules ...) describe their OWN plan inside the
    # message: any number of targets, each with its own share, an optional trailing runner and its trail distance.
    # Classic channel signals leave all of these as None and keep the fixed 4-target ladder.
    weights: Optional[dict] = None       # {1: Decimal share of the position, ...} for each target
    runner_pct: Optional[Decimal] = None  # share left for the trailing runner (0 = none)
    trail_r: Optional[Decimal] = None     # runner trail distance, in R
    levels: Optional[dict] = None         # {1: 2.0, ...} target distance in R (informational)
    variant: Optional[str] = None         # short strategy tag (HDV / SWP / TS1 ...) so two strategies never share a trade key
    title: Optional[str] = None           # strategy title as printed in the channel
    ladder: Optional[str] = None          # ladder name (Q1, CLASSIC, B25 ...) as printed in the channel
    tier: Optional[str] = None            # quality tier line (backtest-validated / experimental ...)
    source: Optional[str] = None          # "module" for strategy-module signals


@dataclass
class ParsedEvent:
    kind: str  # "target_hit" | "stop" | "breakeven" | "sl_after_t2" | "sl_after_t3" | "runner_closed" | "forced_close"
    symbol: str
    timeframe: str
    level: Optional[int] = None  # فقط برای target_hit
    new_stop_price: Optional[float] = None  # اگر پیام قیمت استاپ تازه را نشان داده باشد
    signal_id: Optional[str] = None  # channel signal ID if present in the message
    price: Optional[float] = None  # price printed in the message (target price), when there is one
    after_level: Optional[int] = None  # for "stop after T<k>" exits: k
    source: Optional[str] = None  # "module" for strategy-module events

# الگوی محتاطانه برای پیدا کردن قیمت استاپ تازه در متن رویدادهای تنگ‌کردن
# استاپ - چون فرمت دقیق این پیام‌ها هنوز با نمونه‌ی واقعی تایید نشده،
# چند حالت رایج را امتحان می‌کند. اگر هیچ‌کدام مچ نشد، new_stop_price خالی
# می‌ماند و executor.py با احتیاط رفتار می‌کند (نه حدس می‌زند، نه نادیده
# می‌گیرد).
_NEW_STOP_PRICE = re.compile(
    rf"(?:New Stop|Stop moved to|استاپ جدید|استاپ.{{0,10}}جابجا)[^\d]{{0,20}}({_NUM})",
    re.IGNORECASE,
)


def _extract_new_stop_price(text: str) -> Optional[float]:
    m = _NEW_STOP_PRICE.search(text)
    if m:
        return _num(m.group(1))
    return None


def _parse_message_inner(text: str) -> Optional[object]:
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
            timeframe=_tf(m),
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
            timeframe=_tf(m),
            level=int(m.group("level")),
        )

    m = _HEADER_STOP.search(text)
    if m:
        return ParsedEvent(kind="stop", symbol=m.group("symbol"), timeframe=_tf(m))

    m = _HEADER_BREAKEVEN.search(text)
    if m:
        return ParsedEvent(kind="breakeven", symbol=m.group("symbol"), timeframe=_tf(m))

    m = _HEADER_SL_AFTER_T2.search(text)
    if m:
        return ParsedEvent(
            kind="sl_after_t2", symbol=m.group("symbol"), timeframe=_tf(m),
            new_stop_price=_extract_new_stop_price(text),
        )

    m = _HEADER_SL_AFTER_T3.search(text)
    if m:
        return ParsedEvent(
            kind="sl_after_t3", symbol=m.group("symbol"), timeframe=_tf(m),
            new_stop_price=_extract_new_stop_price(text),
        )

    m = _HEADER_RUNNER_CLOSED.search(text)
    if m:
        return ParsedEvent(kind="runner_closed", symbol=m.group("symbol"), timeframe=_tf(m))

    m = _HEADER_FORCED_CLOSE.search(text)
    if m:
        return ParsedEvent(kind="forced_close", symbol=m.group("symbol"), timeframe=_tf(m))

    return None


# =====================================================================================================================
# Strategy-module messages (HDV3 / SWP / TS1 / candle-signal modules and any future strategy built on the same module
# framework). Unlike the classic signals, such a message DESCRIBES ITS OWN TRADE PLAN: how many targets there are, the
# share of the position each one closes, whether a trailing runner is left and how far it trails. The executor reads the
# plan from the message instead of assuming the fixed 4-target ladder, so a new ladder or a new strategy that keeps this
# layout needs no change here. English (TICB) and Persian (SGNL) layouts are both understood.
#
#   🎯 BTC/USD BUY — Hidden Divergence + Inside Bar        🎯 BTC/USD SELL — <title>
#   ⏱ 4H | <tier>                                          ⏱ 4H | <tier>
#   Entry: 67,500.00           (ورود: ...)
#   Stop: 66,000.00 (2.22%)    (حد ضرر: ... (2.22٪))
#   T1 (2R): 70,500.00  [30%]
#   ...
#   Runner 15%: trailing 1.5R  (رانر 15٪: تریلینگ 1.5R)
#   Ladder Q1 — backtest ...   (نردبان Q1 — ...)
#   #TC-000123
#
#   ✅ BTC/USD #TC-000123 — Target 2 hit (70,500.00) | banked +1.50R. Stop moved to 67,500.00.
#   🛑 BTC/USD #TC-000123 — closed (stop loss | break-even | runner trailing stop | stop after T3) | result +0.50R
#   🏁 BTC/USD #TC-000123 — all targets hit | result +2.50R
# =====================================================================================================================
_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩٪٫٬", "01234567890123456789%.,")

_MOD_NUM = r"[\d,]+(?:\.\d+)?"
_MOD_ENTRY_HEADER = re.compile(
    r"^[ \t]*🎯[ \t]*(?P<sym>[A-Za-z0-9._/\-]+)[ \t]+(?P<side>BUY|SELL|LONG|SHORT)\b[ \t]*[—–-][ \t]*(?P<title>[^\n]*)",
    re.MULTILINE | re.IGNORECASE)
_MOD_TF = re.compile(r"⏱[ \t]*(?P<tf>\d{1,3}[MHDWmhdw])\b(?:[ \t]*\|[ \t]*(?P<tier>[^\n]*))?")
_MOD_ENTRY = re.compile(rf"^[ \t]*(?:Entry|ورود)[ \t]*:[ \t]*(?P<v>{_MOD_NUM})", re.MULTILINE | re.IGNORECASE)
_MOD_STOP = re.compile(rf"^[ \t]*(?:Stop|حد[ \t]*ضرر)[ \t]*:[ \t]*(?P<v>{_MOD_NUM})", re.MULTILINE | re.IGNORECASE)
_MOD_TARGET = re.compile(
    rf"^[ \t]*T(?P<n>\d+)[ \t]*\([ \t]*(?P<r>\d+(?:\.\d+)?)[ \t]*R[ \t]*\)[ \t]*:[ \t]*(?P<p>{_MOD_NUM})[ \t]*\[[ \t]*(?P<w>\d+(?:\.\d+)?)[ \t]*%[ \t]*\]",
    re.MULTILINE | re.IGNORECASE)
_MOD_RUNNER = re.compile(
    r"(?:Runner|رانر)[ \t]+(?P<pct>\d+(?:\.\d+)?)[ \t]*%[ \t]*:[ \t]*(?:trailing|تریلینگ)[ \t]*(?P<trail>\d+(?:\.\d+)?)[ \t]*R",
    re.IGNORECASE)
_MOD_LADDER = re.compile(r"^[ \t]*(?:Ladder|نردبان)[ \t]+(?P<name>[A-Za-z0-9_]+)", re.MULTILINE | re.IGNORECASE)

_MOD_EV_HEAD = (r"^[ \t]*{emoji}[ \t]*(?P<sym>[A-Za-z0-9._/\-]+)[ \t]+#(?P<sid>[A-Za-z0-9_\-]+)[ \t]*[—–-][ \t]*")
_MOD_EV_TARGET = re.compile(
    _MOD_EV_HEAD.format(emoji="✅") + rf"(?:Target[ \t]*(?P<n>\d+)[ \t]*hit|تارگت[ \t]*(?P<n2>\d+))[ \t]*\((?P<price>{_MOD_NUM})\)",
    re.MULTILINE | re.IGNORECASE)
_MOD_EV_NEWSTOP = re.compile(
    rf"(?:Stop[ \t]+moved[ \t]+to|حد[ \t]*ضرر[ \t]+به)[ \t]*(?P<v>{_MOD_NUM})", re.IGNORECASE)
_MOD_EV_CLOSED = re.compile(
    _MOD_EV_HEAD.format(emoji="🛑") + r"(?:closed|بسته[ \t]+شد)[ \t]*\((?P<reason>[^)\n]*)\)",
    re.MULTILINE | re.IGNORECASE)
_MOD_EV_DONE = re.compile(
    _MOD_EV_HEAD.format(emoji="🏁") + r"(?:all[ \t]+targets[ \t]+hit|همه[\u200c ]*ی[ \t]+تارگت[\u200c‌ ]*ها[ \t]+خورد)",
    re.MULTILINE | re.IGNORECASE)

_VARIANT_BY_TITLE = (("hidden divergence", "HDV"), ("liquidity sweep", "SWP"), ("two-leg", "TS1"), ("two leg", "TS1"),
                     ("candle signal", "CS"))


def _variant_tag(title: str) -> str:
    """A short, stable tag per strategy so that two different strategies signalling the same coin on the same timeframe
    get two different trade keys (BTC_4H-HDV next to BTC_4H) instead of one swallowing the other."""
    t = (title or "").lower()
    for needle, tag in _VARIANT_BY_TITLE:
        if needle in t:
            return tag
    words = re.findall(r"[A-Za-z0-9]+", title or "")
    return ("".join(w[0].upper() for w in words)[:4]) or "MOD"


def _module_symbol(raw: str) -> str:
    """'BTC/USD' -> 'BTC' (the classic messages name the coin only; the executor trades the USDT market)."""
    sym = (raw or "").upper().strip()
    for suf in ("/USDT", "/USD", "-USDT", "-USD"):
        if sym.endswith(suf) and len(sym) > len(suf):
            return sym[: -len(suf)]
    return sym.replace("/", "")


def _parse_module_entry(text: str) -> Optional[ParsedSignal]:
    h = _MOD_ENTRY_HEADER.search(text)
    if not h:
        return None
    entry_m, stop_m = _MOD_ENTRY.search(text), _MOD_STOP.search(text)
    if not entry_m or not stop_m:
        return None
    entry, stop = _num(entry_m.group("v")), _num(stop_m.group("v"))
    side = "LONG" if h.group("side").upper() in ("BUY", "LONG") else "SHORT"
    targets, weights, levels = {}, {}, {}
    for m in _MOD_TARGET.finditer(text):
        n = int(m.group("n"))
        if n in targets:
            return None                                   # duplicated target number -> ambiguous, refuse
        targets[n] = _num(m.group("p"))
        weights[n] = Decimal(m.group("w")) / Decimal(100)
        levels[n] = float(m.group("r"))
    if not targets or sorted(targets) != list(range(1, len(targets) + 1)):
        return None
    # Sanity: stop on the losing side, every target on the winning side and strictly increasing in distance.
    if side == "LONG":
        if not (stop < entry) or any(p <= entry for p in targets.values()):
            return None
        prices = [targets[n] for n in sorted(targets)]
        if any(b <= a for a, b in zip(prices, prices[1:])):
            return None
    else:
        if not (stop > entry) or any(p >= entry for p in targets.values()):
            return None
        prices = [targets[n] for n in sorted(targets)]
        if any(b >= a for a, b in zip(prices, prices[1:])):
            return None
    total = sum(weights.values(), Decimal(0))
    if total <= 0 or total > Decimal("1.0001"):
        return None
    # The runner takes whatever the targets leave; the printed runner share is only used to cross-check/ fall back.
    remainder = max(Decimal(0), Decimal(1) - total)
    r_m = _MOD_RUNNER.search(text)
    trail = Decimal(r_m.group("trail")) if r_m else None
    printed = (Decimal(r_m.group("pct")) / Decimal(100)) if r_m else Decimal(0)
    runner = remainder if remainder > Decimal("0.0001") else Decimal(0)
    if runner > 0 and trail is None:
        trail = Decimal("1.5")                            # a runner with no printed trail: the framework default
    if runner == 0:
        trail = None
    if r_m and abs(printed - remainder) > Decimal("0.03") and remainder <= Decimal("0.0001"):
        runner, trail = printed, Decimal(r_m.group("trail"))   # shares given do not sum to 100% but a runner is printed
        total = Decimal(1) - runner
        scale = total / sum(weights.values(), Decimal(0))
        weights = {n: w * scale for n, w in weights.items()}
    tf_m = _MOD_TF.search(text)
    tf = (tf_m.group("tf").upper() if tf_m else "")
    title = h.group("title").strip()
    variant = _variant_tag(title)
    lad = _MOD_LADDER.search(text)
    return ParsedSignal(
        kind="entry", symbol=_module_symbol(h.group("sym")),
        timeframe=(f"{tf}-{variant}" if tf else f"MOD-{variant}"), side=side, entry=entry, stop=stop, targets=targets,
        weights=weights, runner_pct=runner, trail_r=trail, levels=levels, variant=variant, title=title,
        ladder=(lad.group("name").upper() if lad else None),
        tier=((tf_m.group("tier") or "").strip() if tf_m else None) or None, source="module")


def _parse_module_event(text: str) -> Optional[ParsedEvent]:
    m = _MOD_EV_TARGET.search(text)
    if m:
        n = int(m.group("n") or m.group("n2"))
        ns = _MOD_EV_NEWSTOP.search(text)
        return ParsedEvent(kind="target_hit", symbol=_module_symbol(m.group("sym")), timeframe=MODULE_EVENT_TF, level=n,
                           new_stop_price=(_num(ns.group("v")) if ns else None), price=_num(m.group("price")), source="module")
    m = _MOD_EV_CLOSED.search(text)
    if m:
        reason = (m.group("reason") or "").strip().lower().replace("\u200c", " ")
        kind, after = "stop", None
        am = re.search(r"(?:after|پس[ \t]+از)[ \t]*t(\d+)", reason)
        if am:
            after = int(am.group(1))
            kind = f"sl_after_t{after}"
        elif "break" in reason or "ریسک" in reason:
            kind = "breakeven"
        elif "runner" in reason or "رانر" in reason:
            kind = "runner_closed"
        return ParsedEvent(kind=kind, symbol=_module_symbol(m.group("sym")), timeframe=MODULE_EVENT_TF, after_level=after, source="module")
    m = _MOD_EV_DONE.search(text)
    if m:
        return ParsedEvent(kind="all_targets_done", symbol=_module_symbol(m.group("sym")), timeframe=MODULE_EVENT_TF, source="module")
    return None


# Strategy-module event messages carry the symbol and the signal id but NOT the timeframe/strategy, so they are matched
# to their trade by the signal id (executor._find_open_trade_for_event).
MODULE_EVENT_TF = "MODULE"


# Invisible characters (zero-width / bidi marks, BOM) that forwards and copies sometimes put in front of the first
# emoji; the header regexes are anchored at line start, so they would silently stop matching.
_INVISIBLE = re.compile("[\u200b\u200c\u200d\u200e\u200f\u202a-\u202e\u2066-\u2069\ufeff]")


def parse_message(text: str) -> Optional[object]:
    """Parse a channel message; also attaches the channel's own signal ID."""
    text = _INVISIBLE.sub("", text or "").translate(_DIGITS)
    parsed = None
    try:
        parsed = _parse_module_entry(text) or _parse_module_event(text)
    except Exception:
        parsed = None
    if parsed is None:
        parsed = _parse_message_inner(text)
    if parsed is not None:
        try:
            parsed.signal_id = extract_signal_id(text)
        except Exception:
            pass
    return parsed
