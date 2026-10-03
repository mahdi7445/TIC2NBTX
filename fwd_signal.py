# -*- coding: utf-8 -*-
"""Parser for a signal the ADMIN forwards (or pastes) into the bot's own chat.

It is deliberately separate from signal_parser.py (which parses the bot's OWN channel format) because
third-party channels write signals in free text, e.g.

    https://www.tradingview.com/x/D3pQrRpT/
    btcusdt.p
    sell limit : entry price : 85956
    stop loss : 87416
    target 1 : 84513
    target 2 : 82000

What is understood (case-insensitive, English and Persian, Persian digits, 85,956 style numbers):
  symbol   BTCUSDT, btcusdt.p, BTC/USDT, BTC-USDT, BTCUSDT PERP, #BTC, $BTC, "BTC long" ...  (USDT markets only)
  side     buy / long / خرید / لانگ   or   sell / short / فروش / شورت. If missing it is inferred from the numbers;
           if stated AND contradicting the numbers the signal is rejected.
  entry    entry, entry price, enter, buy/sell limit <price>, buy/sell at <price>, ورود, قیمت ورود ...
  stop     stop loss, stoploss, SL, S/L, stop, حد ضرر, استاپ ...
  targets  target N, targets: a / b / c, TP, TP1, TP 1, TP-1, T1, take profit N, tgt, حد سود, تارگت, هدف ...
           also a list under a "Targets:" header ("1) 84513", "2. 82000", "- 80000").
  type     limit (default) or market (word market / cmp / now / بازار, entry price may then be omitted).

Everything doubtful is REJECTED with a reason instead of guessed, because a wrong parse means a wrong live order:
a price range as entry, several different stops, a target on the wrong side of the entry, a side that
contradicts the numbers, 'buy stop' / 'sell stop' entries (stop-entries are not supported), absurd distances.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import List, Optional, Tuple

MAX_TARGETS = 6

_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
_URL_RE = re.compile(r"(?:https?://|www\.|t\.me/)\S+", re.IGNORECASE)
_NUM = r"\d+(?:\.\d+)?"
_NUM_RE = re.compile(_NUM)

# a 'stop' that belongs to an order type ("sell stop", "buy stop limit") is NOT a stop-loss label
_STOP_ORDER_RE = re.compile(r"\b(?:buy|sell)\s*[-_]?\s*stop\b(?!\s*[-_]?\s*loss)", re.IGNORECASE)

_ENTRY_RE = re.compile(
    r"(?:\bentry(?:\s*(?:price|zone|point|level|area))?\b|\benter\b|\bentries\b|قیمت\s*ورود|ورودی|\bورود\b"
    r"|\b(?:buy|sell|long|short)\s*(?:limit\s*)?(?:at|@)\b|\b(?:buy|sell)\s+limit\b)", re.IGNORECASE)
_STOP_RE = re.compile(
    r"(?:stop\s*[-_/]?\s*loss|\bstoploss\b|\bs\s*/\s*l\b|(?<![a-z])sl(?![a-z])|(?<![a-z])stop(?![a-z])"
    r"|حد\s*ضرر|حدضرر|استاپ|ضرر)", re.IGNORECASE)
_TP_RE = re.compile(
    r"(?:take\s*[-_]?\s*profits?|\btargets?(?![a-z])|\btgts?(?![a-z])|(?<![a-z])tps?(?![a-z])|(?<![a-z])tp(?=\d)|(?<![a-z0-9])t(?=\d\b)"
    r"|حد\s*سود|تارگت|هدف)\s*[#:.\-]?\s*(?:(\d{1,2})(?![\d.]))?", re.IGNORECASE)
_SHORT_RE = re.compile(r"(?:\bsell\b|\bshort\b|فروش|شورت)", re.IGNORECASE)
_LONG_RE = re.compile(r"(?:\bbuy\b|\blong\b|خرید|لانگ)", re.IGNORECASE)
_MARKET_RE = re.compile(r"(?:\bmarket\b|\bcmp\b|\bnow\b|current\s*price|at\s*market|بازار|قیمت\s*فعلی|همین\s*الان)", re.IGNORECASE)
_LIMIT_RE = re.compile(r"(?:\blimit\b|لیمیت)", re.IGNORECASE)
_SYMBOL_RE = re.compile(r"(?<![A-Za-z0-9])([A-Za-z]{2,10})\s*[/\-_ ]?\s*(?:USDT|USD)(?:\.?P(?:ERP)?)?(?![A-Za-z])", re.IGNORECASE)
_TAG_RE = re.compile(r"(?:#|\$)([A-Za-z]{2,10})(?![A-Za-z0-9])")
_COIN_SIDE_RE = re.compile(r"(?<![A-Za-z0-9])([A-Za-z]{2,6})\s+(?:long|short|buy|sell)\b", re.IGNORECASE)
_NOT_COINS = {"ENTRY", "STOP", "LOSS", "TARGET", "TP", "SL", "LIMIT", "SELL", "BUY", "LONG", "SHORT", "MARKET", "PRICE",
              "TAKE", "PROFIT", "TARGETS", "ZONE", "SIGNAL", "FUTURES", "SPOT", "PERP", "USDT", "USD", "FREE", "VIP",
              "NEW", "THE", "AND", "FOR", "NOW", "CMP", "RISK", "LEVERAGE", "CROSS", "ISOLATED", "SWING", "SCALP"}
_CLEAN_PAREN = re.compile(r"\([^)]*\)|\[[^\]]*\]")
_CLEAN_UNITS = re.compile(r"[+\-]?\d+(?:\.\d+)?\s*(?:%|x\b|×|usdt\b|r\b|pips?\b)", re.IGNORECASE)
_RANGE_RE = re.compile(r"^\s*[:=@\-]*\s*(\d+(?:\.\d+)?)\s*(?:[-–—~/]|to|تا)\s*(\d+(?:\.\d+)?)", re.IGNORECASE)
_LIST_ITEM_RE = re.compile(r"^\W*(?:\d{1,2}\s*[).:\-]\s*)?(\d+(?:\.\d+)?)\W*$")


@dataclass
class FwdSignal:
    symbol: str                      # e.g. BTCUSDT
    side: str                        # LONG | SHORT
    entry: Optional[Decimal]         # None only for a market order without a stated price
    stop: Decimal
    targets: List[Decimal]           # nearest first
    order_type: str = "limit"        # limit | market
    warnings: List[str] = field(default_factory=list)


def _norm(text: str) -> str:
    t = str(text or "").translate(_DIGITS)
    t = _URL_RE.sub(" ", t)
    t = re.sub(r"(?<=\d),(?=\d{3}(?!\d))", "", t)      # 85,956 -> 85956
    t = t.replace("٫", ".").replace("\u200c", " ").replace("\u200f", " ").replace("\u200e", " ")
    return t.replace("：", ":").replace("＠", "@")


def _nums(s: str) -> List[Decimal]:
    out = []
    for m in _NUM_RE.findall(s):
        try:
            out.append(Decimal(m))
        except InvalidOperation:
            pass
    return out


def _clean_seg(seg: str) -> str:
    """Drop noise that sits next to a price: '(+1.7%)', '10x', '2R', '[risk 1%]'."""
    seg = _CLEAN_PAREN.sub(" ", seg)
    return _CLEAN_UNITS.sub(" ", seg)


def _price_after_label(seg: str):
    """(first price, is_range, how_many_distinct_prices)."""
    seg = _clean_seg(seg)
    nums = _nums(seg)
    if not nums:
        return None, False, 0
    m = _RANGE_RE.match(seg)
    if m:
        try:
            a, b = Decimal(m.group(1)), Decimal(m.group(2))
            if a > 0 and abs(a - b) / a < Decimal("0.2") and a != b:
                return nums[0], True, 2
        except InvalidOperation:
            pass
    return nums[0], False, len(set(nums))


def looks_like_signal(text: str) -> bool:
    """Cheap pre-check: a stop-loss word AND (an entry/target/limit/market word) AND numbers."""
    t = _norm(text)
    t = _STOP_ORDER_RE.sub(" ", t)
    if not _NUM_RE.search(t):
        return False
    return bool(_STOP_RE.search(t)) and bool(_ENTRY_RE.search(t) or _TP_RE.search(t) or _LIMIT_RE.search(t) or _MARKET_RE.search(t))


def _find_symbol(t: str) -> Optional[str]:
    for m in _SYMBOL_RE.finditer(t):
        base = m.group(1).upper()
        if base not in _NOT_COINS:
            return base + "USDT"
    for m in _TAG_RE.finditer(t):
        base = m.group(1).upper()
        if base not in _NOT_COINS:
            return base + "USDT"
    for m in _COIN_SIDE_RE.finditer(t):
        base = m.group(1).upper()
        if base not in _NOT_COINS:
            return base + "USDT"
    return None


def parse_forwarded_signal(text: str) -> Tuple[Optional[FwdSignal], str]:
    """Returns (FwdSignal, '') or (None, persian_reason)."""
    t = _norm(text)
    if not t.strip():
        return None, "متن خالی است."
    if _STOP_ORDER_RE.search(t):
        return None, "ورود از نوع Stop (buy stop / sell stop) پشتیبانی نمی‌شود؛ فقط ورود Limit یا Market."
    symbol = _find_symbol(t)
    if not symbol:
        return None, "نماد رمزارز پیدا نشد (مثلاً BTCUSDT یا #BTC)."

    entry_vals: List[Decimal] = []
    stop_vals: List[Decimal] = []
    tps: dict = {}              # explicit index -> price
    loose: List[Decimal] = []   # targets without an index, in order of appearance
    range_seen = False
    multi_entry = False
    in_tp_block = False

    for line in t.splitlines():
        low = line.strip()
        if not low:
            in_tp_block = False
            continue
        found = ([("stop", m) for m in _STOP_RE.finditer(low)] + [("entry", m) for m in _ENTRY_RE.finditer(low)]
                 + [("tp", m) for m in _TP_RE.finditer(low)])
        if in_tp_block and not found:
            li = _LIST_ITEM_RE.match(_clean_seg(low))
            if li:
                loose.append(Decimal(li.group(1)))
                continue
            in_tp_block = False
        spans = sorted([(m.start(), kind, m) for kind, m in found], key=lambda x: (x[0], -(x[2].end() - x[2].start())))
        # two labels that start at the same place ("sell limit" / "stop"...) -> keep the longer one only
        _dedup = []
        for sp in spans:
            if _dedup and sp[0] < _dedup[-1][2].end() and sp[1] == _dedup[-1][1]:
                continue
            _dedup.append(sp)
        spans = _dedup
        for i, (pos, kind, m) in enumerate(spans):
            end = spans[i + 1][0] if i + 1 < len(spans) else len(low)
            seg = low[m.end():end]
            if kind == "tp":
                idx = m.group(1)
                nums = _nums(_clean_seg(seg))
                if idx and idx.isdigit() and nums:
                    tps[int(idx)] = nums[0]              # 'target 1 : 84513 (+1.7%)' -> only the price
                    in_tp_block = False
                elif nums:
                    loose.extend(nums)                   # 'targets: a / b / c' or 'tp 84000'
                    in_tp_block = True
                else:
                    in_tp_block = True                   # 'Targets:' header, prices follow on the next lines
            elif kind == "entry":
                v, is_range, distinct = _price_after_label(seg)
                if v is not None:
                    if is_range:
                        range_seen = True
                    elif distinct > 1:
                        multi_entry = True
                    entry_vals.append(v)
            else:
                v, _r, _d = _price_after_label(seg)
                if v is not None:
                    stop_vals.append(v)

    low_all = t.lower()
    has_market_word = bool(_MARKET_RE.search(t))
    has_limit_word = bool(_LIMIT_RE.search(t))

    if range_seen:
        return None, "قیمت ورود به‌صورت محدوده داده شده؛ برای امنیت فقط یک قیمت ورود پذیرفته می‌شود (با یک عدد دوباره بفرستید)."
    if multi_entry or len(set(entry_vals)) > 1:
        return None, f"برای ورود چند قیمت مختلف داده شده ({', '.join(str(x) for x in entry_vals[:3])})؛ مبهم است."
    if not stop_vals:
        return None, "حد ضرر (stop loss) پیدا نشد."
    if len(set(stop_vals)) > 1:
        return None, "برای حد ضرر چند قیمت مختلف داده شده؛ مبهم است."
    stop = stop_vals[0]
    if stop <= 0:
        return None, "قیمت‌ها باید مثبت باشند."

    entry: Optional[Decimal] = entry_vals[0] if entry_vals else None
    order_type = "limit"
    if entry is None:
        if has_market_word:
            order_type = "market"
        else:
            return None, "قیمت ورود (entry) پیدا نشد (برای ورود بازاری کلمه‌ی market بنویسید)."
    elif has_market_word and not has_limit_word:
        order_type = "market"          # 'market entry 85900': execute now; the number is only the reference
    if entry is not None and entry <= 0:
        return None, "قیمت‌ها باید مثبت باشند."
    if entry is not None and entry == stop:
        return None, "قیمت ورود و حد ضرر برابرند."

    ref = entry if entry is not None else stop
    lo, hi = ref * Decimal("0.3"), ref * Decimal("3")
    raw = [tps[k] for k in sorted(tps)] + loose
    prices = [p for p in raw if lo <= p <= hi]                 # drops stray numbers such as '1' from '1) ...'
    if not prices:
        return None, "هیچ تارگتی (target/TP) پیدا نشد."
    seen, uniq = set(), []
    for p in prices:
        if p not in seen and p > 0:
            seen.add(p)
            uniq.append(p)
    prices = uniq

    # ---- side ----
    has_short = bool(_SHORT_RE.search(t))
    has_long = bool(_LONG_RE.search(t))
    if entry is not None:
        geo_side = "SHORT" if stop > entry else "LONG"
    else:
        geo_side = "SHORT" if stop > max(prices) else ("LONG" if stop < min(prices) else "")
        if not geo_side:
            return None, "جهت معامله از روی حد ضرر و تارگت‌ها مشخص نیست."
    if has_short and has_long:
        side = geo_side            # e.g. 'sell ... buy back' - the numbers decide
    elif has_short:
        side = "SHORT"
    elif has_long:
        side = "LONG"
    else:
        side = geo_side
    if side != geo_side:
        return None, (f"جهت ({'فروش/Short' if side == 'SHORT' else 'خرید/Long'}) با اعداد نمی‌خواند: "
                      f"حد ضرر باید {'بالاتر' if side == 'SHORT' else 'پایین‌تر'} از ورود باشد.")

    # ---- targets must be on the profit side ----
    if entry is not None:
        bad = [p for p in prices if (p >= entry if side == "SHORT" else p <= entry)]
    else:
        bad = [p for p in prices if (p >= stop if side == "SHORT" else p <= stop)]
    if bad:
        return None, (f"تارگت {', '.join(str(x) for x in bad)} سمت سود قیمت ورود نیست "
                      f"({'پایین‌تر' if side == 'SHORT' else 'بالاتر'} از ورود باید باشد).")
    if entry is not None and abs(entry - stop) / entry > Decimal("0.5"):
        return None, "فاصله‌ی حد ضرر از ورود بیش از ۵۰٪ است؛ احتمالاً عدد اشتباه است."
    prices.sort(reverse=(side == "SHORT"))          # nearest first
    warnings: List[str] = []
    if len(prices) > MAX_TARGETS:
        warnings.append(f"فقط {MAX_TARGETS} تارگت اول (نزدیک‌ترین‌ها) استفاده شد.")
        prices = prices[:MAX_TARGETS]
    return FwdSignal(symbol=symbol, side=side, entry=entry, stop=stop, targets=prices,
                     order_type=order_type, warnings=warnings), ""


# Share of the position closed at each target (sums to 100%; no runner for forwarded signals - the
# provider gave fixed targets and a fixed stop, so everything is closed at those prices).
_WEIGHTS = {
    1: ["1"],
    2: ["0.5", "0.5"],
    3: ["0.4", "0.3", "0.3"],
    4: ["0.3", "0.3", "0.2", "0.2"],
    5: ["0.25", "0.25", "0.2", "0.15", "0.15"],
    6: ["0.2", "0.2", "0.2", "0.15", "0.15", "0.1"],
}


def target_weights(n: int) -> dict:
    """{1: Decimal, 2: Decimal, ...} for n targets, always summing to exactly 1."""
    ws = [Decimal(x) for x in _WEIGHTS[max(1, min(int(n), MAX_TARGETS))]]
    assert sum(ws) == Decimal(1)
    return {i + 1: w for i, w in enumerate(ws)}
