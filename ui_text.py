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

import re
import time
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional

NOBITEX_SIGNUP_URL = "https://nobitex.ir/signup/?refcode=221200"
SIGNUP_NOTE = ("🆕 هنوز در نوبیتکس ثبت‌نام نکرده‌اید؟ از دکمه‌ی «ثبت‌نام در نوبیتکس» همین پایین (یا لینک زیر) حساب بسازید؛ "
               "ربات فقط روی حساب نوبیتکس کار می‌کند.\n" + NOBITEX_SIGNUP_URL)

DIVIDER = "━━━━━━━━━━━━━━"

_SIGNUP_MARKS = ("ثبت‌نام در نوبیتکس", "nobitex.ir/signup", "حساب نوبیتکس ندارید", "ثبت‌نام نکرده‌اید")


def strip_signup(text: str) -> str:
    """For people who already connected their Nobitex account: drop the sign-up note/link/lines (never needed again)."""
    t = str(text).replace(SIGNUP_NOTE, "")
    t = "\n".join(l for l in t.split("\n") if not any(m in l for m in _SIGNUP_MARKS))
    return re.sub(r"\n{3,}", "\n\n", t).rstrip()

# Referral numbers are runtime settings owned by the executor; it registers a provider so every user-facing text states the
# real current numbers (never a stale hard-coded one).
_REF_PROVIDER = lambda: (3.0, 0.0)


def set_referral_provider(fn) -> None:
    global _REF_PROVIDER
    _REF_PROVIDER = fn


def ref_days() -> float:
    try:
        return max(0.0, float(_REF_PROVIDER()[0]))
    except Exception:
        return 3.0


def ref_invitee_days() -> float:
    try:
        return max(0.0, float(_REF_PROVIDER()[1]))
    except Exception:
        return 0.0


def ref_phrase() -> str:
    d = ref_days()
    return f"{d:g} روز اشتراک رایگان" if d > 0 else "روز اشتراک رایگان"


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
        if t.get("merged"):
            lines.append(f"   T{n}  {price(t.get('tp_price'))}   (🔀 {(_dec(t.get('pct')) or Decimal(0)) * 100:.0f}٪ حجم؛ برای حداقل سایز نوبیتکس کوچک بود ← در رانر ادغام شد)")
            continue
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
LEGAL_DISCLAIMER = (
    "استفاده از این ربات و انجام معاملات خودکار، با مسئولیت کامل کاربر و به اختیار شخص استفاده کننده صورت می‌گیرد. "
    "تمامی سیگنال‌ها و عملیات معاملاتی صرفاً ابزارهای کمکی بوده و هیچ‌گونه تضمینی برای کسب سود وجود ندارد. "
    "ربات و سازندگان آن هیچ‌گونه مسئولیتی در قبال سود یا زیان، از دست رفتن سرمایه، اختلالات فنی، قطعی ارتباطات یا "
    "خطاهای اجرایی معاملات کاربران بر عهده نمی‌گیرند. کاربر با استفاده از ربات، آگاهانه ریسک‌های معاملاتی را می‌پذیرد "
    "و مسئولیت نهایی تصمیمات و نتایج معاملات بر عهده خود اوست.")

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
    "• هنگام ساخت کلید «برداشت (Withdraw)» را خاموش بگذارید؛ اگر نوبیتکس مجوزهای کلید را نشان دهد و برداشت روشن باشد، کلید رد می‌شود.\n"
    "• کلیدها رمزنگاری می‌شوند و پیام حاوی کلید از چت شما پاک می‌شود؛ ادمین هیچ‌وقت کلید خام شما را نمی‌بیند.\n"
    "• هر لحظه می‌توانید با دکمه‌ی «قطع اتصال» کلید را از سیستم حذف کنید.\n\n"
    "3️⃣ پیش‌نیازها\n"
    "• حساب نوبیتکس احراز هویت‌شده با معاملات تعهدی (Margin) فعال.\n"
    "• موجودی USDT در کیف پول تعهدی (Margin) — USDT را از کیف پول اسپات به تعهدی منتقل کنید؛ هر معامله بخشی از آن را به‌عنوان وثیقه می‌گیرد. "
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
    "• اگر نمی‌خواهید معامله‌ی جدید باز شود، «🔕 خاموش کردن سیگنال‌دهی» را بزنید؛ معاملات باز همچنان مدیریت می‌شوند و هر زمان بخواهید «🔔 روشن کردن سیگنال‌دهی» را می‌زنید.\n\n"
    "6️⃣ اشتراک\n"
    "• اشتراک مدت‌دار است و بعد از تأیید پرداخت توسط ادمین فعال می‌شود؛ شمارش روزها از لحظه‌ی اتصال موفق کلید شروع می‌شود.\n"
    "• با پایان اشتراک فقط ورود معامله‌ی جدید متوقف می‌شود؛ معاملات باز تا بسته شدن با همان حد ضرر و تارگت‌ها مدیریت می‌شوند.\n"
    "• پرداخت دستی است (کارت‌به‌کارت یا USDT) و پس از بررسی ادمین ثبت می‌شود.\n\n"
    "7️⃣ حریم خصوصی\n"
    "فقط اطلاعات لازم برای اجرای معاملات و اشتراک شما نگه داشته می‌شود و با کسی به اشتراک گذاشته نمی‌شود.\n\n"
    "8️⃣ سلب مسئولیت و پذیرش ریسک ⚖️\n"
    f"{LEGAL_DISCLAIMER}",
]

TERMS_FOOTER = ("⚖️ با زدن «می‌پذیرم» تأیید می‌کنید که بند ۸ (سلب مسئولیت و پذیرش ریسک) را خوانده و پذیرفته‌اید.\n"
                "✅ اگر همه‌ی بندها را قبول دارید دکمه‌ی «می‌پذیرم» را بزنید.\n"
                "برای هر سؤال، دکمه‌ی «🆘 پشتیبانی» همیشه در دسترس است.")


def msg_invite(link: str, code: str, count: int, days_earned: float, reward_days: float, invitee_days: float) -> str:
    bonus = f"\n🎁 دوستِ دعوت‌شده‌ی شما هم {invitee_days:g} روز هدیه می‌گیرد." if invitee_days > 0 else ""
    where = f"🔗 لینک اختصاصی شما:\n{link}" if link else f"🔗 کد دعوت شما: {code}\n(دوستتان باید ربات را با این دستور شروع کند: /start {code})"
    return ("🎁 دعوت دوستان و دریافت اشتراک رایگان\n" + DIVIDER + "\n"
            f"به ازای هر دوستی که با لینک شما وارد ربات شود و اولین اشتراکش تأیید شود، {reward_days:g} روز به اشتراک شما اضافه می‌شود." + bonus + "\n\n"
            f"{where}\n\n"
            f"👥 دعوت‌های موفق: {count}\n🗓 مجموع روزهای هدیه‌ی گرفته‌شده: {days_earned:g}\n\n"
            "ℹ️ هدیه پس از تأیید پرداخت دوستتان ثبت می‌شود (هر نفر فقط یک‌بار).")


