"""Message presentation for the Auto Trade bot (pure functions, no I/O).

Every message a subscriber or the admin reads is built here so the look stays
consistent: a green circle for buy (LONG) trades, a red circle for sell (SHORT)
trades, a fixed divider, one fact per line, results with an explicit
win/loss/flat marker. Nothing here talks to the exchange or to Telegram.

Button contract (shared with executor.py and telegram-github-bridge/bridge.py):
  "cmd:/name args"  -> the command is relayed to the executor
  "ask:<prompt>[:arg]" -> the bridge asks the person for a value, then relays the command
  "menu"            -> admin main menu (drawn by the bridge)
"""
from __future__ import annotations

import time
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional

DIVIDER = "━━━━━━━━━━━━━━"
BRAND = "TRADE IS COOL"

# Why a trade ended, in words a subscriber understands. Unknown codes fall
# back to the code itself so nothing is ever hidden.
REASON_FA = {
    "closed_on_exchange": "بسته‌شدن روی نوبیتکس (حد ضرر/سود خود سفارش‌ها)",
    "closed_externally": "بسته‌شدن خارج از ربات",
    "stop_guard": "حد ضرر فعال شد؛ کل حجم باقی‌مانده بسته شد 🛡",
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
    "user_close": "بستن دستی توسط شما",
    "user_close_all": "بستن همه‌ی معاملات توسط شما",
}


# --------------------------------------------------------------------------- basics
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
    return ip + ((("." + fp)) if fp else "")


def price(x: Any) -> str:
    """Price with sensible precision: 2 decimals for big prices, up to 8 for tiny ones."""
    d = _dec(x)
    if d is None:
        return "؟"
    return num(d, 2 if abs(d) >= 100 else (4 if abs(d) >= 1 else 8))


def qty(x: Any) -> str:
    return num(x, 8)


def signed(x: Any, decimals: int = 2) -> str:
    d = _dec(x)
    if d is None:
        return "؟"
    q = d.quantize(Decimal(1).scaleb(-decimals))
    return f"+{q}" if q > 0 else f"{q}"


def usdt(x: Any, decimals: int = 2) -> str:
    return f"{signed(x, decimals)} USDT"


def pnl_marker(x: Any) -> str:
    d = _dec(x)
    if d is None:
        return "⚪"
    return "🟢" if d > 0 else ("🔴" if d < 0 else "⚪")


def title(key: str, side: Optional[str]) -> str:
    sym, _, tf = str(key).partition("_")
    return f"{side_emoji(side)} {sym} · {tf}  ({side_fa(side)})"


def fmt_time(ts: Any, with_date: bool = True) -> str:
    try:
        t = float(ts)
    except (TypeError, ValueError):
        return "؟"
    return time.strftime("%Y-%m-%d %H:%M" if with_date else "%H:%M", time.localtime(t))


def fmt_duration(opened_at: Any, closed_at: Any) -> str:
    try:
        mins = int((float(closed_at) - float(opened_at)) / 60)
    except (TypeError, ValueError):
        return ""
    if mins < 0:
        return ""
    if mins >= 1440:
        return f"{mins // 1440} روز و {(mins % 1440) // 60} ساعت"
    if mins >= 60:
        return f"{mins // 60} ساعت و {mins % 60} دقیقه"
    return f"{mins} دقیقه"


def targets_line(trade: Dict[str, Any]) -> str:
    tg = trade.get("targets") or {}
    return "  ".join(f"T{n} {'✅' if (tg.get(str(n)) or {}).get('hit') else '⏳'}" for n in range(1, 5))


def clip(text: str, limit: int = 3900) -> str:
    return text if len(text) <= limit else text[: limit - 20].rstrip() + "\n…(ادامه دارد)"


# --------------------------------------------------------------------------- trade cards
def card_signal_received(key: str, side: str, entry, stop, leverage, risk_usdt, collateral,
                         margin_available, signal_id: Optional[str], detailed: bool,
                         low_balance_note: str = "") -> str:
    lines = ["📥 سیگنال جدید", title(key, side), DIVIDER,
             f"🎯 ورود سیگنال: {price(entry)}", f"🛑 حد ضرر: {price(stop)}",
             f"⚙️ اهرم: {leverage}x   |   ریسک این معامله: {num(risk_usdt, 4)} USDT"]
    if detailed:
        lines.append(f"💰 وثیقه‌ی لازم: ≈{num(collateral, 2)} USDT   |   موجودی آزاد: {num(margin_available, 2)} USDT")
    if low_balance_note:
        lines.append(low_balance_note)
    if signal_id:
        lines.append(f"🆔 {signal_id}")
    lines.append("⏳ در حال باز کردن معامله و ثبت حد ضرر/تارگت‌ها...")
    return "\n".join(lines)


def liq_gap_line(side: Optional[str], stop, liq, warn_pct: Decimal = Decimal("5")) -> tuple:
    """(text, is_close). Distance between the stop and the liquidation price."""
    s, q = _dec(stop), _dec(liq)
    if s is None or q is None or s <= 0 or q <= 0:
        return "", False
    gap = ((s - q) / q * 100) if str(side).upper() == "LONG" else ((q - s) / q * 100)
    safe_side = (s > q) if str(side).upper() == "LONG" else (s < q)
    if not safe_side:
        return (f"🚨 حد ضرر ({price(s)}) پشت قیمت لیکویید ({price(q)}) است؛ ممکن است پوزیشن قبل از حد ضرر لیکویید شود!", True)
    if gap < warn_pct:
        return (f"⚠️ فاصله‌ی حد ضرر تا لیکویید ({price(q)}) فقط ٪{gap:.1f} است — کم است.", True)
    return (f"🧯 فاصله‌ی حد ضرر تا لیکویید ({price(q)}): ٪{gap:.1f} — مناسب.", False)


