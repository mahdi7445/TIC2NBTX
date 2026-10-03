# -*- coding: utf-8 -*-
"""Parser for a signal the ADMIN forwards (or pastes) into the bot's own chat.

It is deliberately separate from signal_parser.py (which parses the bot's OWN
channel format) because third-party channels write signals in free text, e.g.

    https://www.tradingview.com/x/D3pQrRpT/
    btcusdt.p
    sell limit : entry price : 85956
    stop loss : 87416
    target 1 : 84513
    target 2 : 82000

Rules (everything is validated; anything doubtful is REJECTED with a reason
instead of guessed, because a wrong parse means a wrong live order):
  * symbol  - BTCUSDT / btcusdt.p / BTC/USDT / #BTC / $BTC ... -> BTCUSDT (USDT markets only)
  * side    - sell/short/فروش/شورت or buy/long/خرید/لانگ; if absent it is inferred from SL vs entry;
              if stated AND it contradicts the numbers, the signal is rejected.
  * entry   - 'entry', 'entry price', 'ورود' ... followed by ONE number (a range is rejected)
  * stop    - 'stop loss', 'sl', 'stop', 'حد ضرر', 'استاپ' ... followed by ONE number
  * targets - 'target N', 'tp N', 'tpN', 'take profit', 'تارگت', 'هدف' ... (one per line, or several
              numbers on one line); sorted nearest-first; every target must be on the profit side.
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

_ENTRY_RE = re.compile(r"(?:\bentry(?:\s*(?:price|zone|point|level))?\b|\benter\b|قیمت\s*ورود|ورودی|\bورود\b)", re.IGNORECASE)
_STOP_RE = re.compile(r"(?:stop\s*[-_]?\s*loss|\bstoploss\b|\bstop\b|\bsl\b|حد\s*ضرر|حدضرر|استاپ)", re.IGNORECASE)
_TP_RE = re.compile(r"(?:take\s*[-_]?\s*profit|\btarget\b|\btargets\b|\btp\b|(?<![a-z])tp(?=\d)|تارگت|هدف)\s*#?\s*(?:(\d{1,2})(?![\d.]))?", re.IGNORECASE)
_SHORT_RE = re.compile(r"(?:\bsell\b|\bshort\b|فروش|شورت)", re.IGNORECASE)
_LONG_RE = re.compile(r"(?:\bbuy\b|\blong\b|خرید|لانگ)", re.IGNORECASE)
_SYMBOL_RE = re.compile(r"(?<![A-Za-z0-9])([A-Za-z]{2,10})\s*[/\-_ ]?\s*(?:USDT|USD)(?:\.?P)?(?![A-Za-z])", re.IGNORECASE)
_TAG_RE = re.compile(r"(?:#|\$)([A-Za-z]{2,10})(?![A-Za-z0-9])")
_NOT_COINS = {"ENTRY", "STOP", "LOSS", "TARGET", "TP", "SL", "LIMIT", "SELL", "BUY", "LONG", "SHORT", "MARKET", "PRICE",
              "TAKE", "PROFIT", "TARGETS", "ZONE", "SIGNAL", "FUTURES", "SPOT", "PERP", "USDT", "USD"}


@dataclass
class FwdSignal:
    symbol: str                      # e.g. BTCUSDT
    side: str                        # LONG | SHORT
    entry: Decimal
    stop: Decimal
    targets: List[Decimal]           # nearest first
    order_type: str = "limit"        # limit | market (informational; entry is always placed as a limit at `entry`)
    warnings: List[str] = field(default_factory=list)


def _norm(text: str) -> str:
    t = str(text or "").translate(_DIGITS)
    t = _URL_RE.sub(" ", t)
    t = re.sub(r"(?<=\d),(?=\d{3}(?!\d))", "", t)      # 85,956 -> 85956
    return t.replace("٫", ".").replace("\u200c", " ").replace("\u200f", " ").replace("\u200e", " ")


def _nums(s: str) -> List[Decimal]:
    out = []
    for m in _NUM_RE.findall(s):
        try:
            out.append(Decimal(m))
        except InvalidOperation:
            pass
    return out


_RANGE_RE = re.compile(r"^\s*[:=@\-]*\s*(\d+(?:\.\d+)?)\s*(?:[-–—~]|to|تا)\s*(\d+(?:\.\d+)?)", re.IGNORECASE)


def _first_price(seg: str):
    """(first number in seg, is_range). 'Stop 87416 (1.7%)' -> (87416, False); '85956 - 86100' -> (85956, True)."""
    nums = _nums(seg)
    if not nums:
        return None, False
    m = _RANGE_RE.match(seg)
    if m:
        try:
            a, b = Decimal(m.group(1)), Decimal(m.group(2))
            if a > 0 and abs(a - b) / a < Decimal("0.2"):
                return nums[0], True
        except InvalidOperation:
            pass
    return nums[0], False


def looks_like_signal(text: str) -> bool:
    """Cheap check used to decide whether a chat message is worth sending to the full parser:
    it mentions a stop-loss AND (an entry or a target) AND contains numbers."""
    t = _norm(text)
    if not _NUM_RE.search(t):
        return False
    return bool(_STOP_RE.search(t)) and bool(_ENTRY_RE.search(t) or _TP_RE.search(t))


def _find_symbol(t: str) -> Optional[str]:
    for m in _SYMBOL_RE.finditer(t):
        base = m.group(1).upper()
        if base not in _NOT_COINS:
            return base + "USDT"
    for m in _TAG_RE.finditer(t):
        base = m.group(1).upper()
        if base not in _NOT_COINS:
            return base + "USDT"
    return None


def parse_forwarded_signal(text: str) -> Tuple[Optional[FwdSignal], str]:
    """Returns (FwdSignal, '') or (None, persian_reason)."""
    t = _norm(text)
    if not t.strip():
        return None, "متن خالی است."
    symbol = _find_symbol(t)
    if not symbol:
        return None, "نماد رمزارز پیدا نشد (مثلاً BTCUSDT یا #BTC)."

    entry_vals: List[Decimal] = []
    stop_vals: List[Decimal] = []
    tps: dict = {}          # explicit index -> price
    loose: List[Decimal] = []   # targets without an index, in order of appearance
    range_seen = False

    for line in t.splitlines():
        low = line.strip()
        if not low:
            continue
        m_stop = _STOP_RE.search(low)
        m_entry = _ENTRY_RE.search(low)
        m_tp = _TP_RE.search(low)
        # a line can start with 'sell limit :' and then carry the real label - use whichever label comes LAST-defined
        # but never let one label swallow another label's number (e.g. 'entry 100 sl 90' on one line).
        spans = sorted([(m.start(), kind, m) for kind, m in (("stop", m_stop), ("entry", m_entry), ("tp", m_tp)) if m])
        for i, (pos, kind, m) in enumerate(spans):
            end = spans[i + 1][0] if i + 1 < len(spans) else len(low)
            seg = low[m.end():end]
            if kind == "tp":
                idx = m.group(1)
                nums = _nums(seg)
                if idx and idx.isdigit() and nums:
                    tps[int(idx)] = nums[0]          # 'target 1 : 84513 (+1.7%)' -> only the price
                else:
                    loose.extend(nums)               # 'targets: a / b / c' or 'tp 84000'
            elif kind == "entry":
                v, is_range = _first_price(seg)
                if v is not None:
                    if is_range:
                        range_seen = True
                    entry_vals.append(v)
            else:
                v, _r = _first_price(seg)
                if v is not None:
                    stop_vals.append(v)

    if not entry_vals:
        return None, "قیمت ورود (entry) پیدا نشد."
    if range_seen:
        return None, "قیمت ورود به‌صورت محدوده داده شده؛ برای امنیت فقط یک قیمت ورود پذیرفته می‌شود."
    if len(set(entry_vals)) > 1:
        return None, f"برای ورود چند قیمت/محدوده داده شده ({', '.join(str(x) for x in entry_vals[:3])})؛ برای امنیت فقط یک قیمت ورود پذیرفته می‌شود."
    if not stop_vals:
        return None, "حد ضرر (stop loss) پیدا نشد."
    if len(set(stop_vals)) > 1:
        return None, "برای حد ضرر چند قیمت داده شده؛ مبهم است."
    entry, stop = entry_vals[0], stop_vals[0]
    if entry <= 0 or stop <= 0:
        return None, "قیمت‌ها باید مثبت باشند."
    if entry == stop:
        return None, "قیمت ورود و حد ضرر برابرند."

    # keep only price-like numbers (drops '+1.7%' style extras that sit on a target line)
    lo, hi = entry * Decimal("0.3"), entry * Decimal("3")
    prices = [p for p in ([tps[k] for k in sorted(tps)] + loose) if lo <= p <= hi]
    if not prices:
        return None, "هیچ تارگتی (target/TP) پیدا نشد."
    # the same price listed twice is a typo/duplicate, not two targets
    seen, uniq = set(), []
    for p in prices:
        if p not in seen and p > 0:
            seen.add(p)
            uniq.append(p)
    prices = uniq

    # ---- side ----
    has_short = bool(_SHORT_RE.search(t))
    has_long = bool(_LONG_RE.search(t))
    geo_side = "SHORT" if stop > entry else "LONG"
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
    if side == "SHORT":
        bad = [p for p in prices if p >= entry]
    else:
        bad = [p for p in prices if p <= entry]
    if bad:
        return None, (f"تارگت {', '.join(str(x) for x in bad)} سمت سود قیمت ورود نیست "
                      f"({'پایین‌تر' if side == 'SHORT' else 'بالاتر'} از ورود باید باشد).")
    prices.sort(reverse=(side == "SHORT"))          # nearest first
    warnings: List[str] = []
    if len(prices) > MAX_TARGETS:
        warnings.append(f"فقط {MAX_TARGETS} تارگت اول (نزدیک‌ترین‌ها) استفاده شد.")
        prices = prices[:MAX_TARGETS]

    low_all = t.lower()
    order_type = "market" if re.search(r"\bmarket\b|\bcmp\b|بازار", low_all) and "limit" not in low_all else "limit"
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