def trial_line(days: float) -> str:
    return (f"🎁 هدیه‌ی ورود: {days:g} روز اشتراک رایگان آزمایشی! بدون پرداخت، ربات را روی حساب خودتان امتحان کنید؛ "
            "فقط شرایط را بپذیرید و حساب نوبیتکس را وصل کنید (شمارش روزها از لحظه‌ی اتصال شروع می‌شود).")


def msg_trial_granted(days: float) -> str:
    return (f"🎁 {days:g} روز اشتراک رایگان برای شما فعال شد!\n{DIVIDER}\n"
            "• این هدیه‌ی ماست تا ربات را بدون هزینه و با حساب خودتان امتحان کنید.\n"
            "• شمارش روزها از لحظه‌ی اتصال حساب نوبیتکس شروع می‌شود، نه الان؛ پس هیچ روزی هدر نمی‌رود.\n"
            "• هیچ پرداختی لازم نیست و هیچ مبلغی از شما کسر نمی‌شود. اگر راضی بودید، هر زمان می‌توانید تمدید کنید.\n"
            "👉 قدم بعدی: اتصال امن حساب نوبیتکس (کلید فقط با دسترسی خواندن و معامله). بعد از اتصال، سیگنال‌های جدید خودکار روی حساب شما اجرا می‌شوند.")


def msg_trial_trust() -> str:
    return ("🛡 شفافیت کامل: دسترسی ربات فقط «معامله» است، نه برداشت؛ ربات هیچ‌وقت برداشت نمی‌کند و کلید دارای برداشت (در صورت قابل‌تشخیص بودن) پذیرفته نمی‌شود. "
            "هر لحظه می‌توانید ربات را متوقف یا اتصال را قطع کنید.")


_STEP_NAMES = ["شرایط", "اشتراک", "اتصال حساب", "شروع"]
_STAGE_INDEX = {"terms": 0, "plans": 1, "connect": 2, "ready": 3}


def step_bar(stage: str, trial: bool = False) -> str:
    """One-line progress strip shown on every onboarding screen so the person always knows where they are and what is next."""
    i = _STAGE_INDEX.get(stage, 0)
    names = list(_STEP_NAMES)
    if trial:
        names[1] = "اشتراک رایگان"
    parts = []
    for n, name in enumerate(names):
        parts.append(("✅ " if n < i else ("🔵 " if n == i else "▫️ ")) + name)
    return "🧭 مسیر شما:  " + "  ›  ".join(parts)


def msg_welcome_new(first_name: str = "", trial_days: float = 0) -> str:
    hello = f"سلام {first_name} عزیز 👋" if first_name else "سلام 👋"
    trial = (f"🎁 هدیه‌ی شروع: {trial_days:g} روز اشتراک رایگان آزمایشی! بدون پرداخت، ربات را روی حساب خودتان امتحان کنید.\n\n"
             if trial_days > 0 else "")
    steps = ("پذیرش شرایط ← دریافت اشتراک رایگان ← اتصال امن حساب ← شروع معامله خودکار" if trial_days > 0 else
             "پذیرش شرایط ← انتخاب و پرداخت اشتراک ← اتصال امن حساب ← شروع معامله خودکار")
    return (f"{hello}\n🤖 به ربات ترید خودکار {BRAND} خوش آمدید\n{DIVIDER}\n"
            "🚀 معاملات ارز دیجیتال را با نظم و مدیریت ریسک، خودکار روی حساب نوبیتکس خودتان اجرا کنید؛ بدون نشستن پای چارت.\n\n"
            + trial +
            "✨ امکانات ربات:\n"
            "✅ اجرای خودکار سیگنال‌های ربات روی حساب شما\n"
            "🎯 ۴ حد سود پله‌ای + حد ضرر متحرک (رانر)\n"
            "🛡 حد ضرر و تارگت‌ها روی خود نوبیتکس ثبت و بررسی می‌شوند\n"
            "📨 سیگنال شخصی: هر سیگنالی (فارسی/انگلیسی، هر سبک) را بفرستید تا ربات ورود، حد ضرر و تارگت‌ها را بخواند و اجرا کند\n"
            "⚙️ ریسک، وثیقه، اهرم و تعداد معاملات را خودتان تنظیم می‌کنید\n"
            "🤖 خروج خودکار طبق سیگنال‌ها (قابل خاموش‌کردن)\n"
            "📈 معاملات باز، تاریخچه و سود و زیان با چند دکمه\n"
            "🛑 سقف ضرر روزانه و گزارش روزانه\n"
            "🎟 کد تخفیف روی اشتراک‌ها\n"
            f"🎁 دعوت دوستان: به ازای هر دوست، {ref_phrase()} هدیه می‌گیرید\n\n"
            "🔐 امنیت: ربات فقط «خواندن» و «معامله» می‌خواهد و هیچ‌وقت برداشت نمی‌کند؛ کلید دارای برداشت (در صورت قابل‌تشخیص بودن) پذیرفته نمی‌شود و هر لحظه می‌توانید اتصال را قطع کنید.\n\n"
            f"🚦 شروع فقط ۴ قدم: {steps}\n"
            "💡 همه‌چیز با دکمه است؛ نیازی به تایپ دستور نیست. هر وقت گیج شدید «📖 راهنما» یا /start را بزنید.\n\n"
            + SIGNUP_NOTE)


def msg_blocked() -> str:
    return ("⛔ دسترسی شما به این ربات فعلاً بسته است.\nاگر فکر می‌کنید اشتباهی شده، با دکمه‌ی پشتیبانی پیام بدهید.")


def msg_declined() -> str:
    return "باشه، مشکلی نیست. تا شرایط را نپذیرید ربات هیچ کاری روی حساب شما انجام نمی‌دهد. هر وقت خواستید دوباره /start را بزنید."