def card_opened(key: str, side: str, entry, stop, leverage, leverage_note: str, targets: Dict[str, Any],
                runner_stop, effective_risk, liability, asset: str, signal_id: Optional[str],
                position_id, liquidation_note: str = "", collateral=None, runner_pct=None, fixed_stop: bool = False) -> str:
    lines = ["✅ معامله باز شد", title(key, side), DIVIDER,
             f"🎯 قیمت ورود: {price(entry)}", f"🛑 حد ضرر: {price(stop)}",
             f"⚙️ اهرم: {leverage}x{leverage_note}",
             f"📦 حجم: {qty(liability)} {asset}   |   ریسک واقعی: ≈{num(effective_risk, 2)} USDT"]
    if collateral is not None:
        lines.append(f"💵 مبلغ تعهد (وثیقه): ≈{num(collateral, 2)} USDT")
    lines += ["", "🎯 تارگت‌ها:"]
    for n in sorted(int(k) for k in targets):
        t = targets.get(str(n)) or {}
        lines.append(f"   T{n}  {price(t.get('tp_price'))}   ({(_dec(t.get('pct')) or Decimal(0)) * 100:.0f}٪ حجم)")
    rp = _dec(runner_pct) if runner_pct is not None else Decimal("0.25")
    if rp is not None and rp > 0:
        lines.append(f"   🏃 Runner ({rp * 100:.0f}٪ حجم): حد ضرر متحرک از {price(runner_stop)}")
    elif fixed_stop:
        lines.append("   🔒 حد ضرر ثابت می‌ماند (همان که در سیگنال بود)؛ هر بخش در تارگت خودش بسته می‌شود.")
    lines.append(DIVIDER)
    if signal_id:
        lines.append(f"🆔 {signal_id}")
    lines.append(f"شناسه‌ی پوزیشن: {position_id}")
    lines.append("🛡 حد ضرر و تارگت‌ها روی خود نوبیتکس ثبت و تأیید شدند. "
                 "«غیرفعال (Inactive)» بودن آن‌ها در اپ نوبیتکس طبیعی است (تا رسیدن قیمت به تریگر).")
    lines.append("🛑 اگر حد ضرر بخورد، ربات کل حجم باقی‌مانده را می‌بندد؛ چیزی نیمه‌باز نمی‌ماند.")
    if liquidation_note:
        lines.append(liquidation_note.strip())
    return "\n".join(lines)


def card_target_hit(key: str, side: str, n: int, banked_pct, tp_price, note: str = "",
                    slice_pnl=None, realized_total=None) -> str:
    pct = (_dec(banked_pct) or Decimal(0)) * 100
    lines = [f"🎯 تارگت {n} خورد!", title(key, side), DIVIDER,
             f"💵 قیمت: {price(tp_price)}", f"💰 {pct:.0f}٪ از حجم سود گرفته شد"]
    if slice_pnl is not None:
        lines.append(f"{pnl_marker(slice_pnl)} سود این بخش: {usdt(slice_pnl, 4)}")
    if realized_total is not None:
        lines.append(f"🧮 مجموع سود ثبت‌شده‌ی این معامله تا این لحظه: {usdt(realized_total, 4)}")
    lines.append("🛡 حد ضرر باقیمانده طبق استراتژی جابه‌جا می‌شود.")
    return "\n".join(lines) + ((("\n" + note.strip())) if note else "")


def card_stop_moved(key: str, side: str, new_stop, why: str = "") -> str:
    return "\n".join(["🛡 حد ضرر جابه‌جا شد", title(key, side), DIVIDER,
                      f"🛑 حد ضرر جدید: {price(new_stop)}"] + ([why] if why else []))


def card_trailing(key: str, side: str, last, peak, new_stop, remaining, note: str = "") -> str:
    text = "\n".join(["📈 حد ضرر متحرک Runner به‌روز شد", title(key, side), DIVIDER,
                      f"💹 قیمت فعلی: {price(last)}   |   اوج: {price(peak)}",
                      f"🛑 حد ضرر جدید: {price(new_stop)}", f"📦 حجم باقیمانده: {qty(remaining)}"])
    return text + ((("\n" + note.strip())) if note else "")


def _fill_lines(fills: List[Dict[str, Any]], start_total: Decimal = Decimal(0)) -> tuple:
    """Numbered, line-by-line fills with the running total after each one.
    Returns (lines, total)."""
    out: List[str] = []
    running = start_total
    for i, f in enumerate(fills, 1):
        p = _dec(f.get("pnl")) or Decimal(0)
        running += p
        est = "" if f.get("exact", True) else " (تخمینی)"
        out.append(f"{i}) {f.get('label', '')}{est}")
        amt, px = _dec(f.get("amount")), _dec(f.get("price"))
        if amt is not None and px is not None:
            out.append(f"    {qty(amt)} × {price(px)}  →  {pnl_marker(p)} {signed(p, 4)}")
        else:
            out.append(f"    {pnl_marker(p)} {signed(p, 4)}")
        out.append(f"    🧮 مجموع این معامله تا اینجا: {signed(running, 4)} USDT")
    return out, running


def card_closed_entry(h: Dict[str, Any], cum_total=None, note: str = "") -> str:
    """The closing report: every step on its own line with the running total,
    then the trade result and the account's all-time total."""
    key, side = str(h.get("key", "?")), h.get("side")
    d = _dec(h.get("realized_usdt") if h.get("realized_usdt") is not None else h.get("realized_usdt_estimate"))
    exact = h.get("realized_usdt") is not None
    r = _dec(h.get("r_multiple") if h.get("r_multiple") is not None else h.get("r_multiple_estimate"))
    lines = ["🏁 معامله بسته شد", title(key, side), DIVIDER]
    head = f"🎯 ورود: {price(h.get('entry_actual'))}"
    if h.get("exit_price") is not None:
        head += f"   |   خروج: {price(h.get('exit_price'))}"
    lines.append(head)
    if h.get("initial_amount"):
        lines.append(f"📦 حجم اولیه: {qty(h.get('initial_amount'))}   |   ⚙️ اهرم: {h.get('leverage', '?')}x")
    lines.append(f"📌 دلیل: {REASON_FA.get(h.get('reason'), h.get('reason'))}")
    hit = set(h.get("targets_hit") or [])
    lines.append("🎯 " + "  ".join(f"T{n} {'✅' if f'T{n}' in hit else '⏳'}" for n in range(1, 5)))
    fills = h.get("fills") or []
    if fills:
        lines += ["", "📋 گزارش سطر به سطر:"]
        fl, _ = _fill_lines(fills)
        lines += fl
    lines.append(DIVIDER)
    if d is None:
        lines.append("⚪ نتیجه: نامشخص")
    else:
        verdict = "✅ سود نهایی" if d > 0 else ("❌ زیان نهایی" if d < 0 else "⚪ سر به سر")
        lines.append(f"{verdict}: {usdt(d, 4)}" + (f"  ({signed(r)}R)" if r is not None else "")
                     + ("" if exact else "  (تخمینی)"))
    if cum_total is not None:
        lines.append(f"🧮 مجموع کل سود/زیان حساب تا این لحظه: {usdt(cum_total, 4)}")
    dur = fmt_duration(h.get("opened_at"), h.get("closed_at"))
    if dur:
        lines.append(f"⏱ مدت معامله: {dur}")
    if h.get("signal_id"):
        lines.append(f"🆔 {h.get('signal_id')}")
    if note and note not in ("closed via market order",):
        n2 = note if not note.startswith(("confirmed closed", "Nobitex reports", "not active", "liability", "closed at", "closed via")) else ""
        if n2:
            lines.append(n2)
    return clip("\n".join(lines))


