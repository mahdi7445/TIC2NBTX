# -*- coding: utf-8 -*-
"""
حلقه‌ی اصلی پروژه‌ی سوم: کانال تلگرام مشترک را می‌خواند، پیام‌ها را پارس
می‌کند و روی نوبیتکس اجرا می‌کند.

این فایل کاملاً مستقل از bot.py و candle_engine.py است - هیچ import یا
فایل مشترکی با آن‌ها ندارد.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from typing import Any, Dict, Optional

import requests

from nobitex_client import NobitexClient, NobitexConfig, NobitexAPIError
from signal_parser import (
    parse_message,
    ParsedSignal,
    ParsedEvent,
    TARGET_CLOSE_PCT,
    RUNNER_CLOSE_PCT,
)
from state_store import load_state, save_state

try:
    # اجرای محلی روی ویندوز: تنظیمات از فایل .env کنار همین اسکریپت خوانده
    # می‌شود (به‌جای GitHub Secrets). اگر python-dotenv نصب نبود، مشکلی
    # نیست - یعنی از متغیرهای محیطی سیستم استفاده می‌شود.
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("executor.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("executor")

# ---------------------------------------------------------------------- #
# تنظیمات از متغیرهای محیطی / فایل .env
# ---------------------------------------------------------------------- #
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHANNEL_ID = os.environ["TELEGRAM_CHANNEL_ID"]  # مثلاً "-1001234567890"
TELEGRAM_ADMIN_CHAT_ID = os.environ.get("TELEGRAM_ADMIN_CHAT_ID")  # برای هشدارها

NOBITEX_PUBLIC_KEY = os.environ["NOBITEX_PUBLIC_KEY"]
NOBITEX_PRIVATE_KEY = os.environ["NOBITEX_PRIVATE_KEY"]
NOBITEX_BASE_URL = os.environ.get("NOBITEX_BASE_URL", "https://testnetapiv2.nobitex.ir")

RISK_MARGIN_USDT = float(os.environ.get("RISK_MARGIN_USDT", "1"))
LEVERAGE = os.environ.get("LEVERAGE", "10")

POLL_INTERVAL_SECONDS = int(os.environ.get("POLL_INTERVAL_SECONDS", "5"))

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"


# ---------------------------------------------------------------------- #
# کمکی‌ها
# ---------------------------------------------------------------------- #
def notify_admin(message: str) -> None:
    """هشدار به ادمین - برای پیام‌های پارس‌نشده، خطاهای اجرا و... حیاتی است."""
    log.warning("ADMIN ALERT: %s", message)
    if not TELEGRAM_ADMIN_CHAT_ID:
        return
    try:
        requests.post(
            f"{TELEGRAM_API}/sendMessage",
            json={"chat_id": TELEGRAM_ADMIN_CHAT_ID, "text": message[:4000]},
            timeout=10,
        )
    except Exception as e:  # noqa: BLE001
        log.error("Failed to notify admin: %s", e)


def symbol_to_currencies(raw_symbol: str) -> tuple[str, str]:
    """
    نگاشت نماد نمایشی پیام (مثلاً BTC یا BTCUSDT) به (srcCurrency, dstCurrency)
    نوبیتکس. فرض پیش‌فرض: بازار مقابل همیشه USDT است.

    ⚠️ این تابع باید بعد از دیدن چند پیام واقعی از دو ربات (که دقیقاً چه
    رشته‌ای در جای SYMBOL می‌گذارند - مثلاً "BTC" یا "BTC/USDT" یا "BTCUSDT")
    تایید یا اصلاح شود.
    """
    s = raw_symbol.upper().replace("/", "")
    for suffix in ("USDT", "USD", "IRT"):
        if s.endswith(suffix) and len(s) > len(suffix):
            return s[: -len(suffix)].lower(), suffix.lower()
    return s.lower(), "usdt"


def trade_key(symbol: str, timeframe: str) -> str:
    return f"{symbol.upper()}_{timeframe.upper()}"


# ---------------------------------------------------------------------- #
# منطق اصلی رویدادها
# ---------------------------------------------------------------------- #
def handle_signal(nx: NobitexClient, state: Dict[str, Any], sig: ParsedSignal) -> None:
    key = trade_key(sig.symbol, sig.timeframe)
    if key in state["open_trades"]:
        log.warning("سیگنال ورود جدید برای %s ولی معامله‌ی باز قبلی هنوز در state هست - رد شد.", key)
        notify_admin(f"⚠️ سیگنال ورود تکراری/همپوشان برای {key} - نادیده گرفته شد.")
        return

    src, dst = symbol_to_currencies(sig.symbol)

    if not nx.is_symbol_available(src, dst):
        log.info("نماد %s%s در نوبیتکس تعهدی موجود نیست - این سیگنال صرف‌نظر شد.", src, dst)
        return

    notional_usdt = RISK_MARGIN_USDT * float(LEVERAGE)
    amount = notional_usdt / sig.entry
    # دقت مقدار: نوبیتکس معمولاً حداکثر ۸-۱۰ رقم اعشار می‌پذیرد؛ محافظه‌کارانه ۸ رقم.
    amount_str = f"{amount:.8f}"

    side = "buy" if sig.side == "LONG" else "sell"

    try:
        nx.open_position_market(
            src_currency=src,
            dst_currency=dst,
            side=side,
            amount=amount_str,
            leverage=LEVERAGE,
            client_order_id=f"{key}-{int(time.time())}"[:32],
        )
    except NobitexAPIError as e:
        log.error("خطا در باز کردن پوزیشن %s: %s", key, e)
        notify_admin(f"❌ باز کردن پوزیشن {key} ناموفق بود: {e.code} - {e.message}")
        return

    # چون /margin/orders/add فقط order.id می‌دهد، positionId واقعی را از
    # positions/list پیدا می‌کنیم (ممکن است چند ثانیه طول بکشد تا مچ شود).
    position = None
    for _ in range(6):
        position = nx.find_open_position_by_market(src, dst)
        if position:
            break
        time.sleep(2)

    if not position:
        log.error("پوزیشن %s باز شد ولی positionId پیدا نشد!", key)
        notify_admin(
            f"🚨 پوزیشن {key} روی نوبیتکس باز شد ولی positionId قابل شناسایی نیست - "
            f"دستی بررسی کنید."
        )
        return

    state["open_trades"][key] = {
        "position_id": position["id"],
        "src_currency": src,
        "dst_currency": dst,
        "side": sig.side,
        "entry": sig.entry,
        "initial_amount": amount,
        "closed_pct": 0.0,
    }
    save_state(state)
    log.info("پوزیشن %s باز شد - positionId=%s amount=%s", key, position["id"], amount_str)


def _close_partial(nx: NobitexClient, state: Dict[str, Any], key: str, pct_of_initial: float) -> None:
    trade = state["open_trades"].get(key)
    if not trade:
        log.warning("رویداد برای %s رسید ولی معامله‌ی بازی در state نیست.", key)
        notify_admin(f"⚠️ رویداد بستن برای {key} رسید ولی هیچ معامله‌ی بازی برایش ثبت نشده بود.")
        return

    close_amount = trade["initial_amount"] * pct_of_initial
    amount_str = f"{close_amount:.8f}"

    try:
        nx.close_position_market(trade["position_id"], amount=amount_str)
    except NobitexAPIError as e:
        log.error("خطا در بستن بخشی از پوزیشن %s: %s", key, e)
        notify_admin(f"❌ بستن بخشی از {key} ناموفق بود: {e.code} - {e.message}")
        return

    trade["closed_pct"] += pct_of_initial
    save_state(state)
    log.info("%.0f%% از %s بسته شد (جمع بسته‌شده تا الان: %.0f%%)", pct_of_initial * 100, key, trade["closed_pct"] * 100)


def _close_all_remaining(nx: NobitexClient, state: Dict[str, Any], key: str, reason: str) -> None:
    trade = state["open_trades"].get(key)
    if not trade:
        log.warning("رویداد '%s' برای %s رسید ولی معامله‌ی بازی در state نیست.", reason, key)
        notify_admin(f"⚠️ رویداد '{reason}' برای {key} رسید ولی هیچ معامله‌ی بازی برایش ثبت نشده بود.")
        return

    remaining_pct = max(0.0, 1.0 - trade["closed_pct"])
    if remaining_pct <= 0:
        state["open_trades"].pop(key, None)
        save_state(state)
        return

    close_amount = trade["initial_amount"] * remaining_pct
    amount_str = f"{close_amount:.8f}"

    try:
        nx.close_position_market(trade["position_id"], amount=amount_str)
    except NobitexAPIError as e:
        log.error("خطا در بستن کامل پوزیشن %s (%s): %s", key, reason, e)
        notify_admin(f"❌ بستن نهایی {key} ({reason}) ناموفق بود: {e.code} - {e.message}")
        return

    state["open_trades"].pop(key, None)
    save_state(state)
    log.info("پوزیشن %s به‌طور کامل بسته شد (دلیل: %s)", key, reason)


def handle_event(nx: NobitexClient, state: Dict[str, Any], ev: ParsedEvent) -> None:
    key = trade_key(ev.symbol, ev.timeframe)

    if ev.kind == "target_hit":
        pct = TARGET_CLOSE_PCT.get(ev.level)
        if pct is None:
            notify_admin(f"⚠️ سطح تارگت ناشناخته ({ev.level}) برای {key}")
            return
        _close_partial(nx, state, key, pct)

    elif ev.kind == "stop":
        _close_all_remaining(nx, state, key, "stop")

    elif ev.kind == "breakeven":
        _close_all_remaining(nx, state, key, "breakeven")

    elif ev.kind == "sl_after_t2":
        _close_all_remaining(nx, state, key, "sl_after_t2")

    elif ev.kind == "sl_after_t3":
        _close_all_remaining(nx, state, key, "sl_after_t3")

    elif ev.kind == "runner_closed":
        _close_all_remaining(nx, state, key, "runner_closed")

    elif ev.kind == "forced_close":
        _close_all_remaining(nx, state, key, "forced_close")


# ---------------------------------------------------------------------- #
# آشتی‌دهی هنگام روشن شدن (بعد از قطعی برق/اینترنت یا کرش)
# ---------------------------------------------------------------------- #
STALE_GAP_ALERT_SECONDS = int(os.environ.get("STALE_GAP_ALERT_SECONDS", str(20 * 3600)))
# تلگرام آپدیت‌های خوانده‌نشده را حدوداً تا ۲۴ ساعت نگه می‌دارد. اگر خاموشی
# بیشتر از این طول بکشد، پیام‌های آن بازه برای همیشه از دست می‌روند - این
# محدودیت خودِ تلگرام است، نه چیزی که این کد بتواند دورش بزند.


def reconcile_on_startup(nx: NobitexClient, state: Dict[str, Any]) -> None:
    """
    هر بار قبل از شروع حلقه صدا زده می‌شود (چه اجرای اول باشد، چه بعد از
    ریستارت کامپیوتر/کرش). وضعیت state.json را با وضعیت واقعی نوبیتکس
    مقایسه می‌کند تا پوزیشن‌هایی که در زمان خاموشی توسط خود نوبیتکس بسته/
    منقضی/لیکویید شده‌اند (و دیگر در state ما فعال نیستند) شناسایی شوند.
    """
    last_seen = state.get("last_successful_poll_ts")
    gap = time.time() - last_seen if last_seen else None

    if gap is not None:
        log.info("مدت خاموشی/قطعی از آخرین اجرای موفق: %.0f دقیقه", gap / 60)
        if gap > STALE_GAP_ALERT_SECONDS:
            notify_admin(
                f"🚨 این اجرا بعد از حدود {gap/3600:.1f} ساعت خاموشی شروع شد. "
                f"چون تلگرام پیام‌های خوانده‌نشده را حدود ۲۴ ساعت نگه می‌دارد، "
                f"ممکن است برخی پیام‌های کانال در این بازه برای همیشه از دست "
                f"رفته باشند. لطفاً پوزیشن‌های باز را دستی هم در نوبیتکس و هم "
                f"در کانال چک کنید."
            )

    try:
        active_positions = nx.list_positions(status="active").get("positions", [])
    except NobitexAPIError as e:
        log.error("آشتی‌دهی اولیه ناموفق بود (نمی‌توان لیست پوزیشن‌ها را گرفت): %s", e)
        notify_admin(f"🚨 در شروع اجرا، دریافت لیست پوزیشن‌های نوبیتکس ناموفق بود: {e.code} - {e.message}")
        return

    active_ids = {p["id"] for p in active_positions}

    for key, trade in list(state["open_trades"].items()):
        if trade["position_id"] not in active_ids:
            log.warning(
                "%s در state ما باز است ولی در نوبیتکس دیگر فعال نیست "
                "(احتمالاً در زمان خاموشی بسته/منقضی/لیکویید شده).",
                key,
            )
            notify_admin(
                f"⚠️ پوزیشن {key} (positionId={trade['position_id']}) در نوبیتکس "
                f"دیگر فعال نیست ولی در state ما هنوز باز بود. به‌احتمال زیاد در "
                f"زمان خاموشی کامپیوتر توسط خود نوبیتکس بسته شده. از state حذف شد "
                f"- لطفاً سود/زیان واقعی را دستی در نوبیتکس چک کنید."
            )
            state["open_trades"].pop(key, None)

    save_state(state)
    log.info("آشتی‌دهی اولیه تمام شد - %d پوزیشن باز در state باقی ماند.", len(state["open_trades"]))


# ---------------------------------------------------------------------- #
# حلقه‌ی تلگرام
# ---------------------------------------------------------------------- #
def poll_once(nx: NobitexClient, state: Dict[str, Any]) -> None:
    resp = requests.get(
        f"{TELEGRAM_API}/getUpdates",
        params={
            "offset": state.get("telegram_update_offset", 0),
            "timeout": 0,
            "allowed_updates": '["channel_post"]',
        },
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        log.error("خطای Telegram getUpdates: %s", data)
        return

    for update in data.get("result", []):
        state["telegram_update_offset"] = update["update_id"] + 1

        post = update.get("channel_post")
        if not post:
            continue
        chat_id = str(post.get("chat", {}).get("id"))
        if chat_id != str(TELEGRAM_CHANNEL_ID):
            continue

        text = post.get("text") or post.get("caption")
        if not text:
            continue

        parsed = parse_message(text)
        if parsed is None:
            snippet = text[:200].replace("\n", " ")
            log.warning("پیام پارس نشد: %s", snippet)
            state["unparsed_messages"].append({"ts": time.time(), "text": snippet})
            notify_admin(f"⚠️ یک پیام کانال پارس نشد و هیچ اقدامی روی نوبیتکس انجام نشد:\n{snippet}")
            continue

        try:
            if isinstance(parsed, ParsedSignal):
                handle_signal(nx, state, parsed)
            elif isinstance(parsed, ParsedEvent):
                handle_event(nx, state, parsed)
        except Exception as e:  # noqa: BLE001
            log.exception("خطای غیرمنتظره هنگام پردازش پیام")
            notify_admin(f"🚨 خطای غیرمنتظره در پردازش پیام: {e}")

    state["last_successful_poll_ts"] = time.time()
    save_state(state)


def main() -> None:
    nx_config = NobitexConfig(
        public_key=NOBITEX_PUBLIC_KEY,
        private_key_b64=NOBITEX_PRIVATE_KEY,
        base_url=NOBITEX_BASE_URL,
    )
    nx = NobitexClient(nx_config)
    state = load_state()

    log.info(
        "شروع اجرا - base_url=%s leverage=%s risk_margin=%s USDT",
        NOBITEX_BASE_URL, LEVERAGE, RISK_MARGIN_USDT,
    )

    reconcile_on_startup(nx, state)

    # این حلقه دیگر سقف زمانی ندارد چون روی کامپیوتر شخصی (نه GitHub
    # Actions) اجرا می‌شود و قرار است پیوسته باز بماند. بیرون از این حلقه،
    # فایل run_watchdog.bat اگر خودِ پردازش پایتون کرش کند دوباره اجرایش
    # می‌کند، و Task Scheduler ویندوز اگر خودِ کامپیوتر ری‌استارت شد، آن
    # bat را دوباره اجرا می‌کند.
    consecutive_errors = 0
    while True:
        try:
            poll_once(nx, state)
            consecutive_errors = 0
        except Exception as e:  # noqa: BLE001
            consecutive_errors += 1
            log.exception("خطا در حلقه‌ی اصلی (خطای متوالی شماره %d)", consecutive_errors)
            if consecutive_errors in (1, 5) or consecutive_errors % 20 == 0:
                # برای جلوگیری از سیل پیام هشدار، فقط گاهی به ادمین اطلاع بده
                notify_admin(f"🚨 خطای حلقه‌ی اصلی (تکرار {consecutive_errors} بار): {e}")
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    sys.exit(main() or 0)