def msg_plans(tiers: Dict[int, Dict[str, Any]], card_ok: bool, usdt_ok: bool, usdt_network: str = "",
              disc: Optional[Dict[str, Any]] = None) -> str:
    lines = ["💳 انتخاب اشتراک", DIVIDER, "ℹ️ برای اجرای معاملات به حساب نوبیتکس نیاز دارید؛ اگر ندارید، دکمه‌ی «🆕 ثبت‌نام در نوبیتکس» پایین است.", ""]
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
    lines += ["", "💡 نکته‌ها:",
              "• بعد از انتخاب، شماره کارت/آدرس کیف پول با دکمه‌ی «کپی» آماده می‌شود تا بدون اشتباه کپی کنید.",
              "• کد تخفیف دارید؟ قبل از انتخاب مدت، «🎟 کد تخفیف دارم» را بزنید.",
              "• پرداخت پس از بررسی ادمین فعال می‌شود؛ شمارش روزها از لحظه‌ی اتصال حساب شروع می‌شود، نه زودتر.",
              f"• 🎁 با دعوت دوستان، به ازای هر دعوت موفق {ref_phrase()} می‌گیرید."]
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
            "معمولاً چند دقیقه تا چند ساعت طول می‌کشد.\n"
            "💡 تا آن موقع می‌توانید حساب نوبیتکس را وصل کنید (🔑) یا با «🎁 دعوت دوستان» اشتراک رایگان بگیرید. راهنمای کامل: «📖 راهنما».")


CONNECT_GUIDE = (
    "🔑 اتصال امن حساب نوبیتکس\n"
    f"{DIVIDER}\n"
    "0️⃣ حساب نوبیتکس ندارید؟ اول از دکمه‌ی «ثبت‌نام در نوبیتکس» ثبت‌نام و احراز هویت را انجام دهید، بعد ادامه دهید.\n\n"
    "📍 مسیر ساخت کلید API در نوبیتکس (۲ دقیقه):\n"
    "1️⃣ وارد حساب نوبیتکس شوید و بالای صفحه روی آیکون آدمک (پروفایل) بزنید.\n"
    "2️⃣ «تنظیمات» ← «خدمات API» ← ساخت کلید جدید.\n"
    "3️⃣ فقط دو دسترسی را روشن کنید: ✅ خواندن (READ) و ✅ معامله (TRADE).\n"
    "   ⛔ دسترسی برداشت (WITHDRAW) را حتماً خاموش بگذارید؛ ربات هیچ‌وقت برداشت نمی‌کند و کلید دارای برداشت (در صورت قابل‌تشخیص بودن) رد می‌شود.\n"
    "4️⃣ ⛔ «محدودیت IP» و «محدودیت زمانی/تاریخ انقضا» را هیچ‌کدام تنظیم نکنید (خالی بگذارید). اگر IP یا انقضا بگذارید ربات نمی‌تواند وصل بماند و معاملات متوقف می‌شود.\n"
    "5️⃣ کلید عمومی (Key) و کلید خصوصی (Secret) را کامل کپی کنید. کلید خصوصی فقط یک‌بار نشان داده می‌شود؛ اگر گم شد کلید جدید بسازید.\n"
    "6️⃣ همین‌جا اول کلید عمومی و بعد کلید خصوصی را بفرستید (هر کدام در یک پیام، بدون هیچ متن اضافه).\n\n"
    "🔒 امنیت: کلید شما قبل از ارسال رمزنگاری می‌شود و پیام حاوی آن از چت پاک می‌شود.\n\n"
    "💰 مهم: برای انجام معامله، USDT را از کیف پول «اسپات» به کیف پول «تعهدی (Margin)» نوبیتکس منتقل کنید. "
    "ربات فقط از موجودی کیف پول تعهدی معامله می‌کند؛ اگر USDT فقط در اسپات باشد هیچ معامله‌ای باز نمی‌شود.\n"
    "🧮 نمی‌دانید چقدر منتقل کنید و تنظیمات چه باشد؟ بعد از اتصال دکمه‌ی «تنظیمات پیشنهادی» را بزنید (یا همین الان از راهنما، مبلغ دلخواه را بدهید).\n\n"
    "✨ بعد از اتصال: سیگنال‌ها خودکار اجرا می‌شوند، با «⚙️ تنظیمات» ریسک و اهرم را خودتان تعیین می‌کنید و می‌توانید سیگنال شخصی هم بفرستید.\n\n"
    "آماده‌اید؟ دکمه‌ی زیر را بزنید؛ ربات مرحله‌به‌مرحله فقط همان چیزی را که لازم است می‌پرسد."
)


def msg_connected(days_left: float, balance=None, perms_unverified: bool = False) -> str:
    lines = ["🎉 اتصال با موفقیت انجام شد!", DIVIDER,
             f"⏳ اشتراک شما فعال است — {days_left:.1f} روز باقی مانده.",
             "از این لحظه سیگنال‌های جدید کانال روی حساب شما اجرا می‌شود.",
             "✅ اهرم ۱۰ و خروج خودکار از همین الان روشن‌اند."]
    if balance is not None:
        lines.append(f"💰 موجودی آزاد Margin: {num(balance, 2)} USDT")
        if _dec(balance) is not None and _dec(balance) < Decimal("5"):
            lines.append("⚠️ موجودی کم است؛ برای اینکه سیگنال‌ها اجرا شوند، USDT را از کیف پول «اسپات» به «تعهدی (Margin)» نوبیتکس منتقل کنید.")
    if perms_unverified:
        lines.append("🔐 مجوزهای کلید از سمت نوبیتکس قابل‌خواندن نبود؛ مطمئن شوید هنگام ساخت کلید «برداشت» خاموش بوده (ربات هیچ‌وقت برداشت نمی‌کند).")
    lines += ["", "🧭 قدم بعدی (۱ دقیقه): بودجه‌ی خود را بگویید تا ربات ریسک، وثیقه و تعداد معاملات را متناسب با آن بچیند 👇"]
    return "\n".join(lines)


def msg_main_menu(status_fa: str, days_left: Optional[float], open_n: int, paused: bool, balance=None,
                  daily_pnl=None, name: str = "") -> str:
    lines = [f"📋 منوی من" + (f" — {name}" if name else ""), DIVIDER, f"وضعیت اشتراک: {status_fa}"]
    if days_left is not None:
        lines.append(f"⏳ روزهای باقی‌مانده: {days_left:.1f}")
    lines.append(f"📈 معاملات باز: {open_n}")
    lines.append("📡 سیگنال‌دهی: " + ("🔕 خاموش (توسط شما)" if paused else "🟢 روشن"))
    if balance is not None:
        lines.append(f"💰 موجودی آزاد Margin: {num(balance, 2)} USDT")
    if daily_pnl is not None:
        lines.append(f"📅 نتیجه‌ی امروز: {usdt(daily_pnl, 4)}")
    lines.append("\nهر کاری لازم دارید با دکمه‌های زیر انجام دهید 👇")
    return "\n".join(lines)


HELP_TOPICS = ["start", "how", "signal", "settings", "rec", "manage", "sub", "safe", "faq"]

HELP_BUTTONS = [
    ("🚀 شروع کار", "start"), ("🤖 ربات چطور معامله می‌کند؟", "how"),
    ("📨 سیگنال شخصی", "signal"), ("⚙️ تنظیمات و ریسک", "settings"),
    ("🧮 تنظیمات مناسب موجودی", "rec"), ("📈 مدیریت معاملات", "manage"), ("💳 اشتراک، تخفیف و دعوت", "sub"),
    ("🔐 امنیت و پول شما", "safe"), ("❓ پرسش‌های پرتکرار", "faq"),
]