def card_closed(key: str, side: str, reason_code: str, entry, exit_price, realized_usdt, r_multiple,
                pnl_exact: bool, targets_line_text: str, signal_id: Optional[str], opened_at=None, closed_at=None,
                note: str = "") -> str:
    """Legacy short card (kept for compatibility)."""
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
             f"{verdict}: {res}" + (f"  ({signed(r)}R)" if r is not None else "") + ("" if pnl_exact else "  (تخمینی)"),
             f"↪️ ورود: {price(entry)}   |   خروج: {price(exit_price)}" if exit_price is not None else f"↪️ ورود: {price(entry)}",
             f"📌 دلیل: {REASON_FA.get(reason_code, reason_code)}", f"🎯 {targets_line_text}"]
    if signal_id:
        lines.append(f"🆔 {signal_id}")
    if note:
        lines.append(note)
    return "\n".join(lines)


def card_auto_exit(key: str, side: str, event_label: str) -> str:
    return "\n".join(["🤖 اجرای خودکار طبق کانال", title(key, side), DIVIDER,
                      f"📡 رویداد: {event_label}", "در حال بستن باقیمانده‌ی معامله با قیمت بازار..."])


def msg_stop_guard(key: str, side: str, last, stop, liability) -> str:
    return "\n".join(["🛑 حد ضرر فعال شد", title(key, side), DIVIDER,
                      f"💹 قیمت الان: {price(last)}   |   حد ضرر: {price(stop)}",
                      f"📦 حجمی که هنوز باز بود: {qty(liability)}",
                      "🛡 ربات همه‌ی سفارش‌های باقی‌مانده را لغو می‌کند و کل حجم را همین حالا با قیمت بازار می‌بندد؛ "
                      "چیزی نیمه‌باز نمی‌ماند. گزارش کامل بعد از بسته شدن می‌آید."])


def msg_stop_partial(key: str, side: str, actual, expected) -> str:
    return "\n".join(["🛑 حد ضرر روی بخشی از معامله اجرا شد", title(key, side), DIVIDER,
                      f"📦 حجم باز الان: {qty(actual)}   (طبق برنامه باید {qty(expected)} می‌بود)",
                      "🛡 باقیمانده هم همین حالا بسته می‌شود تا معامله کامل تمام شود."])


def msg_flatten_stuck(key: str, minutes: float, error: str = "") -> str:
    return "\n".join(["🚨 بستن کامل معامله هنوز تمام نشده", DIVIDER,
                      f"معامله: {key}",
                      f"⏱ {minutes:.0f} دقیقه است تلاش برای بستن ادامه دارد."
                      + (f"\nآخرین خطا: {error}" if error else ""),
                      "ربات هر چند ثانیه دوباره تلاش می‌کند. اگر می‌خواهید همین الان دوباره امتحان شود، دکمه‌ی زیر را بزنید."])


def msg_low_balance(need, have, what: str = "این کار", spot=None) -> str:
    lines = ["💰 موجودی کم است", DIVIDER,
             f"برای {what} حدود {num(need, 2)} USDT لازم است، ولی موجودی آزاد کیف پول تعهدی (Margin) شما "
             f"{num(have, 2)} USDT است."]
    if spot is not None and _dec(spot) is not None and _dec(spot) > 0:
        lines.append(f"ℹ️ در کیف پول اسپات {num(spot, 2)} USDT دارید؛ می‌توانید در اپ نوبیتکس آن را به کیف پول تعهدی (Margin) منتقل کنید.")
    else:
        lines.append("برای ادامه، از اپ نوبیتکس به کیف پول تعهدی (Margin) خود USDT اضافه کنید.")
    lines.append("بعد از شارژ، همین دکمه را دوباره بزنید. تا آن موقع هیچ چیزی تغییر نکرده و معاملات فعلی‌تان سر جایشان هستند.")
    return "\n".join(lines)


def low_balance_signal_note(balance, cap, used) -> str:
    return (f"⚠️ موجودی آزاد کم است ({num(balance, 2)} USDT): سقف وثیقه‌ی شما {num(cap, 2)} بود، ولی برای اینکه "
            f"بقیه‌ی معاملات هم جا داشته باشند این معامله با {num(used, 2)} USDT وثیقه باز می‌شود. برای حجم بیشتر، موجودی Margin را شارژ کنید.")


# --------------------------------------------------------------------------- positions
def card_position(key: str, t: Dict[str, Any], live: Dict[str, Any], liquidation_note: str = "",
                  realized: Optional[Decimal] = None, collateral=None, liq_price=None) -> str:
    pnl = live.get("unrealizedPNL")
    runner = "🏃 Runner: حد ضرر متحرک فعال" if t.get("trailing_active") else "🏃 Runner: در انتظار"
    lev = t.get("leverage", "?")
    lev_live = live.get("leverage")
    lev_txt = f"{lev}x"
    if lev_live not in (None, "", "0", 0) and str(lev_live) != str(lev):
        lev_txt += f" (⚠️ نوبیتکس الان {lev_live}x گزارش می‌دهد)"
    lines = [title(key, t.get("side")),
             f"🎯 ورود: {price(t.get('entry_actual'))}   |   🛑 حد ضرر فعلی: {price(t.get('stop_price'))}",
             f"⚙️ اهرم: {lev_txt}   |   📦 حجم: {qty(live.get('liability', t.get('initial_amount')))}"]
    if collateral is not None:
        lines.append(f"💵 مبلغ تعهد (وثیقه): {num(collateral, 4)} USDT")
    if liq_price is not None:
        lines.append(f"🧯 قیمت لیکویید: {price(liq_price)}")
    lines += [f"🎯 {targets_line(t)}", runner]
    if realized is not None:
        lines.append(f"💰 سود ثبت‌شده تا الان: {usdt(realized, 4)}")
    if pnl is not None:
        lines.append(f"{pnl_marker(pnl)} سود/زیان لحظه‌ای: {usdt(pnl, 4)}")
        if realized is not None:
            tot = (_dec(pnl) or Decimal(0)) + realized
            lines.append(f"🧮 جمع کل این معامله در این لحظه: {usdt(tot, 4)}")
    if t.get("signal_id"):
        lines.append(f"🆔 {t.get('signal_id')}")
    if liquidation_note:
        lines.append(liquidation_note.strip())
    return "\n".join(lines)


def card_positions(cards: List[str], footer: str = "") -> str:
    text = f"📊 معاملات باز ({len(cards)})\n{DIVIDER}\n" + f"\n{DIVIDER}\n".join(cards)
    if footer:
        text += f"\n{DIVIDER}\n{footer}"
    return clip(text)


