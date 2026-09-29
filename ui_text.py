"""Message presentation for the Auto Trade bot (pure functions, no I/O).

Every trade-related message a user or the admin reads is built here so the
look stays consistent: a green circle for buy (LONG) trades, a red circle for
sell (SHORT) trades, a fixed divider, one fact per line, results with an
explicit win/loss/flat marker. Nothing here talks to the exchange.
"""
from __future__ import annotations

import time
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional

DIVIDER = "━━━━━━━━━━━━━━"

# Why a trade ended, in words a subscriber understands. Unknown codes fall
# back to the code itself so nothing is ever hidden.
REASON_FA = {
    "closed_on_exchange": "بسته‌شدن روی نوبیتکس (حد ضرر/سود خود سفارش‌ها)",
    "closed_externally": "بسته‌شدن خارج از ربات",
    "admin_close": "بستن دستی",
    "admin_close_all": "بستن دستی همه‌ی معاملات",
    "confirmed_close": "بستن طبق تأیید",
    "channel_breakeven": "خروج طبق کانال: رسیدن به Breakeven",
    "channel_sl_after_t2": "خروج طبق کانال: SL بعد از تارگت ۲",
    "channel_sl_after_t3": "خروج طبق کانال: SL بعد از تارگت ۳",
    "channel_stop": "خروج طبق کانال: خوردن حد ضرر",
    "channel_runner_closed": "خروج طبق کانال: بسته‌شدن Runner",
    "channel_forced_close": "خروج طبق کانال: بستن اجباری",
    "breakeven": "خروج طبق کانال: رسیدن به Breakeven",
    "sl_after_t2": "خروج طبق کانال: SL بعد از تارگت ۲",
    "sl_after_t3": "خروج طبق کانال: SL بعد از تارگت ۳",
    "stop": "خروج طبق کانال: خوردن حد ضرر",
    "runner_closed": "خروج طبق کانال: بسته‌شدن Runner",
    "forced_close": "خروج طبق کانال: بستن اجباری",
}


def side_emoji(side: Optional[str]) -> str:
    s = str(side or "").upper()
    return "🟢" if s == "LONG" else ("🔴" if s == "SHORT" else "⚪")


def side_fa(side: Optional[str]) -> str:
    s = str(side or "").upper()
    return "خرید (Long)" if s == "LONG" else ("فروش (Short)" if s == "SHORT" else "؟")


def _dec(x: Any) -> Optional[Decimal]:
    try:
        return Decimal(str(x))
    except (InvalidOperation, TypeError, ValueError):
        return None