_HELP_PAGES = {
    "start": (
        "🚀 شروع کار (۴ قدم)\n" + DIVIDER + "\n"
        "1️⃣ شرایط: خواندن و «✅ می‌پذیرم».\n"
        "2️⃣ اشتراک: اگر هدیه‌ی رایگان فعال باشد با یک دکمه می‌گیرید؛ وگرنه مدت را انتخاب و پرداخت می‌کنید (کارت‌به‌کارت یا USDT، با دکمه‌ی «کپی» برای شماره کارت/آدرس). کد تخفیف دارید؟ قبل از انتخاب مدت وارد کنید.\n"
        "3️⃣ اتصال امن حساب: در نوبیتکس روی آیکون آدمک (بالای صفحه) ← «تنظیمات» ← «خدمات API» کلید جدید بسازید؛ فقط «خواندن» و «معامله» روشن، برداشت خاموش و محدودیت IP خالی. سپس کلید عمومی و خصوصی را به ربات بدهید؛ ربات پیام حاوی کلید را از چت پاک می‌کند.\n"
        "4️⃣ شروع: از همین لحظه سیگنال‌های جدید روی حساب شما اجرا می‌شوند.\n\n"
        "✅ پیش‌نیازها: حساب نوبیتکس احراز هویت‌شده با معاملات تعهدی (Margin).\n"
        "💰 مهم: USDT را از کیف پول «اسپات» به کیف پول «تعهدی (Margin)» منتقل کنید؛ ربات فقط از موجودی تعهدی معامله می‌کند.\n"
        "🆕 حساب نوبیتکس ندارید؟ دکمه‌ی «ثبت‌نام در نوبیتکس» پایین است.\n"
        "🔒 هر جا گیر کردید /start را بزنید؛ ربات شما را به مرحله‌ی درست برمی‌گرداند."),
    "how": (
        "🤖 ربات چطور معامله می‌کند؟\n" + DIVIDER + "\n"
        "• سیگنال‌های کانال را خودکار روی حساب نوبیتکس خودِ شما اجرا می‌کند.\n"
        "• ورود با حد ضرر؛ سپس ۴ حد سود پله‌ای (۲۰٪ / ۳۰٪ / ۱۵٪ / ۱۰٪) و یک «رانر» ۲۵٪ با حد ضرر متحرک.\n"
        "• با رسیدن به هر تارگت، حد ضرر طبق استراتژی به جای امن‌تر می‌رود (مثلاً سر به سر).\n"
        "• حد ضرر و تارگت‌ها روی خود نوبیتکس ثبت و بعدش دوباره بررسی می‌شوند؛ اگر چیزی ناقص بماند ربات خودش ترمیم می‌کند و به شما خبر می‌دهد.\n"
        "• اگر سهم یک تارگت از حداقل سایز نوبیتکس کوچک‌تر باشد، آن سهم به رانر اضافه می‌شود تا معامله رها نشود.\n"
        "• پیام‌های ربات هر مرحله را می‌گویند: ورود، تارگت خورد، حد ضرر جابه‌جا شد، معامله بسته شد.\n"
        "• «خروج خودکار»: وقتی کانال خروج یا انتقال حد ضرر اعلام کند، ربات اجرا می‌کند (در تنظیمات قابل خاموش‌کردن)."),
    "signal": (
        "📨 سیگنال شخصی\n" + DIVIDER + "\n"
        "هر سیگنالی دارید (فارسی یا انگلیسی، هر سبک) را همین‌جا بنویسید یا فوروارد کنید. ربات نماد، جهت، ورود، حد ضرر و تارگت‌ها را می‌خواند و با تنظیمات خودِ شما اجرا می‌کند.\n\n"
        "مثال:\nBTCUSDT LONG\nEntry: 60000\nSL: 59000\nTP1: 61000\nTP2: 62000\n\n"
        "• ورود به‌صورت سفارش Limit در قیمت ورود ثبت می‌شود و تا پر شدن در «سفارش‌های در انتظار» می‌ماند؛ برای ورود فوری کلمه‌ی market یا «بازار» بنویسید.\n"
        "• بعد از پر شدن، حد ضرر و تارگت‌ها روی نوبیتکس ثبت می‌شوند. سهم تارگت‌ها: ۱ تارگت ۱۰۰٪؛ ۲ تارگت ۵۰/۵۰؛ ۳ تارگت ۴۰/۳۰/۳۰؛ ۴ تارگت ۳۰/۳۰/۲۰/۲۰.\n"
        "• اگر چیزی مبهم یا ناهماهنگ باشد (ورود به‌صورت بازه، حد ضرر سمت اشتباه، قیمت خیلی دور از ورود)، ربات حدس نمی‌زند: دلیل را می‌گوید و سفارشی ثبت نمی‌کند.\n"
        "• سفارش در انتظار را از «📨 ارسال سیگنال شخصی» می‌توانید لغو کنید."),
    "settings": (
        "⚙️ تنظیمات و مدیریت ریسک\n" + DIVIDER + "\n"
        "از «⚙️ تنظیمات» با دکمه (بدون تایپ دستور) این‌ها را تعیین می‌کنید:\n"
        "🎯 ریسک هر معامله: حداکثر ضرر تا حد ضرر. کمتر = ضرر هر معامله کمتر.\n"
        "💵 سقف وثیقه: حداکثر سرمایه‌ای که یک معامله قفل می‌کند.\n"
        "📈 اهرم؛ 🔢 حداکثر معاملات همزمان.\n"
        "🛑 سقف ضرر روزانه: با رسیدن به آن، معامله‌ی جدید باز نمی‌شود.\n"
        "📰 گزارش روزانه؛ 🤖 خروج خودکار؛ 🔔/🔕 روشن/خاموش کردن سیگنال‌دهی.\n\n"
        "💡 هر مقدار تا سقفی که ادمین برای شما گذاشته آزاد است؛ بالاتر از آن می‌توانید با یک دکمه از ادمین درخواست افزایش بدهید.\n"
        "💡 تغییر تنظیمات فقط روی معاملات جدید اثر دارد. موجودی Margin باید برای وثیقه کافی باشد؛ اگر کم باشد ربات خبر می‌دهد."),
    "rec": (
        "🧮 تنظیمات مناسب موجودی کیف پول تعهدی\n" + DIVIDER + "\n"
        "ربات با موجودی «کیف پول تعهدی (Margin)» معامله می‌کند؛ پس اول USDT را از «اسپات» به «تعهدی» منتقل کنید.\n\n"
        "قاعده‌ی ساده‌ی پیشنهادی: ریسک هر معامله حدود ۱٪ بودجه، اهرم ۱۰، چند معامله‌ی همزمان محدود و سقف ضرر روزانه حدود ۳٪. بعد از اتصال حساب، ربات خودش بودجه را می‌پرسد و فقط با تأیید شما اعمال می‌کند.\n"
        "• 🎯 ریسک: هر چه کمتر، ضرر هر معامله کمتر.\n"
        "• 💵 سقف وثیقه: پولی که برای یک معامله قفل می‌شود؛ ربات حجم را طوری کم می‌کند که از آن نگذرد.\n"
        "• 📈 اهرم: حداکثر ممکن ۱۰ است و پیش‌فرض ربات هم همین است؛ چون بودجه معمولاً محدود است، وثیقه‌ی کمتری قفل می‌شود. فاصله‌ی لیکویید در اهرم ۱۰ حدود ۱۰٪ است (اهرم ۵ حدود دو برابر امن‌تر).\n"
        "• 🔢 حداکثر معاملات همزمان: بیشتر = ریسک کل بیشتر.\n\n"
        "👇 با دکمه‌ی «🧮 محاسبه برای موجودی من» (یا «مبلغ دلخواه») عددهای دقیق را می‌بینید؛ بعد از اتصال هم یک‌دکمه‌ای اعمال می‌شود.\n"
        "⚠️ این پیشنهاد آموزشی است و تضمین سود نیست؛ مسئولیت نهایی تنظیمات با شماست."),
    "manage": (
        "📈 مدیریت معاملات\n" + DIVIDER + "\n"
        "• 📈 معاملات باز: وضعیت هر معامله، سود/زیان لحظه‌ای، تارگت‌های خورده و دکمه‌ی بستن.\n"
        "• 📜 تاریخچه و 📊 سود و زیان: نتیجه‌ی معاملات بسته‌شده و جمع روزانه.\n"
        "• 💰 موجودی من: Margin و Spot.\n"
        "• 🛡 افزایش فاصله تا لیکویید: وثیقه‌ی یک معامله را معقول بالا می‌برد؛ 💵 مبلغ تعهد همه: همین کار برای همه‌ی معاملات باز با درصد دلخواه.\n"
        "• ❌ بستن معاملات: تک‌تک یا همه‌ی آن‌ها، با تأیید دومرحله‌ای (سفارش‌های در انتظار هم لغو می‌شوند).\n"
        "• 🔕 خاموش کردن سیگنال‌دهی: معامله‌ی جدید باز نمی‌شود ولی معاملات باز مدیریت می‌شوند.\n"
        "⚠️ سفارش‌ها و حد ضرر ربات را در اپ نوبیتکس دستی تغییر ندهید؛ مدیریت خودکار به هم می‌ریزد."),
    "sub": (
        "💳 اشتراک، تخفیف و دعوت\n" + DIVIDER + "\n"
        "• 🔔 اشتراک من: وضعیت، روزهای باقی‌مانده و پرداخت‌های اخیر.\n"
        "• 💳 تمدید اشتراک: مدت را انتخاب کنید؛ شماره کارت یا آدرس کیف پول با دکمه‌ی «کپی» می‌آید. بعد از واریز «✅ پرداخت کردم» را بزنید و کد رهگیری/هش را بفرستید؛ نتیجه‌ی بررسی ادمین همین‌جا اعلام می‌شود.\n"
        "• 🎟 کد تخفیف: قبل از انتخاب مدت وارد کنید؛ قیمت تخفیف‌خورده همان‌جا دیده می‌شود. هر کد برای هر نفر یک‌بار است.\n"
        "• 🎁 اشتراک رایگان آزمایشی: اگر فعال باشد، هنگام ثبت‌نام می‌گیرید.\n"
        "• 🎁 دعوت دوستان: با لینک اختصاصی، به ازای هر دوستی که اولین اشتراکش تأیید شود {REF} به اشتراک شما اضافه می‌شود{INV}.\n"
        "• با پایان اشتراک فقط معامله‌ی جدید متوقف می‌شود؛ معاملات باز تا بسته شدن مدیریت می‌شوند."),
    "safe": (
        "🔐 امنیت و پول شما\n" + DIVIDER + "\n"
        "• ربات فقط «خواندن» و «معامله» می‌خواهد و هیچ‌وقت برداشت نمی‌کند؛ کلید دارای برداشت (Withdraw) در صورت قابل‌تشخیص بودن رد می‌شود.\n"
        "• کلیدها رمزنگاری می‌شوند و پیام حاوی کلید از چت پاک می‌شود؛ ادمین کلید خام شما را نمی‌بیند.\n"
        "• هر لحظه با «🔌 قطع اتصال» کلید را از سیستم حذف می‌کنید.\n"
        "• پول در حساب نوبیتکس خودتان می‌ماند.\n"
        "⚠️ سود تضمین نیست و معامله‌ی اهرمی ریسک دارد؛ ریسک را با بودجه‌ی خودتان تنظیم کنید."),
    "faq": (
        "❓ پرسش‌های پرتکرار\n" + DIVIDER + "\n"
        "• چرا سیگنال اجرا نشد؟ معمولاً موجودی Margin کم است، سیگنال‌دهی خاموش است، اشتراک تمام شده، یا به سقف «حداکثر معاملات»/«ضرر روزانه» رسیده‌اید. ربات دلیل را در پیام می‌گوید.\n"
        "• اشتراک من از کی شروع می‌شود؟ از لحظه‌ی اتصال موفق کلید.\n"
        "• سیگنال اجرا نشد و موجودی دارم؟ مطمئن شوید USDT در کیف پول «تعهدی (Margin)» است نه فقط «اسپات».\n"
        "• کلید پذیرفته نشد؟ دوباره بسازید: فقط خواندن + معامله، برداشت خاموش، محدودیت IP خالی؛ کلید عمومی و خصوصی را کامل بفرستید.\n"
        "• تغییر تنظیمات روی معاملات باز اثر دارد؟ نه، فقط معاملات جدید.\n"
        "• ربات جواب نداد؟ چند ثانیه صبر کنید؛ نشانگر «⏳ در حال انجام…» تا رسیدن پاسخ می‌ماند. اگر نیامد /start را بزنید.\n"
        "• قیمت خروج با حد ضرر فرق دارد؟ در نوسان شدید ممکن است (Slippage).\n"
        "• سؤال دیگری دارید؟ «🆘 پشتیبانی» پیام مستقیم به ادمین است."),
}