def split_cards(cards: List[str], footer: str = "", limit: int = 3600) -> List[str]:
    """Pack position cards into Telegram-sized messages (never cutting a card in half);
    the totals footer goes on the last message."""
    pages: List[str] = []
    cur: List[str] = []
    size = 0
    for c in cards:
        c = clip(c, limit - 60)
        if cur and size + len(c) + 20 > limit - len(footer):
            pages.append(cur)
            cur, size = [], 0
        cur.append(c)
        size += len(c) + 20
    if cur:
        pages.append(cur)
    out: List[str] = []
    n_all = len(cards)
    for i, grp in enumerate(pages):
        head = f"📊 معاملات باز ({n_all})" + (f" — بخش {i + 1} از {len(pages)}" if len(pages) > 1 else "")
        body = f"\n{DIVIDER}\n".join(grp)
        text = f"{head}\n{DIVIDER}\n{body}"
        if i == len(pages) - 1 and footer:
            if len(text) + len(footer) + 20 > 3990 and len(pages) >= 1:
                out.append(text)
                text = f"🧮 جمع کل\n{DIVIDER}\n{footer}"
            else:
                text += f"\n{DIVIDER}\n{footer}"
        out.append(text)
    return out or ["📊 معاملات باز (۰)"]


def positions_footer(realized_total: Decimal, unrealized_total: Decimal, cum_total: Optional[Decimal] = None) -> str:
    tot = realized_total + unrealized_total
    lines = [f"🧮 جمع همه‌ی معاملات باز در این لحظه: {usdt(tot, 4)}",
             f"   (سود ثبت‌شده {signed(realized_total, 4)} + لحظه‌ای {signed(unrealized_total, 4)})"]
    if cum_total is not None:
        lines.append(f"📚 مجموع سود/زیان بسته‌شده‌ها تا الان: {usdt(cum_total, 4)}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- history / pnl reports
def _entry_value(h: Dict[str, Any]) -> Optional[Decimal]:
    return _dec(h.get("realized_usdt") if h.get("realized_usdt") is not None else h.get("realized_usdt_estimate"))


def history_block(idx: int, h: Dict[str, Any], cum_total: Decimal) -> str:
    """One trade, line by line, with its own running total and the account total after it."""
    key = str(h.get("key", "?"))
    v = _entry_value(h)
    exact = h.get("realized_usdt") is not None
    r = _dec(h.get("r_multiple") if h.get("r_multiple") is not None else h.get("r_multiple_estimate"))
    lines = [f"#{idx}  {title(key, h.get('side'))}",
             f"🕒 {fmt_time(h.get('opened_at'))} ← {fmt_time(h.get('closed_at'), True)}"
             + (f"  ({fmt_duration(h.get('opened_at'), h.get('closed_at'))})" if h.get("opened_at") else ""),
             f"🎯 ورود {price(h.get('entry_actual'))}" + (f"  →  خروج {price(h.get('exit_price'))}" if h.get("exit_price") else ""),
             f"📌 {REASON_FA.get(h.get('reason'), h.get('reason'))}"]
    hit = ", ".join(h.get("targets_hit") or []) or "هیچ‌کدام"
    lines.append(f"🎯 تارگت‌های خورده: {hit}")
    fills = h.get("fills") or []
    if fills:
        fl, _ = _fill_lines(fills)
        lines += fl
    if v is None:
        lines.append("⚪ نتیجه: نامشخص")
    else:
        lines.append(f"{pnl_marker(v)} نتیجه‌ی معامله: {usdt(v, 4)}" + (f" ({signed(r)}R)" if r is not None else "")
                     + ("" if exact else " (تخمینی)"))
    lines.append(f"🧮 مجموع کل حساب تا بعد از این معامله: {usdt(cum_total, 4)}")
    return "\n".join(lines)


def history_pages(blocks: List[str], per_page_chars: int = 3600) -> List[List[str]]:
    """Group blocks into Telegram-sized pages (never cut a block in half)."""
    pages: List[List[str]] = []
    cur: List[str] = []
    size = 0
    for b in blocks:
        b = clip(b, per_page_chars - 50)
        if cur and size + len(b) + 40 > per_page_chars:
            pages.append(cur)
            cur, size = [], 0
        cur.append(b)
        size += len(b) + 40
    if cur:
        pages.append(cur)
    return pages


def history_text(page_blocks: List[str], page: int, pages: int, total: int, cum_total: Decimal) -> str:
    head = [f"📜 تاریخچه‌ی معاملات — صفحه {page} از {pages}", f"کل معاملات ثبت‌شده: {total}   |   مجموع کل: {usdt(cum_total, 4)}", DIVIDER]
    return "\n".join(head) + "\n" + f"\n{DIVIDER}\n".join(page_blocks)


def card_history_item(h: Dict[str, Any]) -> str:
    """Compact single-trade line (used by short listings)."""
    key = str(h.get("key", "?"))
    v = _entry_value(h)
    exact = h.get("realized_usdt") is not None
    r = _dec(h.get("r_multiple") if h.get("r_multiple") is not None else h.get("r_multiple_estimate"))
    when = fmt_time(h.get("closed_at"))
    hit = ", ".join(h.get("targets_hit") or []) or "هیچ‌کدام"
    line1 = f"{pnl_marker(v)} {title(key, h.get('side'))}"
    line2 = (f"   {signed(v)} USDT" + (f" ({signed(r)}R)" if r is not None else "") + ("" if exact else " (تخمینی)")) \
        if v is not None else "   نتیجه نامشخص"
    return f"{line1}\n{line2}\n   🎯 {hit}   |   📌 {REASON_FA.get(h.get('reason'), h.get('reason'))}\n   🕒 {when}"


def pnl_report(sample_len: int, total: Decimal, wins: int, losses: int, flat: int, exact_n: int, counted: int,
               best: Optional[Decimal], worst: Optional[Decimal], days: List[tuple], all_time: Decimal,
               skipped: int = 0, today: Optional[Decimal] = None) -> str:
    winrate = (wins / counted * 100) if counted else 0
    avg = (total / counted) if counted else Decimal(0)
    lines = ["📊 خلاصه‌ی سود و زیان" + (" (واقعی از نوبیتکس)" if exact_n == counted else " (ترکیب واقعی + تخمینی)"),
             DIVIDER,
             f"بازه: آخرین {sample_len} معامله" + (f" ({skipped} مورد بدون داده‌ی کافی)" if skipped else ""),
             f"{pnl_marker(total)} جمع این بازه: {usdt(total, 4)}",
             f"📈 میانگین هر معامله: {usdt(avg, 4)}",
             f"✅ برد: {wins}   |   ❌ باخت: {losses}   |   ⚪ سر به سر: {flat}",
             f"🎯 نرخ برد: ٪{winrate:.1f}"]
    if best is not None:
        lines.append(f"🏆 بهترین: {usdt(best, 4)}   |   💥 بدترین: {usdt(worst, 4)}")
    if today is not None:
        lines.append(f"📅 امروز: {usdt(today, 4)}")
    if days:
        lines += ["", "🗓 روز به روز (سطر به سطر با مجموع تا آن روز):"]
        for day, cnt, dsum, cum in days:
            lines.append(f"{pnl_marker(dsum)} {day}  |  {cnt} معامله  |  {signed(dsum, 4)}  |  🧮 {signed(cum, 4)}")
    lines += ["", f"📚 مجموع کل حساب از ابتدا: {usdt(all_time, 4)}",
              f"از {counted} معامله: {exact_n} مورد عدد واقعی نوبیتکس، {counted - exact_n} مورد تخمینی.",
              "ℹ️ عدد واقعی همان PNL خود پوزیشن در نوبیتکس (یا جمع Fillهای واقعی) است."]
    return clip("\n".join(lines))


# --------------------------------------------------------------------------- liquidation safety
def liq_analysis(key: str, side: str, entry, stop, liq, gap_pct, collateral, leverage, free_balance,
                 options: List[Dict[str, Any]], note: str = "") -> str:
    lines = ["🛡 افزایش فاصله‌ی ایمنی تا لیکویید", title(key, side), DIVIDER,
             f"🎯 ورود: {price(entry)}   |   🛑 حد ضرر: {price(stop)}",
             f"🧯 قیمت لیکویید فعلی: {price(liq)}" + (f"   (فاصله تا حد ضرر ٪{gap_pct:.1f})" if gap_pct is not None else ""),
             f"💵 مبلغ تعهد (وثیقه) الان: {num(collateral, 4)} USDT   |   ⚙️ اهرم: {leverage}x",
             f"💰 موجودی آزاد Margin: {num(free_balance, 2)} USDT", ""]
    if note:
        lines += [note, ""]
    lines.append("با اضافه کردن وثیقه، قیمت لیکویید از حد ضرر دورتر می‌شود (اهرم مؤثر کمتر). "
                 "حد ضرر و تارگت‌ها هیچ تغییری نمی‌کنند. یکی از گزینه‌ها را بزنید:")
    for o in options:
        ok = o.get("affordable", True)
        lines.append(f"• {o['label']}: +{num(o['add'], 2)} USDT  ⟶  لیکویید ≈ {price(o['new_liq'])}  (فاصله ≈ ٪{o['new_gap']:.1f})"
                     + ("" if ok else "  ❌ موجودی کافی نیست"))
    lines.append("ℹ️ اعداد لیکویید تخمینی‌اند؛ مقدار دقیق بعد از اعمال از نوبیتکس خوانده و گزارش می‌شود.")
    return "\n".join(lines)


def liq_applied(key: str, side: str, added, old_col, new_col, old_liq, new_liq, stop) -> str:
    lines = ["✅ وثیقه افزایش یافت", title(key, side), DIVIDER,
             f"➕ اضافه‌شده: {num(added, 4)} USDT",
             f"💵 وثیقه: {num(old_col, 4)} ⟶ {num(new_col, 4)} USDT"]
    if new_liq is not None:
        line = f"🧯 قیمت لیکویید: {price(old_liq)} ⟶ {price(new_liq)}" if old_liq is not None else f"🧯 قیمت لیکویید جدید: {price(new_liq)}"
        lines.append(line)
        t, _ = liq_gap_line(side, stop, new_liq)
        if t:
            lines.append(t)
    lines.append("حد ضرر و تارگت‌های معامله بدون تغییر ماندند.")
    return "\n".join(lines)


# --------------------------------------------------------------------------- subscriber flow
TERMS_PAGES = [
    "📜 شرایط و قوانین استفاده از ربات معامله‌گر خودکار\n"
    f"{BRAND}\n"
    f"{DIVIDER}\n"
    "قبل از شروع، این بندها را با آرامش بخوانید. با زدن «✅ می‌پذیرم» یعنی همه را قبول دارید.\n\n"
    "1️⃣ ربات چه کاری می‌کند؟\n"
    "سیگنال‌های کانال را روی حساب نوبیتکس خودِ شما اجرا می‌کند: ورود به معامله، ثبت حد ضرر، چهار تارگت پله‌ای "
    "(۲۰٪ / ۳۰٪ / ۱۵٪ / ۱۰٪) و یک Runner ۲۵٪ با حد ضرر متحرک. همه‌ی سفارش‌های محافظتی روی خود نوبیتکس ثبت می‌شوند.\n\n"
    "2️⃣ پول شما همیشه دست خودتان است\n"
    "• ربات فقط اجازه‌ی «خواندن» و «معامله» دارد و هیچ‌وقت نمی‌تواند برداشت کند.\n"
    "• کلیدی که دسترسی برداشت (Withdraw) داشته باشد به‌طور خودکار رد می‌شود.\n"
    "• کلیدها رمزنگاری می‌شوند و پیام حاوی کلید از چت شما پاک می‌شود؛ ادمین هیچ‌وقت کلید خام شما را نمی‌بیند.\n"
    "• هر لحظه می‌توانید با دکمه‌ی «قطع اتصال» کلید را از سیستم حذف کنید.\n\n"
    "3️⃣ پیش‌نیازها\n"
    "• حساب نوبیتکس احراز هویت‌شده با معاملات تعهدی (Margin) فعال.\n"
    "• موجودی USDT در کیف پول تعهدی (Margin)؛ هر معامله بخشی از آن را به‌عنوان وثیقه می‌گیرد. "
    "اگر موجودی کم باشد، ربات شما را با پیام مطلع می‌کند.\n"
    "• کلید API فقط با دسترسی Read و Trade. اگر برای کلید محدودیت IP گذاشته‌اید، آدرس سرور ربات را از ادمین بگیرید.",

    "4️⃣ ریسک را جدی بگیرید ⚠️\n"
    "• سود تضمین‌شده نیست. معامله با اهرم می‌تواند به ضرر یا لیکویید شدن برسد و مسئولیت آن با خود شماست.\n"
    "• در نوسان شدید یا پرش قیمت، قیمت خروج ممکن است بدتر از حد ضرر باشد (Slippage).\n"
    "• ربات نگهبان حد ضرر دارد: اگر حد ضرر بخورد، کل حجم باقی‌مانده معامله بسته می‌شود؛ ولی هیچ سیستمی نمی‌تواند "
    "در برابر قطعی صرافی، اینترنت یا سرور تضمین ریاضی بدهد.\n"
    "• ریسک هر معامله، سقف وثیقه، اهرم و حداکثر معاملات همزمان را در «⚙️ تنظیمات» خودتان تعیین می‌کنید "
    "(در سقفی که ادمین برایتان گذاشته؛ بالاتر از آن با تأیید ادمین).\n\n"
    "5️⃣ قوانین استفاده\n"
    "• سفارش‌ها و حد ضرر معاملات ربات را در اپ نوبیتکس دستی تغییر ندهید؛ مدیریت خودکار به هم می‌ریزد. "
    "برای بستن از دکمه‌های همین ربات استفاده کنید.\n"
    "• کارمزدها و هزینه‌های نوبیتکس بر عهده‌ی شماست.\n"
    "• اگر نمی‌خواهید معامله‌ی جدید باز شود، «⏸ توقف ورود» را بزنید؛ معاملات باز همچنان مدیریت می‌شوند.\n\n"
    "6️⃣ اشتراک\n"
    "• اشتراک مدت‌دار است و بعد از تأیید پرداخت توسط ادمین فعال می‌شود؛ شمارش روزها از لحظه‌ی اتصال موفق کلید شروع می‌شود.\n"
    "• با پایان اشتراک فقط ورود معامله‌ی جدید متوقف می‌شود؛ معاملات باز تا بسته شدن با همان حد ضرر و تارگت‌ها مدیریت می‌شوند.\n"
    "• پرداخت دستی است (کارت‌به‌کارت یا USDT) و پس از بررسی ادمین ثبت می‌شود.\n\n"
    "7️⃣ حریم خصوصی\n"
    "فقط اطلاعات لازم برای اجرای معاملات و اشتراک شما نگه داشته می‌شود و با کسی به اشتراک گذاشته نمی‌شود.",
]

TERMS_FOOTER = ("✅ اگر همه‌ی بندها را قبول دارید دکمه‌ی «می‌پذیرم» را بزنید.\n"
                "برای هر سؤال، دکمه‌ی «🆘 پشتیبانی» همیشه در دسترس است.")


def msg_welcome_new(first_name: str = "") -> str:
    hello = f"سلام {first_name} عزیز 👋" if first_name else "سلام 👋"
    return (f"{hello}\nبه ربات معامله‌گر خودکار {BRAND} خوش آمدید.\n{DIVIDER}\n"
            "این ربات سیگنال‌های کانال را روی حساب نوبیتکس خودِ شما اجرا می‌کند، با حد ضرر و تارگت‌های خودکار، "
            "و همه‌چیز را با چند دکمه مدیریت می‌کنید؛ بدون تایپ دستور.\n\n"
            "مسیر راه‌اندازی فقط ۴ قدم است:\n"
            "1) خواندن و پذیرفتن شرایط\n2) انتخاب و پرداخت اشتراک\n3) اتصال امن حساب نوبیتکس\n4) شروع معامله‌ی خودکار 🚀")


def msg_blocked() -> str:
    return ("⛔ دسترسی شما به این ربات فعلاً بسته است.\nاگر فکر می‌کنید اشتباهی شده، با دکمه‌ی پشتیبانی پیام بدهید.")


def msg_declined() -> str:
    return "باشه، مشکلی نیست. تا شرایط را نپذیرید ربات هیچ کاری روی حساب شما انجام نمی‌دهد. هر وقت خواستید دوباره /start را بزنید."


def msg_plans(tiers: Dict[int, Dict[str, Any]], card_ok: bool, usdt_ok: bool, usdt_network: str = "",
              disc: Optional[Dict[str, Any]] = None) -> str:
    lines = ["💳 انتخاب اشتراک", DIVIDER]
    if not tiers:
        lines.append("هنوز تعرفه‌ای ثبت نشده است. لطفاً با پشتیبانی هماهنگ کنید.")
        return "\n".join(lines)
    if disc:
        lines += [f"🎟 کد تخفیف {disc['code']} فعال است: {float(disc['percent']):g}٪ تخفیف"
                  + ("" if disc.get("all_plans") else " (فقط روی اشتراک‌هایی که با 🎟 مشخص شده‌اند)"), ""]
    for days, p in tiers.items():
        opts = []
        dp = (disc or {}).get("prices", {}).get(days, {})
        if p.get("toman") is not None and card_ok:
            opts.append(f"{int(p['toman']):,} ← {int(dp['toman']):,} تومان 🎟" if "toman" in dp else f"{int(p['toman']):,} تومان")
        if p.get("usdt") is not None and usdt_ok:
            opts.append(f"{float(p['usdt']):g} ← {float(dp['usdt']):g} USDT 🎟" if "usdt" in dp else f"{float(p['usdt']):g} USDT")
        lines.append(f"📅 {days} روز:  " + ("  یا  ".join(opts) if opts else "— (روش پرداخت هنوز تنظیم نشده)"))
    lines.append("")
    lines.append("روش پرداخت: " + "، ".join(x for x in ["کارت‌به‌کارت (تومان)" if card_ok else "", f"USDT ({usdt_network})" if usdt_ok else ""] if x) or "—")
    lines.append("👇 مدت و روش پرداخت را با دکمه انتخاب کنید.")
    return "\n".join(lines)


def msg_pay_instructions(pid: str, days: int, currency: str, amount: float, card_number: str = "",
                         card_holder: str = "", address: str = "", network: str = "",
                         code: str = "", list_amount: Optional[float] = None, percent: float = 0.0) -> str:
    lines = ["🧾 درخواست پرداخت ثبت شد", DIVIDER, f"شناسه: {pid}   |   مدت: {days} روز"]
    if code and list_amount:
        was = f"{int(list_amount):,} تومان" if currency == "toman" else f"{list_amount:g} USDT"
        lines.append(f"🎟 کد {code}: {percent:g}٪ تخفیف (قیمت اصلی {was})")
    if currency == "toman":
        lines += [f"💳 مبلغ {int(amount):,} تومان را به کارت زیر واریز کنید:", f"{card_number}", f"به‌نام: {card_holder}"]
    else:
        lines += [f"💵 دقیقاً {amount:g} USDT را فقط روی شبکه‌ی {network} به آدرس زیر واریز کنید "
                  f"(ارسال روی شبکه‌ی دیگر ممکن است قابل بازگشت نباشد):", f"{address}"]
    lines += ["", "بعد از واریز، دکمه‌ی «✅ پرداخت کردم» را بزنید و کد رهگیری (یا هش تراکنش) را بفرستید. "
                  "ادمین بررسی می‌کند و نتیجه همین‌جا به شما اطلاع داده می‌شود."]
    return "\n".join(lines)


def msg_receipt_saved(pid: str) -> str:
    return (f"✅ رسید پرداخت {pid} ثبت شد.\nادمین بررسی می‌کند و نتیجه همین‌جا به شما اعلام می‌شود. "
            "معمولاً چند دقیقه تا چند ساعت طول می‌کشد.")


CONNECT_GUIDE = (
    "🔑 اتصال امن حساب نوبیتکس\n"
    f"{DIVIDER}\n"
    "قدم‌های ساخت کلید API (۲ دقیقه):\n"
    "1️⃣ وارد نوبیتکس شوید ← پروفایل ← «API / کلید API».\n"
    "2️⃣ روی «ساخت کلید جدید» بزنید.\n"
    "3️⃣ فقط دو دسترسی را روشن کنید: ✅ خواندن (READ) و ✅ معامله (TRADE).\n"
    "   ⛔ دسترسی برداشت (WITHDRAW) را حتماً خاموش بگذارید؛ کلیدی که برداشت داشته باشد پذیرفته نمی‌شود.\n"
    "4️⃣ کلید عمومی (API Key) و کلید خصوصی (Secret) را کپی کنید. کلید خصوصی فقط یک‌بار نشان داده می‌شود.\n"
    "5️⃣ اگر محدودیت IP گذاشته‌اید، آدرس سرور ربات را از پشتیبانی بگیرید.\n\n"
    "🔒 امنیت: کلید شما قبل از ارسال رمزنگاری می‌شود و پیام حاوی آن از چت پاک می‌شود.\n"
    "💰 یادتان باشد در کیف پول تعهدی (Margin) نوبیتکس USDT داشته باشید.\n\n"
    "آماده‌اید؟ دکمه‌ی زیر را بزنید؛ ربات مرحله‌به‌مرحله فقط همان چیزی را که لازم است می‌پرسد."
)


def msg_connected(days_left: float, balance=None) -> str:
    lines = ["🎉 اتصال با موفقیت انجام شد!", DIVIDER,
             f"⏳ اشتراک شما فعال است — {days_left:.1f} روز باقی مانده.",
             "از این لحظه سیگنال‌های جدید کانال روی حساب شما اجرا می‌شود."]
    if balance is not None:
        lines.append(f"💰 موجودی آزاد Margin: {num(balance, 2)} USDT")
        if _dec(balance) is not None and _dec(balance) < Decimal("5"):
            lines.append("⚠️ موجودی کم است؛ برای اینکه سیگنال‌ها اجرا شوند، USDT بیشتری به کیف پول تعهدی اضافه کنید.")
    lines.append("منوی کامل شما پایین باز شد 👇")
    return "\n".join(lines)


def msg_main_menu(status_fa: str, days_left: Optional[float], open_n: int, paused: bool, balance=None,
                  daily_pnl=None, name: str = "") -> str:
    lines = [f"📋 منوی من" + (f" — {name}" if name else ""), DIVIDER, f"وضعیت اشتراک: {status_fa}"]
    if days_left is not None:
        lines.append(f"⏳ روزهای باقی‌مانده: {days_left:.1f}")
    lines.append(f"📈 معاملات باز: {open_n}")
    lines.append("ورود معامله‌ی جدید: " + ("⏸ متوقف (توسط شما)" if paused else "🟢 فعال"))
    if balance is not None:
        lines.append(f"💰 موجودی آزاد Margin: {num(balance, 2)} USDT")
    if daily_pnl is not None:
        lines.append(f"📅 نتیجه‌ی امروز: {usdt(daily_pnl, 4)}")
    lines.append("\nهر کاری لازم دارید با دکمه‌های زیر انجام دهید 👇")
    return "\n".join(lines)


def msg_help_user() -> str:
    return ("📖 راهنمای سریع\n" + DIVIDER + "\n"
            "• 📈 معاملات باز: وضعیت هر معامله، سود/زیان لحظه‌ای، و دکمه‌های بستن و افزایش ایمنی.\n"
            "• 📜 تاریخچه: گزارش سطر به سطر معاملات بسته‌شده با مجموع سود/زیان.\n"
            "• 📊 سود و زیان: خلاصه و روزبه‌روز.\n"
            "• 💰 موجودی من: موجودی Margin و Spot.\n"
            "• ⚙️ تنظیمات: ریسک، وثیقه، اهرم، حداکثر معاملات، خروج خودکار، سقف ضرر روزانه، گزارش روزانه.\n"
            "• 🛡 «افزایش فاصله تا لیکویید»: وثیقه‌ی یک معامله را معقول بالا می‌برد.\n"
            "• 💵 «مبلغ تعهد همه»: وثیقه‌ی همه‌ی معاملات باز را با درصد دلخواه بالا می‌برد.\n"
            "• 💳 تمدید اشتراک: انتخاب مدت و پرداخت.\n"
            "• 🆘 پشتیبانی: پیام مستقیم به ادمین.\n\n"
            "🔒 اگر جایی گیر کردید، /start را بزنید؛ ربات شما را به مرحله‌ی درست برمی‌گرداند.")


def status_fa(status: str) -> str:
    return {"active": "🟢 فعال", "pending_connect": "🟡 منتظر اتصال کلید", "suspended": "🟠 معلق",
            "expired": "🔴 منقضی", "new": "⚪ ثبت‌نام‌شده (بدون اشتراک)", "blocked": "⛔ مسدود"}.get(str(status), str(status))


def msg_subscription(rec: Dict[str, Any], open_n: int, payments: List[str]) -> str:
    lines = ["🔔 وضعیت اشتراک من", DIVIDER, f"وضعیت: {status_fa(rec['status'])}" + (" (ورود جدید متوقف شده توسط خودتان)" if rec.get("paused") else "")]
    if rec["status"] == "active":
        lines.append(f"⏳ روزهای باقیمانده: {rec['days_left']:.1f}")
        lines.append(f"📅 تاریخ پایان: {rec['expires_date']}")
    elif rec["status"] == "pending_connect":
        lines.append(f"🎁 روزهای در انتظار (بعد از اتصال کلید شروع می‌شود): {rec['pending_days']:.0f}")
    elif rec["status"] == "expired":
        lines.append("اشتراک منقضی شده؛ ورود جدید متوقف است. با «💳 تمدید اشتراک» ادامه دهید.")
    lines.append(f"🔑 کلید API: {'متصل ✅' if rec['connected'] else 'متصل نشده ❌'}")
    lines.append(f"📈 معاملات باز: {open_n}")
    if payments:
        lines += ["", "💳 پرداخت‌های اخیر:"] + payments
    return "\n".join(lines)


def msg_settings(rec: Dict[str, Any]) -> str:
    return "\n".join([
        "⚙️ تنظیمات معامله‌ی من", DIVIDER,
        f"🎯 ریسک هر معامله: {rec['risk']} USDT   (سقف شما {rec['cap_risk']})",
        f"💵 سقف وثیقه‌ی هر معامله: {rec['collateral']} USDT   (سقف شما {rec['cap_collateral']})",
        f"📈 اهرم: {rec['leverage']}x   (سقف شما {rec['cap_leverage']}x)",
        f"🔢 حداکثر معاملات همزمان: {rec['max_open']}",
        f"🤖 خروج خودکار طبق کانال: {'روشن ✅' if rec['auto_exit'] else 'خاموش ⏸'}",
        f"🛑 سقف ضرر روزانه: {('خاموش' if str(rec['daily_loss']) in ('0', '0.0', '') else str(rec['daily_loss']) + ' USDT')}",
        f"📰 گزارش روزانه: {'روشن ✅' if rec['daily_report'] else 'خاموش ⏸'}",
        "",
        "روی هر مورد بزنید تا مقدار دلخواه را با دکمه انتخاب کنید (بدون تایپ دستور).",
        "تغییرات فقط روی معاملات جدید اثر می‌گذارد.",
    ])


def msg_value_menu(title_fa: str, current: str, hint: str) -> str:
    return f"{title_fa}\n{DIVIDER}\nمقدار فعلی: {current}\n{hint}\n\n👇 یکی از گزینه‌ها را بزنید:"


def msg_cap_exceeded(name_fa: str, value: str, cap: str) -> str:
    return (f"⚠️ {name_fa} = {value} از سقف مجاز شما ({cap}) بالاتر است.\n"
            "اگر می‌خواهید سقف شما بالا برده شود، می‌توانید از ادمین درخواست کنید؛ با دکمه‌ی زیر درخواست برای ادمین ارسال می‌شود "
            "و نتیجه همین‌جا به شما اعلام می‌شود.")


def msg_confirm(title_fa: str, body: str) -> str:
    return f"⚠️ {title_fa}\n{DIVIDER}\n{body}"


def msg_no_key_yet() -> str:
    return "🔑 هنوز حساب نوبیتکس‌تان را وصل نکرده‌اید. دکمه‌ی زیر شما را مرحله‌به‌مرحله راهنمایی می‌کند."


def msg_expired_hint() -> str:
    return "⏳ اشتراک شما تمام شده است. معاملات باز تا بسته شدن مدیریت می‌شوند؛ برای معامله‌ی جدید، اشتراک را تمدید کنید."


# --------------------------------------------------------------------------- admin cards
def card_new_user_admin(uid: str, name: str, username: str) -> str:
    return "\n".join(["👤 کاربر جدید وارد ربات شد", DIVIDER, f"شناسه: {uid}",
                      f"نام: {name or '—'}", f"یوزرنیم: @{username}" if username else "یوزرنیم: —",
                      "شرایط را نپذیرفته/پرداختی ندارد؛ تا وقتی اشتراک نگیرد هیچ معامله‌ای برایش باز نمی‌شود.",
                      "می‌توانید اشتراک هدیه بدهید یا او را مسدود کنید:"])


def card_payment_admin(pid: str, uid: str, name: str, days: int, amount: float, currency: str, note: str,
                       code: str = "", list_amount: float = 0.0, percent: float = 0.0) -> str:
    amt = f"{int(amount):,} تومان" if currency == "toman" else f"{amount:g} USDT"
    disc_line = []
    if code and list_amount:
        was = f"{int(list_amount):,} تومان" if currency == "toman" else f"{list_amount:g} USDT"
        disc_line = [f"🎟 کد تخفیف {code}: {percent:g}٪ (قیمت اصلی {was}) — مبلغ بالا همان مبلغ تخفیف‌خورده است"]
    return "\n".join(["💳 رسید پرداخت جدید — منتظر تأیید شما", DIVIDER, f"شناسه‌ی پرداخت: {pid}",
                      f"کاربر: {uid}" + (f" ({name})" if name else ""),
                      f"مدت: {days} روز   |   مبلغ: {amt}", *disc_line,
                      f"کد رهگیری/هش: {note or '—'}",
                      "لطفاً با حساب بانکی/بلاک‌چین خودتان چک کنید و نتیجه را بزنید:"])


def card_cap_request_admin(uid: str, name: str, req: Dict[str, str], cur: Dict[str, str]) -> str:
    lines = ["📨 درخواست افزایش سقف از طرف کاربر", DIVIDER, f"کاربر: {uid}" + (f" ({name})" if name else "")]
    names = {"risk_usdt": "ریسک هر معامله", "max_collateral_usdt": "سقف وثیقه", "leverage": "اهرم", "max_open_trades": "حداکثر معاملات"}
    for k, v in req.items():
        lines.append(f"• {names.get(k, k)}: {cur.get(k, '؟')} ⟵ درخواست: {v}")
    lines.append("تأیید می‌کنید؟")
    return "\n".join(lines)


def card_support_admin(uid: str, name: str, text: str) -> str:
    return "\n".join(["🆘 پیام پشتیبانی از کاربر", DIVIDER, f"کاربر: {uid}" + (f" ({name})" if name else ""), "", text[:1500]])


def card_user_panel(rec: Dict[str, Any], open_n: int, pay_lines: List[str]) -> str:
    lines = [f"👤 کاربر {rec['user_id']}" + (f" ({rec['name']})" if rec.get("name") else ""), DIVIDER,
             f"وضعیت: {status_fa(rec['status'])}" + (" | ⏸ توقف ورود توسط کاربر" if rec.get("paused") else "")]
    if rec["status"] == "active":
        lines.append(f"⏳ باقیمانده: {rec['days_left']:.1f} روز (تا {rec['expires_date']})")
    elif rec["status"] == "pending_connect":
        lines.append(f"🎁 روزهای در انتظار اتصال: {rec['pending_days']:.0f}")
    lines.append(f"🔑 کلید: {'متصل ✅' if rec['connected'] else 'متصل نشده ❌'}   |   شرایط: {'پذیرفته ✅' if rec['terms'] else 'نه'}")
    lines.append(f"🎯 ریسک {rec['risk']}/{rec['cap_risk']}  |  💵 وثیقه {rec['collateral']}/{rec['cap_collateral']}  |  "
                 f"📈 اهرم {rec['leverage']}/{rec['cap_leverage']}x  |  🔢 حداکثر {rec['max_open']}")
    lines.append(f"📈 معاملات باز: {open_n}")
    if rec.get("last_error"):
        lines.append(f"⚠️ آخرین خطا: {rec['last_error']}")
    if pay_lines:
        lines += ["", "💳 پرداخت‌ها:"] + pay_lines
    return "\n".join(lines)


def discount_line(dc, uses: int) -> str:
    """One admin-facing line describing a discount code (dc is a user_store.DiscountCode)."""
    scope = "همه‌ی اشتراک‌ها" if not dc.plans else "، ".join(f"{d} روزه" for d in dc.plans)
    cap = f"{uses}/{dc.max_uses}" if dc.max_uses else f"{uses}/∞"
    exp = ""
    if dc.expires_at:
        import time as _t
        left = (dc.expires_at - _t.time()) / 86400
        exp = f" | انقضا: {left:.1f} روز دیگر" if left > 0 else " | ⌛ منقضی شده"
    state = "" if dc.active else " | ⏸ غیرفعال"
    return f"🎟 {dc.code} — {dc.percent:g}٪ روی {scope} | استفاده: {cap}{exp}{state}"