def num(x: Any, max_decimals: int = 6) -> str:
    """Readable number: no trailing zeros, thousands separators when large."""
    d = _dec(x)
    if d is None:
        return "؟"
    q = d.quantize(Decimal(1).scaleb(-max_decimals)) if max_decimals >= 0 else d
    s = format(q, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    ip, _, fp = s.partition(".")
    if abs(d) >= 1000:
        ip = f"{int(ip):,}"
    return ip + (("." + fp) if fp else "")


def signed(x: Any, decimals: int = 2) -> str:
    d = _dec(x)
    if d is None:
        return "؟"
    q = d.quantize(Decimal(1).scaleb(-decimals))
    return f"+{q}" if q > 0 else f"{q}"


def pnl_marker(x: Any) -> str:
    d = _dec(x)
    if d is None:
        return "⚪"
    return "🟢" if d > 0 else ("🔴" if d < 0 else "⚪")


def title(key: str, side: Optional[str]) -> str:
    sym, _, tf = str(key).partition("_")
    return f"{side_emoji(side)} {sym} · {tf}  ({side_fa(side)})"


def targets_line(trade: Dict[str, Any]) -> str:
    tg = trade.get("targets") or {}
    return "  ".join(f"T{n} {'✅' if (tg.get(str(n)) or {}).get('hit') else '⏳'}" for n in range(1, 5))


def card_signal_received(key: str, side: str, entry, stop, leverage, risk_usdt, collateral,
                         margin_available, signal_id: Optional[str], detailed: bool) -> str:
    lines = ["📥 سیگنال جدید", title(key, side), DIVIDER,
             f"🎯 ورود سیگنال: {num(entry)}", f"🛑 حد ضرر: {num(stop)}",
             f"⚙️ اهرم: {leverage}x   |   ریسک این معامله: {num(risk_usdt, 4)} USDT"]
    if detailed:
        lines.append(f"💰 وثیقه‌ی لازم: ≈{num(collateral, 2)} USDT   |   موجودی آزاد: {num(margin_available, 2)} USDT")
    if signal_id:
        lines.append(f"🆔 {signal_id}")
    lines.append("⏳ در حال باز کردن معامله و ثبت حد ضرر/تارگت‌ها...")
    return "\n".join(lines)


def card_opened(key: str, side: str, entry, stop, leverage, leverage_note: str, targets: Dict[str, Any],
                runner_stop, effective_risk, liability, asset: str, signal_id: Optional[str],
                position_id, liquidation_note: str = "") -> str:
    lines = ["✅ معامله باز شد", title(key, side), DIVIDER,
             f"🎯 قیمت ورود: {num(entry)}", f"🛑 حد ضرر: {num(stop)}",
             f"⚙️ اهرم: {leverage}x{leverage_note}",
             f"📦 حجم: {num(liability, 6)} {asset}   |   ریسک واقعی: ≈{num(effective_risk, 2)} USDT", "",
             "🎯 تارگت‌ها:"]
    for n in range(1, 5):
        t = targets.get(str(n)) or {}
        lines.append(f"   T{n}  {num(t.get('tp_price'))}   ({(_dec(t.get('pct')) or Decimal(0)) * 100:.0f}٪ حجم)")
    lines.append(f"   🏃 Runner (۲۵٪ حجم): حد ضرر متحرک از {num(runner_stop)}")
    lines.append(DIVIDER)
    if signal_id:
        lines.append(f"🆔 {signal_id}")
    lines.append(f"شناسه‌ی پوزیشن: {position_id}")
    lines.append("🛡 حد ضرر و تارگت‌ها روی خود نوبیتکس ثبت و تأیید شدند. "
                 "«غیرفعال (Inactive)» بودن آن‌ها در اپ نوبیتکس طبیعی است (تا رسیدن قیمت به تریگر).")
    if liquidation_note:
        lines.append(liquidation_note.strip())
    return "\n".join(lines)


def card_target_hit(key: str, side: str, n: int, banked_pct, tp_price, note: str = "") -> str:
    pct = (_dec(banked_pct) or Decimal(0)) * 100
    text = "\n".join([f"🎯 تارگت {n} خورد!", title(key, side), DIVIDER,
                      f"💵 قیمت: {num(tp_price)}", f"💰 {pct:.0f}٪ از حجم سود گرفته شد",
                      "🛡 حد ضرر باقیمانده طبق استراتژی جابه‌جا می‌شود."])
    return text + (("\n" + note.strip()) if note else "")


def card_stop_moved(key: str, side: str, new_stop, why: str = "") -> str:
    return "\n".join(["🛡 حد ضرر جابه‌جا شد", title(key, side), DIVIDER,
                      f"🛑 حد ضرر جدید: {num(new_stop)}"] + ([why] if why else []))


def card_trailing(key: str, side: str, last, peak, new_stop, remaining, note: str = "") -> str:
    text = "\n".join(["📈 حد ضرر متحرک Runner به‌روز شد", title(key, side), DIVIDER,
                      f"💹 قیمت فعلی: {num(last)}   |   اوج: {num(peak)}",
                      f"🛑 حد ضرر جدید: {num(new_stop)}", f"📦 حجم باقیمانده: {num(remaining, 6)}"])
    return text + (("\n" + note.strip()) if note else "")


def card_closed(key: str, side: str, reason_code: str, entry, exit_price, realized_usdt, r_multiple,
                pnl_exact: bool, targets_line_text: str, signal_id: Optional[str], opened_at=None, closed_at=None,
                note: str = "") -> str:
    d = _dec(realized_usdt)
    if d is None:
        verdict, res = "⚪ نتیجه", "نامشخص"
    elif d > 0:
        verdict, res = "✅ سود", f"{signed(d)} USDT"
    elif d < 0:
        verdict, res = "❌ ضرر", f"{signed(d)} USDT"
    else:
        verdict, res = "⚪ سر به سر", "0 USDT"
    r = _dec(r_multiple)
    lines = ["🏁 معامله بسته شد", title(key, side), DIVIDER,
             f"{verdict}: {res}" + (f"  ({signed(r)}R)" if r is not None else "")
             + ("" if pnl_exact else "  (تخمینی)"),
             f"↪️ ورود: {num(entry)}   |   خروج: {num(exit_price)}" if exit_price is not None else f"↪️ ورود: {num(entry)}",
             f"📌 دلیل: {REASON_FA.get(reason_code, reason_code)}", f"🎯 {targets_line_text}"]
    if opened_at and closed_at:
        mins = int((float(closed_at) - float(opened_at)) / 60)
        lines.append(f"⏱ مدت: {mins // 60} ساعت و {mins % 60} دقیقه" if mins >= 60 else f"⏱ مدت: {mins} دقیقه")
    if signal_id:
        lines.append(f"🆔 {signal_id}")
    if note:
        lines.append(note)
    return "\n".join(lines)


def card_auto_exit(key: str, side: str, event_label: str) -> str:
    return "\n".join(["🤖 اجرای خودکار طبق کانال", title(key, side), DIVIDER,
                      f"📡 رویداد: {event_label}", "در حال بستن باقیمانده‌ی معامله با قیمت بازار..."])


def card_position(key: str, t: Dict[str, Any], live: Dict[str, Any], liquidation_note: str = "") -> str:
    pnl = live.get("unrealizedPNL")
    runner = "🏃 Runner: حد ضرر متحرک فعال" if t.get("trailing_active") else "🏃 Runner: در انتظار"
    lev = t.get("leverage", "?")
    lev_live = live.get("leverage")
    lev_txt = f"{lev}x"
    if lev_live not in (None, "", "0", 0) and str(lev_live) != str(lev):
        lev_txt += f" (⚠️ نوبیتکس الان {lev_live}x گزارش می‌دهد)"
    lines = [title(key, t.get("side")),
             f"🎯 ورود: {num(t.get('entry_actual'))}   |   🛑 حد ضرر فعلی: {num(t.get('stop_price'))}",
             f"⚙️ اهرم: {lev_txt}   |   📦 حجم: {num(live.get('liability', t.get('initial_amount')), 6)}",
             f"🎯 {targets_line(t)}", runner]
    if pnl is not None:
        lines.append(f"{pnl_marker(pnl)} سود/زیان لحظه‌ای: {signed(pnl, 4)} USDT")
    if t.get("signal_id"):
        lines.append(f"🆔 {t.get('signal_id')}")
    if liquidation_note:
        lines.append(liquidation_note.strip())
    return "\n".join(lines)


def card_positions(cards: List[str]) -> str:
    return f"📊 معاملات باز ({len(cards)})\n{DIVIDER}\n" + f"\n{DIVIDER}\n".join(cards)


def card_history_item(h: Dict[str, Any]) -> str:
    key = str(h.get("key", "?"))
    usdt = h.get("realized_usdt") if h.get("realized_usdt") is not None else h.get("realized_usdt_estimate")
    exact = h.get("realized_usdt") is not None
    r = h.get("r_multiple") if h.get("r_multiple") is not None else h.get("r_multiple_estimate")
    closed = h.get("closed_at")
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(closed)) if closed else "؟"
    hit = ", ".join(h.get("targets_hit") or []) or "هیچ‌کدام"
    line1 = f"{pnl_marker(usdt)} {title(key, h.get('side'))}"
    line2 = (f"   {signed(usdt)} USDT" + (f" ({signed(r)}R)" if _dec(r) is not None else "")
             + ("" if exact else " (تخمینی)")) if _dec(usdt) is not None else "   نتیجه نامشخص"
    return f"{line1}\n{line2}\n   🎯 {hit}   |   📌 {REASON_FA.get(h.get('reason'), h.get('reason'))}\n   🕒 {when}"