def msg_help_user() -> str:
    """Help hub: a short map of everything the bot can do; each topic opens as its own short page."""
    return ("📖 راهنمای ربات\n" + DIVIDER + "\n"
            "همه‌چیز با دکمه است. موضوع مورد نظرتان را انتخاب کنید 👇\n\n"
            "• 🚀 شروع کار: ۴ قدم تا معامله‌ی خودکار\n"
            "• 🤖 ربات چطور معامله می‌کند؟ ورود، حد ضرر، ۴ تارگت و رانر\n"
            "• 📨 سیگنال شخصی: سیگنال خودتان را بفرستید تا اجرا شود\n"
            "• ⚙️ تنظیمات و ریسک: ریسک، وثیقه، اهرم، سقف ضرر روزانه\n"
            "• 🧮 تنظیمات مناسب موجودی: با هر مبلغ تتر، ریسک و وثیقه و اهرم مناسب را ببینید\n"
            "• 📈 مدیریت معاملات: باز، تاریخچه، سود و زیان، بستن، افزایش ایمنی\n"
            "• 💳 اشتراک، تخفیف و دعوت\n"
            "• 🔐 امنیت و پول شما\n"
            "• ❓ پرسش‌های پرتکرار\n\n"
            "🔒 اگر جایی گیر کردید /start را بزنید؛ ربات شما را به مرحله‌ی درست برمی‌گرداند.\n\n" + SIGNUP_NOTE)


def _fill_ref(t: str) -> str:
    inv = ref_invitee_days()
    return t.replace("{REF}", ref_phrase()).replace("{INV}", (f" و دوستتان هم {inv:g} روز هدیه می‌گیرد" if inv > 0 else ""))


def msg_help_topic(topic: str) -> str:
    page = _HELP_PAGES.get(str(topic).lower())
    return _fill_ref(page) if page else msg_help_user()


# One short rotating tip on the main menu so features are discovered gradually, never as a wall of text.
MENU_TIPS = [
    "📨 می‌توانید سیگنال خودتان را همین‌جا بفرستید یا فوروارد کنید تا ربات اجرا کند.",
    "🛑 با «سقف ضرر روزانه» در تنظیمات، ضرر یک روز بدخیم محدود می‌ماند.",
    "🎁 با «دعوت دوستان» برای هر دعوت موفق {REF} می‌گیرید.",
    "🧮 «تنظیمات پیشنهادی» ریسک و اهرم مناسب موجودی تعهدی‌تان را حساب می‌کند.",
    "💰 ربات فقط از کیف پول «تعهدی» معامله می‌کند؛ USDT را از اسپات به تعهدی منتقل کنید.",
    "🛡 «افزایش فاصله تا لیکویید» وثیقه‌ی یک معامله را معقول بالا می‌برد.",
    "📰 «گزارش روزانه» را از تنظیمات روشن کنید تا هر شب خلاصه‌ی نتیجه برسد.",
    "🤖 «خروج خودکار» را در تنظیمات خاموش/روشن می‌کنید؛ کانال خروج را اعلام کند ربات اجرا می‌کند.",
    "🎟 کد تخفیف دارید؟ هنگام تمدید، قبل از انتخاب مدت وارد کنید.",
    "📖 «راهنما» هر امکان ربات را با زبان ساده توضیح می‌دهد.",
]


def menu_tip(seed: int = 0) -> str:
    try:
        return _fill_ref(MENU_TIPS[int(seed) % len(MENU_TIPS)])
    except Exception:
        return ""



# --------------------------------------------------------------------------- settings advisor
def recommend_settings(balance, max_lev: float = 10.0):
    """Per-trade settings for a Margin (تعهدی) USDT budget, or None when it is too small to trade.
    Mirrors the executor's sizing rule: loss at stop = min(risk, collateral x stop% x leverage), so the collateral needed
    for the full risk at a typical 1.5% stop is risk / (0.015 x leverage). The plan uses the maximum leverage (10x, the
    bot's default - budgets are usually small, and higher leverage means less collateral locked per trade) with about 1%
    of the budget at risk per trade, a few concurrent trades and a daily loss cap of about 3 trades' worth. 'safer' is the
    same risk at 5x (more collateral per trade, liquidation twice as far)."""
    try:
        b = float(balance)
    except (TypeError, ValueError):
        return None
    if not (b == b) or b < 5:
        return None

    def col_for(risk: float, lev: float, n: int) -> float:
        return round(max(1.0, min(b / n, risk / (0.015 * lev))), 1)

    risk = round(max(0.1, b * 0.01), 2)
    n = 3 if b < 50 else (5 if b < 500 else 6)
    top = float(max_lev)
    plan = {"risk": risk, "leverage": top, "max_open": n, "collateral": col_for(risk, top, n), "daily": round(risk * 3, 2)}
    safer = {"risk": risk, "leverage": 5.0, "max_open": n, "collateral": col_for(risk, 5.0, n), "daily": round(risk * 3, 2)}
    return {"balance": b, "plan": plan, "safer": safer, "balanced": plan, "max_lev": plan}


def msg_recommend(balance, caps: Optional[Dict[str, Any]] = None, connected: bool = False, applied: str = "") -> str:
    r = recommend_settings(balance)
    if r is None:
        return ("🧮 تنظیمات مناسب بودجه\n" + DIVIDER + "\n"
                "بودجه‌ی کیف پول تعهدی (Margin) کمتر از ۵ USDT است و برای معامله‌ی خودکار کافی نیست.\n"
                "💰 USDT را از کیف پول «اسپات» به کیف پول «تعهدی (Margin)» نوبیتکس منتقل کنید (انتقال از بخش کیف پول‌ها)، سپس دوباره مبلغ را بگویید.\n"
                "برای دیدن عدد یک مبلغ دلخواه، دکمه‌ی «✏️ مبلغ دلخواه» را بزنید.")
    b, a, sf = r["balance"], r["plan"], r["safer"]
    lines = ["🧮 تنظیمات پیشنهادی برای بودجه‌ی شما", DIVIDER, f"💰 بودجه‌ی کیف پول تعهدی: {num(b, 2)} USDT", ""]
    if applied:
        lines += [applied, ""]
    lines += [
        f"✅ پیشنهاد ربات (اهرم {a['leverage']:g}):",
        f"📈 اهرم: {a['leverage']:g}x (حداکثر اهرم ممکن؛ چون بودجه معمولاً محدود است وثیقه‌ی کمتری قفل می‌شود)",
        f"🎯 ریسک هر معامله: {a['risk']:g} USDT (حدود ۱٪ بودجه)",
        f"💵 سقف وثیقه‌ی هر معامله: {a['collateral']:g} USDT",
        f"🔢 حداکثر معاملات همزمان: {a['max_open']}",
        f"🛑 سقف ضرر روزانه: {a['daily']:g} USDT",
        "🤖 خروج خودکار: روشن",
        "",
        f"🛡 نسخه‌ی محتاط‌تر: اهرم {sf['leverage']:g}x با وثیقه‌ی {sf['collateral']:g} USDT (فاصله‌ی لیکویید حدود دو برابر).",
        "",
        "ℹ️ در اهرم ۱۰ فاصله‌ی لیکویید حدود ۱۰٪ حرکت خلاف جهت است؛ حد ضرر سیگنال‌ها خیلی نزدیک‌تر از آن است و ربات حجم را طوری می‌بندد که ضرر هر معامله از «ریسک» بالاتر نرود.",
        "💰 ربات فقط از کیف پول «تعهدی» معامله می‌کند؛ مطمئن شوید همین مبلغ آنجاست.",
        "⚠️ این پیشنهاد آموزشی است و تضمین سود نیست؛ با زدن «تأیید» مسئولیت تنظیمات با شماست.",
    ]
    if caps:
        lines.append(f"🔒 سقف‌های فعلی شما: ریسک {caps.get('risk')}، وثیقه {caps.get('col')}، اهرم {caps.get('lev')}x؛ تأیید این پیشنهاد سقف‌ها را در صورت نیاز بالا می‌برد.")
    if not connected:
        lines.append("🔑 بعد از اتصال حساب، با یک دکمه همین تنظیمات اعمال می‌شود.")
    return "\n".join(lines)


def msg_budget_ask(balance=None, perms_unverified: bool = False) -> str:
    lines = ["💰 قدم بعدی: بودجه‌ی شما", DIVIDER,
             "چه مبلغی USDT در کیف پول «تعهدی (Margin)» نوبیتکس دارید یا می‌خواهید برای ربات بگذارید؟",
             "با دکمه‌ی زیر مبلغ را انتخاب کنید؛ ربات ریسک هر معامله، سقف وثیقه و تعداد معاملات همزمان را متناسب با آن می‌چیند (اهرم ۱۰ و خروج خودکار هم روشن است) و فقط با تأیید خودتان اعمال می‌کند.",
             "💡 اگر USDT هنوز در «اسپات» است، آن را به «تعهدی» منتقل کنید؛ ربات فقط از کیف پول تعهدی معامله می‌کند."]
    if balance is not None:
        lines.append(f"📊 موجودی تعهدی که ربات الان می‌بیند: {num(balance, 2)} USDT")
    if perms_unverified:
        lines.append("🔐 یادآوری: هنگام ساخت کلید «برداشت» باید خاموش و «محدودیت IP» و «تاریخ انقضا» خالی باشد (ربات هیچ‌وقت برداشت نمی‌کند).")
    return "\n".join(lines)


def status_fa(status: str) -> str:
    return {"active": "🟢 فعال", "pending_connect": "🟡 منتظر اتصال کلید", "suspended": "🟠 معلق",
            "expired": "🔴 منقضی", "new": "⚪ ثبت‌نام‌شده (بدون اشتراک)", "blocked": "⛔ مسدود"}.get(str(status), str(status))


def msg_subscription(rec: Dict[str, Any], open_n: int, payments: List[str]) -> str:
    lines = ["🔔 وضعیت اشتراک من", DIVIDER, f"وضعیت: {status_fa(rec['status'])}" + (" (سیگنال‌دهی را خودتان خاموش کرده‌اید)" if rec.get("paused") else "")]
    if rec.get("trial_active"):
        lines.append(f"🎁 دوره‌ی رایگان آزمایشی ({rec.get('trial_days', 0):g} روز هدیه) — برای ادامه پس از پایان آن «💳 تمدید اشتراک» را بزنید.")
    if rec["status"] == "active":
        lines.append(f"⏳ روزهای باقیمانده: {rec['days_left']:.1f}")
        lines.append(f"📅 تاریخ پایان: {rec['expires_date']}")
    elif rec["status"] == "pending_connect":
        lines.append(f"🎁 روزهای در انتظار (بعد از اتصال کلید شروع می‌شود): {rec['pending_days']:.0f}")
    elif rec["status"] == "expired":
        lines.append("اشتراک منقضی شده؛ سیگنال‌دهی متوقف است. با «💳 تمدید اشتراک» ادامه دهید.")
    lines.append(f"🔑 کلید API: {'متصل ✅' if rec['connected'] else 'متصل نشده ❌'}")
    lines.append(f"📈 معاملات باز: {open_n}")
    if payments:
        lines += ["", "💳 پرداخت‌های اخیر:"] + payments
    return "\n".join(lines)


def msg_settings(rec: Dict[str, Any]) -> str:
    return "\n".join([
        "⚙️ تنظیمات معامله‌ی من", DIVIDER,
        "📡 سیگنال‌دهی: " + ("🔕 خاموش (سیگنال جدید اجرا نمی‌شود)" if rec.get("paused") else "🟢 روشن"),
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


def msg_expired_hint(was_trial: bool = False) -> str:
    if was_trial:
        return ("⏳ دوره‌ی رایگان آزمایشی شما تمام شد. امیدواریم تجربه‌ی خوبی داشته باشید 🌱 "
                "معاملات باز تا بسته شدن مدیریت می‌شوند؛ برای ادامه‌ی معامله‌ی خودکار، اشتراک تهیه کنید (دکمه‌ی «💳 تمدید اشتراک»).")
    return "⏳ اشتراک شما تمام شده است. معاملات باز تا بسته شدن مدیریت می‌شوند؛ برای معامله‌ی جدید، اشتراک را تمدید کنید."


# --------------------------------------------------------------------------- admin cards
def msg_mysignal_help(trial_note: str = "") -> str:
    return ("📨 ارسال سیگنال شخصی\n" + DIVIDER + "\n"
            "سیگنال را همین‌جا بنویسید یا از کانال/گروه فوروارد کنید؛ فارسی یا انگلیسی، با هر سبک نوشتاری. مثال‌ها:\n\n"
            "BTCUSDT LONG\nEntry: 60000\nSL: 59000\nTP1: 61000\nTP2: 62000\n\n"
            "سیگنال خرید #SOL\nقیمت ورود: ۱۵۰\nحد ضرر: ۱۴۵\nتارگت اول: ۱۵۵\nتارگت دوم: ۱۶۰\n\n"
            "برای ورود فوری (قیمت بازار) کلمه‌ی market یا «بازار» بنویسید.\n\n"
            "✅ ربات خودش نماد، جهت (خرید/فروش)، ورود، حد ضرر و تارگت‌ها را تشخیص می‌دهد و با ریسک و تنظیمات خودِ شما "
            "روی حساب نوبیتکستان اجرا می‌کند؛ حد ضرر و تارگت‌ها بعد از پر شدن ورود روی نوبیتکس ثبت می‌شوند.\n"
            "🛡 اگر چیزی مبهم یا ناهماهنگ باشد (مثلاً ورود به‌صورت بازه، حد ضرر سمت اشتباه، چند حد ضرر مختلف) "
            "ربات به‌جای حدس زدن، دلیل را می‌گوید و هیچ سفارشی ثبت نمی‌کند." + trial_note)


def card_new_user_admin(uid: str, name: str, username: str, trial_days: float = 0, terms_accepted: bool = True) -> str:
    lines = ["👤 کاربر جدید وارد ربات شد", DIVIDER, f"شناسه: {uid}",
             f"نام: {name or '—'}", f"یوزرنیم: @{username}" if username else "یوزرنیم: —",
             "📜 شرایط: پذیرفته شد ✅" if terms_accepted else "📜 شرایط: هنوز نپذیرفته ⏳"]
    if trial_days > 0:
        lines.append(f"🎁 اشتراک رایگان آزمایشی {trial_days:g} روزه برای او فعال شد؛ بعد از اتصال حساب شروع به شمارش می‌کند.")
    else:
        lines.append("💳 اشتراکی ندارد؛ تا وقتی اشتراک نگیرد هیچ معامله‌ای برایش باز نمی‌شود.")
    lines += ["", "می‌توانید اشتراک هدیه بدهید یا او را مسدود کنید؛ جزئیات کامل در «👤 پنل کاربر»:"]
    return "\n".join(lines)


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


def card_user_full(d: Dict[str, Any]) -> str:
    """Complete admin view of one subscriber: identity, subscription, connection, settings, trades, payments, activity."""
    def yn(v) -> str:
        return "✅" if v else "❌"
    L = [f"👤 کاربر {d['user_id']}" + (f" — {d['name']}" if d.get("name") else ""), DIVIDER]
    L.append("🪪 هویت: " + " | ".join(x for x in (f"@{d['username']}" if d.get("username") else "", f"چت: {d['chat_id']}" if d.get("chat_id") else "") if x) or "🪪 هویت: —")
    L += ["", "💳 اشتراک",
          f"• وضعیت: {status_fa(d['status'])}" + (" | 🔕 سیگنال‌دهی خاموش (توسط کاربر)" if d.get("paused") else "")]
    if d["status"] == "active":
        L.append(f"• باقیمانده: {d['days_left']:.1f} روز (تا {d['expires_date']})")
    elif d["status"] == "pending_connect":
        L.append(f"• روزهای در انتظار اتصال: {d['pending_days']:.0f}")
    L.append(f"• شرایط پذیرفته: {yn(d['terms'])}   | پرداخت تأییدشده: {yn(d.get('has_paid'))}   | اشتراک رایگان: "
             + (f"دریافت شد ({d['trial_days']:g} روز)" if d.get("trial_days") else ("برداشته شد" if d.get("trial_revoked") else "—")))
    L += ["", "🔑 اتصال نوبیتکس",
          f"• کلید: {'متصل ✅' if d['connected'] else 'متصل نشده ❌'}" + (f"   (از {d['connected_date']})" if d.get("connected_date") else "")]
    if d["connected"]:
        if d.get("perms"):
            L.append(f"• مجوزهای کلید: {d['perms']} " + ("(تأییدشده توسط نوبیتکس ✅)" if d.get("perms_verified") else "(تأیید نشد ⚠️)"))
        else:
            L.append("• مجوزهای کلید: ⚠️ نوبیتکس فهرست مجوزها را نداد (احراز هویت کلید موفق بود؛ بررسی دستی «برداشت خاموش» توصیه می‌شود)")
        L.append(f"• آخرین بررسی اتصال: {d.get('checked') or '—'}" + (f" — {d['check_note']}" if d.get("check_note") else ""))
        L.append("• سلامت کلید: " + ("🚨 نامعتبر/لغو شده" if d.get("key_bad") else "سالم ✅"))
        if d.get("balance"):
            L.append(f"• آخرین موجودی Margin دیده‌شده: {d['balance']} USDT ({d.get('balance_at') or '—'})")
    if d.get("last_error"):
        L.append(f"• ⚠️ آخرین خطا: {d['last_error']}")
    L += ["", "⚙️ تنظیمات (مقدار/سقف)",
          f"• 🎯 ریسک {d['risk']}/{d['cap_risk']}  | 💵 وثیقه {d['collateral']}/{d['cap_collateral']}",
          f"• 📈 اهرم {d['leverage']}/{d['cap_leverage']}x  | 🔢 حداکثر معاملات {d['max_open']}/{d['cap_max_open']}",
          f"• 🛑 سقف ضرر روزانه: {d['daily_loss'] if d['daily_loss'] not in ('0', '', None) else 'خاموش'}   | 📰 گزارش روزانه: {yn(d.get('daily_report'))}   | 🤖 خروج خودکار: {yn(d.get('auto_exit'))}",
          "• راهنمای بودجه: " + (f"تأیید شد ({d['confirmed_date']}) — بودجه‌ی اعلام‌شده {d['budget']} USDT" if d.get("confirmed_date") else "هنوز تأیید نکرده ⏳")]
    if d.get("cap_request"):
        L.append(f"• 📨 درخواست افزایش سقف در انتظار: {d['cap_request']}")
    L += ["", "📈 معاملات",
          f"• باز: {d['open_n']}   | سفارش در انتظار: {d.get('pending_n', 0)}   | بسته‌شده: {d.get('closed_n', 0)}"
          + (f" (برد {d.get('wins', 0)} / باخت {d.get('losses', 0)})" if d.get("closed_n") else ""),
          f"• سود/زیان کل ثبت‌شده: {d.get('total_pnl', '—')} USDT   | امروز: {d.get('today_pnl', '—')} USDT"]
    for t in d.get("open_list", [])[:6]:
        L.append("  ◦ " + t)
    if d.get("last_trade"):
        L.append(f"• آخرین معامله‌ی بسته‌شده: {d['last_trade']}")
    if d.get("pay_lines"):
        L += ["", "💳 پرداخت‌ها:"] + d["pay_lines"]
    L += ["", "🎁 رشد",
          f"• دعوت‌شده توسط: {d.get('referred_by') or '—'}   | دعوت‌های موفق: {d.get('ref_count', 0)} (هدیه: {d.get('ref_days', 0):g} روز)",
          f"🕒 آخرین فعالیت: {d.get('last_seen') or '—'}   | ثبت‌نام: {d.get('added_date') or '—'}"]
    return "\n".join(L)


def card_user_panel(rec: Dict[str, Any], open_n: int, pay_lines: List[str]) -> str:
    lines = [f"👤 کاربر {rec['user_id']}" + (f" ({rec['name']})" if rec.get("name") else ""), DIVIDER,
             f"وضعیت: {status_fa(rec['status'])}" + (" | 🔕 سیگنال‌دهی خاموش (توسط کاربر)" if rec.get("paused") else "")]
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
