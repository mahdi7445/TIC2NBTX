# -*- coding: utf-8 -*-
"""Production executor: GitHub -> Nobitex Margin.

Telegram is deliberately not used here. The GitHub bridge receives channel
messages and writes signals.jsonl; this process consumes that file and writes
admin reports to outbox.jsonl.

Trading rules locked for this deployment:
- isolated Margin on Nobitex
- leverage: 5x
- maximum planned loss at the original stop: 1 USDT (before fees/slippage)
- position plan: 20% / 30% / 15% / 10% at T1/T2/T3/T4; final 25% runner
- RR levels: 1R / 2R / 4R / 6R
- runner trailing stop: 1.5R behind the favorable peak
"""
from __future__ import annotations

import json
import logging
import os
import socket
import sys
import time
from decimal import Decimal, ROUND_DOWN, ROUND_UP, InvalidOperation
from typing import Any, Dict, Optional

from github_client import GithubClient
from nobitex_client import NobitexClient, NobitexConfig, NobitexAPIError
from signal_parser import parse_message, ParsedSignal, ParsedEvent
from state_store import load_state, save_state

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler("executor.log", encoding="utf-8")],
)
log = logging.getLogger("executor")

GITHUB_REPO = os.environ["GITHUB_SIGNALS_REPO"]
GITHUB_PAT = os.environ["GITHUB_PAT"]
GITHUB_BRANCH = os.environ.get("GITHUB_BRANCH", "main")
TELEGRAM_CHANNEL_ID = os.environ.get("TELEGRAM_CHANNEL_ID", "")

NOBITEX_PUBLIC_KEY = os.environ["NOBITEX_PUBLIC_KEY"]
NOBITEX_PRIVATE_KEY = os.environ["NOBITEX_PRIVATE_KEY"]
NOBITEX_BASE_URL = os.environ.get("NOBITEX_BASE_URL", "https://apiv2.nobitex.ir")
NOBITEX_PUBLIC_BASE_URL = os.environ.get("NOBITEX_PUBLIC_BASE_URL", "https://api.nobitex.ir")

# Default trading parameters; live controls are persisted in GitHub control.json and can be changed from the admin bot.
DEFAULT_RISK_USDT = Decimal("1")
LEVERAGE = Decimal(os.environ.get("LEVERAGE", "5"))
DEFAULT_MAX_COLLATERAL_USDT = Decimal("1.25")
DEFAULT_MAX_OPEN_TRADES = 15
RISK_USDT = DEFAULT_RISK_USDT
W1, W2, W3, W4, WRUNNER = map(Decimal, ("0.20", "0.30", "0.15", "0.10", "0.25"))
TRAILING_R_MULT = Decimal("1.5")

POLL_INTERVAL_SECONDS = max(2, int(os.environ.get("POLL_INTERVAL_SECONDS", "5")))
TRAILING_CHECK_SECONDS = max(5, int(os.environ.get("TRAILING_CHECK_SECONDS", "10")))
STALE_GAP_ALERT_SECONDS = int(os.environ.get("STALE_GAP_ALERT_SECONDS", str(20 * 3600)))
STOP_LIMIT_BUFFER_PCT = Decimal(os.environ.get("STOP_LIMIT_BUFFER_PCT", "0.005"))
PROTECTION_VERIFY_RETRIES = max(2, int(os.environ.get("PROTECTION_VERIFY_RETRIES", "5")))
PROTECTION_VERIFY_DELAY_SECONDS = max(0.2, float(os.environ.get("PROTECTION_VERIFY_DELAY_SECONDS", "0.7")))
EMERGENCY_CLOSE_RETRIES = max(2, int(os.environ.get("EMERGENCY_CLOSE_RETRIES", "5")))
MIN_BALANCE_BUFFER_USDT = Decimal(os.environ.get("MIN_BALANCE_BUFFER_USDT", "0.05"))

_SINGLE_INSTANCE_PORT = int(os.environ.get("SINGLE_INSTANCE_PORT", "47632"))
_singleton_socket: Optional[socket.socket] = None
_gh_outbox: Optional[GithubClient] = None
_CONTROL_PATH = "control.json"
_COMMANDS_PATH = "commands.jsonl"


def acquire_single_instance_lock() -> None:
    global _singleton_socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", _SINGLE_INSTANCE_PORT))
        s.listen(1)
    except OSError:
        log.error("نسخه دیگری از executor در حال اجراست؛ این نسخه بسته می‌شود.")
        sys.exit(1)
    _singleton_socket = s


def notify_admin(message: str) -> None:
    log.warning("ADMIN: %s", message)
    if _gh_outbox is None:
        return
    try:
        line = json.dumps({"ts": time.time(), "text": message}, ensure_ascii=False)
        _gh_outbox.append_line("outbox.jsonl", line)
    except Exception as e:
        log.error("نوشتن outbox ناموفق بود: %s", e)


def symbol_to_currencies(raw_symbol: str) -> tuple[str, str]:
    s = raw_symbol.upper().replace("/", "")
    for suffix in ("USDT", "USD", "IRT"):
        if s.endswith(suffix) and len(s) > len(suffix):
            return s[:-len(suffix)].lower(), suffix.lower()
    return s.lower(), "usdt"


def trade_key(symbol: str, timeframe: str) -> str:
    return f"{symbol.upper()}_{timeframe.upper()}"


def D(v: Any) -> Decimal:
    return Decimal(str(v))


def fmt_amount(v: Decimal) -> str:
    # Nobitex position liability can require up to 10 decimal places.
    return format(v.quantize(Decimal("0.0000000001"), rounding=ROUND_DOWN), "f")


def fmt_price(v: Decimal) -> str:
    # Preserve enough precision for small altcoins without inventing precision.
    q = Decimal("0.00000001") if abs(v) < 1 else Decimal("0.01")
    return format(v.quantize(q, rounding=ROUND_DOWN), "f")


def order_id_from(resp: dict) -> Optional[int]:
    order = resp.get("order") or {}
    value = order.get("id") or resp.get("id")
    return int(value) if value is not None else None


def stop_limit_price(side: str, stop: Decimal) -> Decimal:
    if side == "LONG":
        return stop * (Decimal("1") - STOP_LIMIT_BUFFER_PCT)
    return stop * (Decimal("1") + STOP_LIMIT_BUFFER_PCT)


def close_order_ids(oco: dict) -> tuple[Optional[int], Optional[int]]:
    orders = oco.get("orders") or []
    if len(orders) < 2:
        raise ValueError(f"Nobitex OCO response did not contain two orders: {oco}")

    tp_id: Optional[int] = None
    sl_id: Optional[int] = None
    for order in orders:
        oid = order.get("id")
        execution = str(order.get("execution", "")).replace("_", "").replace("-", "").lower()
        if oid is None:
            continue
        if execution == "limit":
            tp_id = int(oid)
        elif execution in ("stoplimit", "stopmarket"):
            sl_id = int(oid)

    if tp_id is None or sl_id is None:
        raise ValueError(f"Invalid Nobitex OCO response; TP/SL ids missing: {oco}")

    return tp_id, sl_id


def calculate_position_size(sig: ParsedSignal, collateral_cap: Decimal, risk_cap: Decimal) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    entry = D(sig.entry); stop = D(sig.stop)
    distance = abs(entry - stop)
    if distance <= 0: raise ValueError("Entry و Stop نمی‌توانند برابر باشند")
    stop_pct = distance / entry
    # Keep collateral small enough to allow many concurrent positions, while
    # never allowing planned stop loss to exceed the configured risk ceiling.
    effective_risk = min(risk_cap, collateral_cap * stop_pct * LEVERAGE)
    amount = effective_risk / distance
    notional = amount * entry
    collateral = notional / LEVERAGE
    return amount, notional, collateral, effective_risk

def load_control(gh: GithubClient) -> dict:
    default={"enabled":True,"risk_usdt":str(DEFAULT_RISK_USDT),"max_collateral_usdt":str(DEFAULT_MAX_COLLATERAL_USDT),"max_open_trades":DEFAULT_MAX_OPEN_TRADES}
    try: c=gh.get_json(_CONTROL_PATH, default)
    except Exception as e:
        notify_admin(f"⚠️ control.json خوانده نشد؛ تنظیمات امن پیش‌فرض استفاده شد: {e}"); return default
    c={**default, **c}; return c

def save_control(gh: GithubClient, control: dict) -> None:
    gh.put_file(_CONTROL_PATH, json.dumps(control,ensure_ascii=False,indent=2)+"\n", "chore: update trading controls")

def control_value(gh: GithubClient, key: str, fallback):
    return load_control(gh).get(key, fallback)


def _get_position(nx: NobitexClient, src: str, dst: str, side: str) -> Optional[dict]:
    for _ in range(8):
        p = nx.find_open_position_by_market(src, dst, side)
        if p:
            return p
        time.sleep(1.5)
    return None


def _get_new_position_after_open(nx: NobitexClient, src: str, dst: str, side: str, before_ids: set[int]) -> Optional[dict]:
    """Find the position created by this signal, not an older same-market position."""
    wanted_side = side.lower()
    for _ in range(10):
        try:
            positions = nx.list_positions(status="active").get("positions", [])
        except NobitexAPIError:
            positions = []
        candidates = []
        for p in positions:
            try:
                pid = int(p.get("id"))
            except (TypeError, ValueError):
                continue
            if pid in before_ids:
                continue
            if str(p.get("srcCurrency", "")).lower() != src.lower():
                continue
            if str(p.get("dstCurrency", "")).lower() != dst.lower():
                continue
            if str(p.get("side", "")).lower() != wanted_side:
                continue
            candidates.append(p)
        if candidates:
            return candidates[-1]
        time.sleep(0.8)
    return None


def _position_status(nx: NobitexClient, trade: dict) -> Optional[dict]:
    try:
        return nx.get_position_status(int(trade["position_id"])).get("position")
    except NobitexAPIError as e:
        log.warning("position status failed %s: %s", trade.get("position_id"), e)
        return None


def _cancel_order_quiet(nx: NobitexClient, order_id: Optional[int], label: str) -> bool:
    if not order_id:
        return True
    try:
        nx.cancel_order(int(order_id))
        return True
    except NobitexAPIError as e:
        # Done/Canceled is expected during reconciliation.
        log.info("cancel %s (%s) returned %s: %s", label, order_id, e.code, e.message)
        return False


def _cancel_oco(nx: NobitexClient, target: dict) -> None:
    _cancel_order_quiet(nx, target.get("tp_order_id"), f"T{target.get('target')}-tp")
    _cancel_order_quiet(nx, target.get("sl_order_id"), f"T{target.get('target')}-sl")


def _create_target_oco(nx: NobitexClient, trade: dict, target_num: int,
                       amount: Decimal, tp_price: Decimal, stop_price: Decimal) -> dict:
    if amount <= 0:
        return {"target": target_num, "amount": "0", "tp_order_id": None, "sl_order_id": None}
    resp = nx.place_position_close_oco(
        position_id=int(trade["position_id"]),
        amount=fmt_amount(amount),
        price=fmt_price(tp_price),
        stop_price=fmt_price(stop_price),
        stop_limit_price=fmt_price(stop_limit_price(trade["side"], stop_price))
    )
    tp_id, sl_id = close_order_ids(resp)
    return {
        "target": target_num,
        "amount": str(amount),
        "tp_price": str(tp_price),
        "stop_price": str(stop_price),
        "tp_order_id": tp_id,
        "sl_order_id": sl_id,
        "status": "open",
    }


def _create_runner_stop(nx: NobitexClient, trade: dict, amount: Decimal, stop_price: Decimal) -> dict:
    resp = nx.place_position_close_stop_market(
        position_id=int(trade["position_id"]),
        amount=fmt_amount(amount),
        stop_price=fmt_price(stop_price)
    )
    return {
        "order_id": order_id_from(resp),
        "amount": str(amount),
        "stop_price": str(stop_price),
        "status": "open",
    }


def _order_is_live_or_done(status: str) -> bool:
    return str(status or "").lower() in {"new", "active", "inactive", "done", "partial", "partiallyfilled"}


def _verify_order_exists(nx: NobitexClient, order_id: Optional[int], label: str) -> dict:
    if not order_id:
        raise RuntimeError(f"{label}: missing order id")
    last_error = None
    for attempt in range(PROTECTION_VERIFY_RETRIES):
        try:
            resp = nx.get_order_status(int(order_id))
            order = resp.get("order") or {}
            status = str(order.get("status", ""))
            if _order_is_live_or_done(status):
                return order
            last_error = RuntimeError(f"{label}: unexpected status={status}")
        except Exception as e:
            last_error = e
        time.sleep(PROTECTION_VERIFY_DELAY_SECONDS)
    raise RuntimeError(f"{label}: verification failed: {last_error}")


def _verify_trade_protection(nx: NobitexClient, trade: dict) -> None:
    # Verify the live position first. If it disappeared, there is nothing to protect.
    status = _position_status(nx, trade)
    if not status or str(status.get("status", "")).lower() != "open":
        raise RuntimeError("Position is not open while verifying protection")

    expected = 4
    targets = trade.get("targets", {})
    if len(targets) != expected:
        raise RuntimeError(f"Expected {expected} targets, got {len(targets)}")

    for n in range(1, 5):
        t = targets.get(str(n)) or {}
        if not t.get("tp_order_id") or not t.get("sl_order_id"):
            raise RuntimeError(f"T{n}: TP/SL order id missing")
        tp = _verify_order_exists(nx, t["tp_order_id"], f"T{n} TP")
        sl = _verify_order_exists(nx, t["sl_order_id"], f"T{n} SL")
        tp_exec = str(tp.get("execution", "")).lower()
        sl_exec = str(sl.get("execution", "")).lower()
        if "limit" not in tp_exec or "stop" not in sl_exec:
            raise RuntimeError(f"T{n}: invalid protection executions TP={tp_exec}, SL={sl_exec}")

    runner = trade.get("runner") or {}
    if not runner.get("order_id"):
        raise RuntimeError("Runner stop order id missing")
    _verify_order_exists(nx, runner["order_id"], "Runner SL")


def _emergency_flatten(nx: NobitexClient, state: Dict[str, Any], key: str, trade: dict, reason: str) -> None:
    """Fail-safe: never leave a newly opened position intentionally unprotected."""
    log.error("EMERGENCY FLATTEN %s: %s", key, reason)
    notify_admin(f"🚨 FAIL-SAFE {key}: protection incomplete; attempting emergency market close. {reason}")

    for t in trade.get("targets", {}).values():
        _cancel_oco(nx, t)
    _cancel_order_quiet(nx, (trade.get("runner") or {}).get("order_id"), "runner-emergency")

    for attempt in range(EMERGENCY_CLOSE_RETRIES):
        try:
            status = _position_status(nx, trade)
            if not status or str(status.get("status", "")).lower() != "open":
                state.get("open_trades", {}).pop(key, None)
                save_state(state)
                notify_admin(f"🛡️ {key}: emergency close confirmed; no unprotected position remains.")
                return
            liability = D(status.get("liability", "0"))
            if liability <= 0:
                state.get("open_trades", {}).pop(key, None)
                save_state(state)
                return
            nx.close_position_market(int(trade["position_id"]), amount=fmt_amount(liability))
            time.sleep(0.8)
        except Exception as e:
            log.exception("Emergency close attempt %s failed for %s", attempt + 1, key)
            time.sleep(0.8)

    # Keep state so startup reconciliation can continue trying; never falsely report success.
    save_state(state)
    notify_admin(f"🚨 CRITICAL {key}: emergency close could not be confirmed. MANUAL NOBITEX CHECK REQUIRED.")
    raise RuntimeError(f"Emergency close could not be confirmed for {key}")


def _rebuild_protection(nx: NobitexClient, trade: dict, stop_price: Decimal) -> None:
    """Rebuild all not-yet-hit target OCOs and runner stop at one new stop.

    This is necessary because Nobitex does not expose a modify-order endpoint
    for these position-close orders. The cancel/recreate operation is made
    idempotent through state and clientOrderId values.
    """
    for t in trade.get("targets", {}).values():
        if not t.get("hit"):
            _cancel_oco(nx, t)

    runner = trade.get("runner") or {}
    if runner.get("order_id"):
        _cancel_order_quiet(nx, runner.get("order_id"), "runner-stop")

    status = _position_status(nx, trade)
    if not status or str(status.get("status", "")).lower() != "open":
        return
    liability = D(status.get("liability", "0"))
    if liability <= 0:
        return

    # Allocate from the live liability. The final remainder is assigned to the
    # runner so rounding never leaves a protected amount unaccounted for.
    hit_pct = sum(D(t["pct"]) for t in trade.get("targets", {}).values() if t.get("hit"))
    remaining_original = max(Decimal("0"), Decimal("1") - hit_pct)
    if remaining_original <= 0:
        return

    new_targets = {}
    for n, t in sorted(trade["targets"].items(), key=lambda kv: int(kv[0])):
        if t.get("hit"):
            new_targets[n] = t
            continue
        pct = D(t["pct"]) / remaining_original
        amount = (liability * pct).quantize(Decimal("0.0000000001"), rounding=ROUND_DOWN)
        new_targets[n] = _create_target_oco(nx, trade, int(n), amount, D(t["tp_price"]), stop_price)
        new_targets[n]["pct"] = t["pct"]
        new_targets[n]["hit"] = False

    # Runner is always the final 25% of original position after T4. Before T4
    # it is protected by the same initial/new stop as the other future chunks.
    runner_original_pct = WRUNNER
    runner_amount = (liability * (runner_original_pct / remaining_original)).quantize(
        Decimal("0.0000000001"), rounding=ROUND_DOWN
    )
    if runner_amount > 0:
        trade["runner"] = _create_runner_stop(nx, trade, runner_amount, stop_price)
    else:
        trade["runner"] = {"order_id": None, "amount": "0", "stop_price": str(stop_price), "status": "none"}
    trade["targets"] = new_targets
    trade["stop_price"] = str(stop_price)


def handle_signal(nx: NobitexClient, state: Dict[str, Any], sig: ParsedSignal, gh: GithubClient) -> None:
    key = trade_key(sig.symbol, sig.timeframe)
    existing_trade = state["open_trades"].get(key)
    if existing_trade:
        # If the process died after opening the position but before all OCOs were
        # installed, resume protection instead of either abandoning the position
        # or opening a duplicate. A fully protected trade is left untouched.
        fully_protected = (len(existing_trade.get("targets", {})) == 4
                           and bool(existing_trade.get("runner", {}).get("order_id")))
        if not fully_protected:
            try:
                for n in range(1, 5):
                    existing_trade.setdefault("targets", {}).setdefault(str(n), {
                        "target": n, "pct": str({1: W1, 2: W2, 3: W3, 4: W4}[n]),
                        "tp_price": str(D(sig.targets[n])), "hit": False,
                    })
                _rebuild_protection(nx, existing_trade, D(existing_trade.get("stop_price", sig.stop)))
                save_state(state)
                _verify_trade_protection(nx, existing_trade)
                notify_admin(f"♻️ Protection resumed and verified for existing {key}; no duplicate entry opened.")
            except Exception as e:
                notify_admin(f"🚨 {key}: protection resume failed: {e}")
                try:
                    _emergency_flatten(nx, state, key, existing_trade, f"protection resume failed: {e}")
                except Exception:
                    pass
                raise
        else:
            try:
                _verify_trade_protection(nx, existing_trade)
                notify_admin(f"⚠️ سیگنال تکراری/همپوشان برای {key} دریافت شد؛ معامله جدید باز نشد و protection تأیید شد.")
            except Exception as e:
                notify_admin(f"🚨 {key}: existing protection verification failed: {e}")
                _emergency_flatten(nx, state, key, existing_trade, f"duplicate-signal protection verification failed: {e}")
                raise
        return

    control = load_control(gh)
    if not bool(control.get("enabled", True)):
        notify_admin(f"⏸ {key}: ورودهای جدید غیرفعال است؛ سیگنال اجرا نشد."); return
    if len(state.get("open_trades", {})) >= int(control.get("max_open_trades", DEFAULT_MAX_OPEN_TRADES)):
        notify_admin(f"⛔ {key}: سقف معاملات همزمان پر است ({control.get('max_open_trades')})."); return
    src, dst = symbol_to_currencies(sig.symbol)
    side = sig.side.upper()
    if dst != "usdt":
        notify_admin(f"⚠️ {key}: فقط بازارهای USDT برای این Executor فعال هستند؛ {src}/{dst} رد شد.")
        return

    try:
        if not nx.is_symbol_available(src, dst, "buy" if side == "LONG" else "sell"):
            notify_admin(f"❌ {key}: بازار تعهدی/جهت موردنظر در نوبیتکس فعال نیست.")
            return

        max_lev = nx.market_max_leverage(src, dst)
        if max_lev is not None and max_lev < LEVERAGE:
            notify_admin(f"❌ {key}: اهرم 5x برای این بازار مجاز نیست؛ سقف فعلی {max_lev}x است. معامله لغو شد.")
            return

        risk_cap=D(control.get("risk_usdt", DEFAULT_RISK_USDT)); collateral_cap=D(control.get("max_collateral_usdt", DEFAULT_MAX_COLLATERAL_USDT))
        margin_balance=nx.get_margin_usdt_balance()
        free_slots=max(1, int(control.get("max_open_trades", DEFAULT_MAX_OPEN_TRADES))-len(state.get("open_trades", {})))
        per_trade_budget=min(collateral_cap, margin_balance / Decimal(free_slots))
        if per_trade_budget <= 0:
            notify_admin(f"❌ {key}: موجودی آزاد Margin USDT کافی نیست. Available={margin_balance} USDT"); return
        amount, notional, collateral, effective_risk = calculate_position_size(sig, per_trade_budget, risk_cap)
        if amount <= 0:
            raise ValueError("حجم محاسبه‌شده صفر/منفی است")

        signal_uid = str(getattr(sig, "source_update_id", "") or f"{key}-{sig.entry}-{sig.stop}")
        safe_uid = "".join(ch if ch.isalnum() else "-" for ch in signal_uid)[-18:]
        prefix = f"tc-{key[:8]}-{safe_uid}".replace("/", "-")[:27]
        open_side = "buy" if side == "LONG" else "sell"

        notify_admin(
            f"📥 سیگنال دریافت شد\n{key} · {side}\n"
            f"Entry={sig.entry} · SL={sig.stop}\n"
            f"Risk cap=${risk_cap} · Effective risk≈${effective_risk:.4f} · Leverage={LEVERAGE}x\n"
            f"Calculated amount={fmt_amount(amount)} {src.upper()}\n"
            f"Notional≈${notional:.4f} · Collateral≈${collateral:.4f} · Margin available={margin_balance:.4f}"
        )

        # Crash recovery before the state file was written. We snapshot active
        # position IDs first, then identify the newly created position by ID.
        # This prevents accidentally adopting an older same-market position.
        active_before = nx.list_positions(status="active").get("positions", [])
        before_ids = {int(p["id"]) for p in active_before if p.get("id") is not None}
        position = None
        for candidate in active_before:
            try:
                existing_entry = D(candidate.get("entryPrice"))
                candidate_side = str(candidate.get("side", "")).lower()
                candidate_src = str(candidate.get("srcCurrency", "")).lower()
                candidate_dst = str(candidate.get("dstCurrency", "")).lower()
                if (candidate_src == src.lower() and candidate_dst == dst.lower() and
                        candidate_side == side.lower() and
                        abs(existing_entry - D(sig.entry)) / D(sig.entry) <= Decimal("0.005")):
                    position = candidate
                    break
            except Exception:
                continue

        if position is None:
            nx.open_position_market(
                src_currency=src, dst_currency=dst, side=open_side,
                amount=fmt_amount(amount), leverage=str(LEVERAGE),
            )
            position = _get_new_position_after_open(nx, src, dst, side, before_ids)
        else:
            notify_admin(f"♻️ {key}: matching open position {position.get('id')} adopted; no duplicate entry opened.")
        if not position:
            notify_admin(f"🚨 {key}: سفارش ورود پذیرفته شد اما positionId پیدا نشد؛ فوراً حساب را بررسی کنید.")
            return

        actual_entry = D(position.get("entryPrice", sig.entry))
        live_liability = D(position.get("liability", amount))
        if live_liability <= 0:
            raise RuntimeError("Nobitex returned an open position with zero liability")
        actual_notional = live_liability * actual_entry
        actual_collateral = actual_notional / LEVERAGE
        actual_risk = abs(actual_entry - D(sig.stop)) * live_liability
        if actual_collateral > collateral_cap + MIN_BALANCE_BUFFER_USDT:
            _emergency_flatten(nx, state, key, {"position_id": int(position["id"])}, f"actual collateral {actual_collateral} exceeds cap {collateral_cap}")
            raise RuntimeError("Actual collateral exceeded configured cap")
        if actual_risk > risk_cap + MIN_BALANCE_BUFFER_USDT:
            _emergency_flatten(nx, state, key, {"position_id": int(position["id"])}, f"actual stop risk {actual_risk} exceeds cap {risk_cap}")
            raise RuntimeError("Actual stop risk exceeded configured cap")
        trade = {
            "symbol": sig.symbol,
            "timeframe": sig.timeframe,
            "side": side,
            "position_id": int(position["id"]),
            "entry_signal": str(sig.entry),
            "entry_actual": str(actual_entry),
            "original_stop": str(sig.stop),
            "risk_usdt": str(effective_risk),
            "risk_cap_usdt": str(risk_cap),
            "collateral": str(collateral),
            "leverage": str(LEVERAGE),
            "initial_amount": str(live_liability),
            "initial_amount_requested": str(amount),
            "risk_unit": str(abs(D(sig.entry) - D(sig.stop))),
            "targets": {},
            "runner": {},
            "client_prefix": prefix,
            "closed_pct": "0",
            "last_trailing_check": 0,
            "peak": str(actual_entry),
            "trailing_active": False,
            "stop_price": str(sig.stop),
        }
        # Persist the position immediately. If the process dies while placing
        # protection orders, the next run can reconcile this position instead
        # of opening a second one.
        state["open_trades"][key] = trade
        save_state(state)

        # The four OCO chunks + runner stop together consume exactly the live
        # liability (subject to 10-decimal downward rounding).
        pcts = {1: W1, 2: W2, 3: W3, 4: W4}
        for n, pct in pcts.items():
            trade["targets"][str(n)] = {
                "target": n, "pct": str(pct), "tp_price": str(D(sig.targets[n])),
                "hit": False, "tp_order_id": None, "sl_order_id": None,
            }

        remaining = live_liability
        try:
            for n, pct in pcts.items():
                chunk = (live_liability * pct).quantize(Decimal("0.0000000001"), rounding=ROUND_DOWN)
                remaining -= chunk
                if chunk <= 0:
                    raise RuntimeError(f"T{n}: calculated protection amount is zero")
                created = _create_target_oco(nx, trade, n, chunk, D(sig.targets[n]), D(sig.stop))
                created["pct"] = str(pct)
                created["hit"] = False
                trade["targets"][str(n)] = created
                save_state(state)

            runner_amount = max(Decimal("0"), remaining)
            if runner_amount <= 0:
                raise RuntimeError("Runner protection amount is zero")
            trade["runner"] = _create_runner_stop(nx, trade, runner_amount, D(sig.stop))
            save_state(state)

            # Critical invariant: do not accept/process the signal until every
            # target OCO and the runner stop are confirmed at Nobitex.
            _verify_trade_protection(nx, trade)

        except Exception as protection_error:
            _emergency_flatten(nx, state, key, trade, str(protection_error))
            raise

        lines = [
            f"🟢 معامله باز شد: {key}",
            f"Position ID: {position['id']}",
            f"Direction: {side}",
            f"Signal entry: {sig.entry} · Actual entry: {actual_entry}",
            f"Original SL: {sig.stop}",
            f"Risk cap: ${risk_cap} · Effective risk: ${effective_risk:.4f} · Leverage: {LEVERAGE}x",
            f"Requested amount: {fmt_amount(amount)} {src.upper()}",
            f"Live liability: {fmt_amount(live_liability)} {src.upper()}",
        ]
        for n in range(1, 5):
            t = trade["targets"][str(n)]
            lines.append(f"🎯 T{n}: {t['tp_price']} · {D(t['pct'])*100}% · OCO TP={t.get('tp_order_id')} SL={t.get('sl_order_id')}")
        lines.append(f"🏁 Runner 25%: stop={trade['runner'].get('stop_price')} order={trade['runner'].get('order_id')}")
        notify_admin("\n".join(lines))

    except (NobitexAPIError, ValueError, InvalidOperation) as e:
        log.exception("handle_signal failed for %s", key)
        notify_admin(f"❌ اجرای سیگنال {key} شکست خورد: {e}")
        raise


def _close_target_if_needed(nx: NobitexClient, trade: dict, key: str, target_num: int) -> None:
    t = trade["targets"].get(str(target_num))
    if not t or t.get("hit"):
        return

    order_id = t.get("tp_order_id")
    matched = Decimal("0")
    status = "Unknown"
    if order_id:
        try:
            order = nx.get_order_status(int(order_id)).get("order", {})
            matched = D(order.get("matchedAmount", "0"))
            status = str(order.get("status", "Unknown"))
        except NobitexAPIError as e:
            log.warning("T%d status failed for %s: %s", target_num, key, e)

    planned = D(t.get("amount", "0"))
    if status != "Done" and matched < planned:
        remaining_target = max(Decimal("0"), planned - matched)
        # The channel says target was reached. If the exchange limit did not
        # fill, force-close only the unfilled target slice at market after
        # canceling the stale limit order.
        if order_id:
            _cancel_order_quiet(nx, int(order_id), f"T{target_num}-tp-fallback")
        if remaining_target > 0:
            try:
                nx.close_position_market(
                    int(trade["position_id"]), amount=fmt_amount(remaining_target)
                )
                notify_admin(f"⚠️ {key}: T{target_num} روی قیمت حدی کامل Fill نشد؛ بخش باقیمانده ({fmt_amount(remaining_target)}) با Market بسته شد.")
            except NobitexAPIError as e:
                notify_admin(f"🚨 {key}: T{target_num} hit شد ولی بستن fallback ناموفق بود: {e.code} - {e.message}")
                return

    t["hit"] = True
    trade["closed_pct"] = str(D(trade.get("closed_pct", "0")) + D(t["pct"]))
    notify_admin(
        f"🎯 Target {target_num} HIT — {key}\n"
        f"Banked: {D(t['pct'])*100:.0f}%\n"
        f"TP price: {t['tp_price']}\n"
        f"Protection will move according to the strategy."
    )


def handle_event(nx: NobitexClient, state: Dict[str, Any], ev: ParsedEvent) -> None:
    key = trade_key(ev.symbol, ev.timeframe)
    trade = state["open_trades"].get(key)

    if trade is None:
        notify_admin(f"⚠️ رویداد {ev.kind} برای {key} رسید ولی معامله‌ای در state باز نیست.")
        return

    if ev.kind == "target_hit":
        _close_target_if_needed(nx, trade, key, int(ev.level))
        if int(ev.level) == 1:
            new_stop = D(trade["entry_actual"])
        elif int(ev.level) == 2:
            new_stop = D(trade["targets"]["1"]["tp_price"])
        elif int(ev.level) == 3:
            new_stop = D(trade["targets"]["2"]["tp_price"])
        elif int(ev.level) == 4:
            new_stop = D(trade["targets"]["3"]["tp_price"])
        else:
            new_stop = D(trade["stop_price"])

        if int(ev.level) < 4:
            _rebuild_protection(nx, trade, new_stop)
        else:
            # T4 implies T1/T2/T3/T4 have all been reached even if one or more
            # channel result messages were missed. Mark them accordingly, then
            # leave only the live runner liability on the exchange.
            for n, t in trade["targets"].items():
                t["hit"] = True
            trade["closed_pct"] = str(W1 + W2 + W3 + W4)
            # Cancel every fixed-target OCO; their actual fills, if any, are
            # already reflected by the live position liability below.
            for t in trade["targets"].values():
                _cancel_oco(nx, t)
            trade["trailing_active"] = True
            _cancel_order_quiet(nx, trade.get("runner", {}).get("order_id"), "runner-before-trailing")
            status = _position_status(nx, trade)
            if status and str(status.get("status", "")).lower() == "open":
                liability = D(status.get("liability", "0"))
                market = symbol_to_currencies(trade["symbol"])
                try:
                    last = nx.get_last_trade_price(f"{market[0].upper()}{market[1].upper()}")
                except Exception:
                    last = D(trade["targets"]["4"]["tp_price"])
                r = D(trade["risk_unit"])
                entry = D(trade["entry_actual"])
                if trade["side"] == "LONG":
                    peak = max(entry, last)
                    runner_stop = max(new_stop, peak - TRAILING_R_MULT * r)
                else:
                    peak = min(entry, last)
                    runner_stop = min(new_stop, peak + TRAILING_R_MULT * r)
                trade["peak"] = str(peak)
                trade["runner"] = _create_runner_stop(nx, trade, liability, runner_stop)
                trade["stop_price"] = str(runner_stop)
            else:
                trade["stop_price"] = str(new_stop)

        save_state(state)
        return

    if ev.kind == "breakeven":
        _close_remaining_at_price(nx, state, key, "breakeven", D(trade["entry_actual"]))
        return

    if ev.kind in ("sl_after_t2", "sl_after_t3"):
        # These messages are CLOSE events, not merely instructions to move the
        # stop. The source strategy explicitly says the remaining 50%/35% was
        # closed at T1/T2. The exchange therefore needs the remaining position
        # settled now; otherwise the executor would leave a live position open.
        implied_hits = ["1", "2"] if ev.kind == "sl_after_t2" else ["1", "2", "4"]
        for h in implied_hits:
            if h in trade.get("targets", {}):
                trade["targets"][h]["hit"] = True
        close_price = D(trade["targets"]["1"]["tp_price"] if ev.kind == "sl_after_t2" else trade["targets"]["2"]["tp_price"])
        _close_remaining_at_price(nx, state, key, ev.kind, close_price)
        return

    if ev.kind in ("stop", "runner_closed", "forced_close"):
        _close_remaining_market(nx, state, key, ev.kind)
        return



def _close_remaining_at_price(nx: NobitexClient, state: Dict[str, Any], key: str, reason: str, price: Decimal) -> None:
    trade = state["open_trades"].get(key)
    if not trade:
        return
    for t in trade.get("targets", {}).values():
        _cancel_oco(nx, t)
    _cancel_order_quiet(nx, trade.get("runner", {}).get("order_id"), "runner-price-close")

    status = _position_status(nx, trade)
    if not status or str(status.get("status", "")).lower() != "open":
        state["open_trades"].pop(key, None)
        save_state(state)
        notify_admin(f"🏁 {key} already closed before {reason}.")
        return

    liability = D(status.get("liability", "0"))
    if liability <= 0:
        state["open_trades"].pop(key, None)
        save_state(state)
        return

    try:
        resp = nx.place_position_close_limit(
            position_id=int(trade["position_id"]),
            amount=fmt_amount(liability),
            price=fmt_price(price)
        )
        oid = order_id_from(resp)
        filled = False
        if oid:
            for _ in range(3):
                time.sleep(1)
                try:
                    order = nx.get_order_status(oid).get("order", {})
                    if str(order.get("status", "")).lower() == "done":
                        filled = True
                        break
                except NobitexAPIError:
                    pass
        if not filled:
            _cancel_order_quiet(nx, oid, f"{reason}-limit-fallback")
            status2 = _position_status(nx, trade)
            remaining = D(status2.get("liability", "0")) if status2 else Decimal("0")
            if remaining > 0:
                nx.close_position_market(int(trade["position_id"]), amount=fmt_amount(remaining))
        state["open_trades"].pop(key, None)
        save_state(state)
        notify_admin(f"🏁 {key} closed — {reason} at strategy price {price}")
    except NobitexAPIError as e:
        notify_admin(f"🚨 {key}: {reason} close failed: {e.code} - {e.message}")


def _close_remaining_market(nx: NobitexClient, state: Dict[str, Any], key: str, reason: str) -> None:
    trade = state["open_trades"].get(key)
    if not trade:
        return

    for t in trade.get("targets", {}).values():
        _cancel_oco(nx, t)
    _cancel_order_quiet(nx, trade.get("runner", {}).get("order_id"), "runner-final")

    status = _position_status(nx, trade)
    if not status or str(status.get("status", "")).lower() != "open":
        state["open_trades"].pop(key, None)
        save_state(state)
        notify_admin(f"🏁 {key} already closed before final command ({reason}).")
        return

    liability = D(status.get("liability", "0"))
    if liability > 0:
        try:
            nx.close_position_market(int(trade["position_id"]), amount=fmt_amount(liability))
        except NobitexAPIError as e:
            notify_admin(f"🚨 {key}: final market close failed ({reason}): {e.code} - {e.message}")
            return

    state["open_trades"].pop(key, None)
    save_state(state)
    notify_admin(f"🏁 {key} fully closed — reason={reason}")


def update_runner_trailing(nx: NobitexClient, state: Dict[str, Any]) -> None:
    now = time.time()
    for key, trade in list(state["open_trades"].items()):
        if not trade.get("trailing_active"):
            continue
        if now - float(trade.get("last_trailing_check", 0)) < TRAILING_CHECK_SECONDS:
            continue
        trade["last_trailing_check"] = now
        try:
            market = symbol_to_currencies(trade["symbol"])
            last = nx.get_last_trade_price(f"{market[0].upper()}{market[1].upper()}")
            entry = D(trade["entry_actual"])
            r = D(trade["risk_unit"])
            old_peak = D(trade.get("peak", entry))
            if trade["side"] == "LONG":
                peak = max(old_peak, last)
                candidate = peak - TRAILING_R_MULT * r
                old_stop = D(trade["stop_price"])
                if candidate <= old_stop:
                    continue
            else:
                peak = min(old_peak, last)
                candidate = peak + TRAILING_R_MULT * r
                old_stop = D(trade["stop_price"])
                if candidate >= old_stop:
                    continue

            status = _position_status(nx, trade)
            if not status or str(status.get("status", "")).lower() != "open":
                state["open_trades"].pop(key, None)
                continue
            liability = D(status.get("liability", "0"))
            if liability <= 0:
                state["open_trades"].pop(key, None)
                continue

            _cancel_order_quiet(nx, trade.get("runner", {}).get("order_id"), "runner-trail")
            trade["runner"] = _create_runner_stop(nx, trade, liability, candidate)
            trade["peak"] = str(peak)
            trade["stop_price"] = str(candidate)
            save_state(state)
            notify_admin(
                f"📈 Runner trailing updated\n{key}\n"
                f"Last={last} · Peak={peak} · New stop={candidate} · Remaining liability={liability}"
            )
        except Exception as e:
            log.exception("runner update failed for %s", key)
            notify_admin(f"⚠️ Runner trailing update failed for {key}: {e}")


def audit_open_trade_protection(nx: NobitexClient, state: Dict[str, Any], flatten_on_failure: bool = True) -> str:
    if not state.get("open_trades"):
        return "🛡️ Protection audit: no open trades in executor state."
    lines = [f"🛡️ Protection audit: {len(state['open_trades'])} trade(s)"]
    for key, trade in list(state["open_trades"].items()):
        try:
            _verify_trade_protection(nx, trade)
            lines.append(f"✅ {key}: all TP/SL OCOs + runner stop verified")
        except Exception as e:
            lines.append(f"🚨 {key}: protection FAILED — {e}")
            if flatten_on_failure:
                try:
                    _emergency_flatten(nx, state, key, trade, f"manual protection audit failed: {e}")
                except Exception as close_error:
                    lines.append(f"🚨 {key}: emergency close also failed — {close_error}")
    save_state(state)
    return "\n".join(lines)[:4000]


def reconcile_on_startup(nx: NobitexClient, state: Dict[str, Any]) -> None:
    last_seen = state.get("last_successful_poll_ts")
    gap = time.time() - float(last_seen) if last_seen else None
    if gap and gap > STALE_GAP_ALERT_SECONDS:
        notify_admin(f"🚨 Executor after {gap/3600:.1f}h downtime. Reconciliation started.")

    try:
        active = nx.list_positions(status="active").get("positions", [])
    except NobitexAPIError as e:
        notify_admin(f"🚨 Startup reconciliation failed: {e.code} - {e.message}")
        return

    active_by_id = {int(p["id"]): p for p in active if p.get("id") is not None}
    lines = [f"🔄 Startup reconciliation: state={len(state['open_trades'])}, Nobitex active={len(active)}"]
    for key, trade in list(state["open_trades"].items()):
        pid = int(trade["position_id"])
        if pid not in active_by_id:
            state["open_trades"].pop(key, None)
            lines.append(f"❌ {key}: position {pid} no longer active; removed from state.")
            continue
        p = active_by_id[pid]
        trade["entry_actual"] = str(p.get("entryPrice", trade.get("entry_actual")))
        try:
            _verify_trade_protection(nx, trade)
            lines.append(f"✅ {key}: position {pid} open and protection verified; liability={p.get('liability')}, stop={trade.get('stop_price')}")
        except Exception as protection_error:
            lines.append(f"🚨 {key}: position {pid} is open but protection is NOT fully verified: {protection_error}")
            try:
                _emergency_flatten(nx, state, key, trade, f"startup protection verification failed: {protection_error}")
            except Exception:
                pass

    save_state(state)
    notify_admin("\n".join(lines))


def poll_commands(nx: NobitexClient, gh: GithubClient, state: Dict[str, Any]) -> None:
    """Consume admin commands relayed through GitHub by the Telegram bridge."""
    content, _sha = gh.get_file(_COMMANDS_PATH, allow_missing=True)
    lines = [x for x in content.splitlines() if x.strip()]
    processed = set(state.setdefault("processed_command_ids", []))
    changed = False
    for line in lines:
        try:
            obj = json.loads(line)
            command_id = str(obj.get("command_id") or obj.get("update_id") or "")
            command = str(obj.get("command") or "").strip()
            if not command_id or not command:
                continue
        except Exception:
            continue
        if command_id in processed:
            continue
        try:
            result = handle_bot_command(command)
            notify_admin(f"🛠 فرمان {command_id} اجرا شد\n{result}")
            processed.add(command_id)
        except Exception as e:
            notify_admin(f"🚨 اجرای فرمان {command_id} ناموفق بود: {type(e).__name__}: {e}")
            # Mark it processed after reporting, so a permanently bad command
            # cannot loop forever.
            processed.add(command_id)
        changed = True
    if changed:
        state["processed_command_ids"] = list(processed)[-5000:]
        save_state(state)


def poll_once(nx: NobitexClient, gh: GithubClient, state: Dict[str, Any]) -> None:
    # signals.jsonl is allowed to be absent on a brand-new repository.
    # The bridge creates it as soon as the first channel post arrives.
    content, _sha = gh.get_file("signals.jsonl", allow_missing=True)
    lines = [x for x in content.splitlines() if x.strip()]
    last_update_id = int(state.get("last_signal_update_id", 0))

    for line in lines:
        try:
            obj = json.loads(line)
            update_id = int(obj.get("update_id", 0))
            message_id = int(obj.get("message_id", 0) or 0)
            # Telegram channel message_id is the stable per-channel identifier.
            # update_id is retained as a transport cursor/fallback.
            signal_uid = str(obj.get("signal_id") or (f"{TELEGRAM_CHANNEL_ID}:{message_id}" if message_id else update_id))
            text = obj["text"]
        except Exception:
            notify_admin(f"⚠️ Invalid JSON line in signals.jsonl: {line[:200]}")
            continue

        processed_ids = state.setdefault("processed_signal_ids", [])
        if signal_uid in processed_ids:
            continue
        if update_id and update_id <= last_update_id and not message_id:
            continue

        parsed = parse_message(text)
        if parsed is None:
            state.setdefault("unparsed_messages", []).append({"ts": time.time(), "text": text[:1000], "update_id": update_id})
            state["unparsed_messages"] = state["unparsed_messages"][-200:]
            notify_admin(f"⚠️ Channel message could not be parsed; no exchange action taken:\n{text[:500]}")
            processed_ids.append(signal_uid)
            state["processed_signal_ids"] = processed_ids[-5000:]
            if update_id:
                last_update_id = max(last_update_id, update_id)
            continue

        try:
            if isinstance(parsed, ParsedSignal):
                setattr(parsed, "source_update_id", signal_uid)
                handle_signal(nx, state, parsed, gh)
            elif isinstance(parsed, ParsedEvent):
                handle_event(nx, state, parsed)
            processed_ids.append(signal_uid)
            state["processed_signal_ids"] = processed_ids[-5000:]
            if update_id:
                last_update_id = max(last_update_id, update_id)
        except Exception as e:
            # Do NOT mark a failed signal as processed. The executor will retry it.
            # If a position was opened, handle_signal either fully protected it or
            # executed the fail-safe flatten before reaching this point.
            log.exception("message processing failed")
            notify_admin(f"🚨 Unexpected executor error for channel signal={signal_uid} update_id={update_id}: {e}")
            break

    state["last_signal_update_id"] = last_update_id
    state["github_signals_processed_lines"] = len(lines)  # informational only
    state["last_successful_poll_ts"] = time.time()
    save_state(state)



def handle_bot_command(command: str) -> str:
    """Admin bot command handler. Returns a user-visible Persian report."""
    global _gh_outbox
    try:
        gh=_gh_outbox
        nx=NobitexClient(NobitexConfig(public_key=NOBITEX_PUBLIC_KEY,private_key_b64=NOBITEX_PRIVATE_KEY,base_url=NOBITEX_BASE_URL,public_base_url=NOBITEX_PUBLIC_BASE_URL))
        state=load_state()
        parts=command.strip().split()
        cmd=parts[0].lower() if parts else ""
        if cmd in ("/status","/config"):
            c=load_control(gh); bal=nx.get_margin_usdt_balance();
            return (f"📊 وضعیت\nExecutor: 🟢 online\nورود جدید: {'🟢 فعال' if c['enabled'] else '🔴 متوقف'}\n"
                    f"Risk cap: {c['risk_usdt']} USDT\nMax collateral/trade: {c['max_collateral_usdt']} USDT\n"
                    f"Max open trades: {c['max_open_trades']}\nOpen trades(state): {len(state.get('open_trades',{}))}\nMargin USDT: {bal}")
        if cmd=="/balance":
            m=nx.get_margin_usdt_balance(); s=nx.get_spot_usdt_balance(); return f"💰 موجودی USDT\nMargin active: {m}\nSpot active: {s}"
        if cmd=="/positions":
            ps=nx.list_positions(status="active").get("positions",[])
            if not ps: return "📈 هیچ پوزیشن فعالی در نوبیتکس نیست."
            lines=[f"📈 پوزیشن‌های فعال: {len(ps)}"]
            for p in ps: lines.append(f"#{p.get('id')} {str(p.get('srcCurrency')).upper()}/{str(p.get('dstCurrency')).upper()} {str(p.get('side')).upper()} · entry={p.get('entryPrice')} · liability={p.get('liability')} · PNL={p.get('unrealizedPNL')}")
            return "\n".join(lines)[:4000]
        if cmd=="/pause":
            c=load_control(gh); c["enabled"]=False; save_control(gh,c); return "⏸ ورود معاملات جدید متوقف شد. پوزیشن‌های باز دست‌نخورده می‌مانند."
        if cmd=="/resume":
            c=load_control(gh); c["enabled"]=True; save_control(gh,c); return "▶️ ورود معاملات جدید فعال شد."
        if cmd=="/risk" and len(parts)>1:
            v=D(parts[1]);
            if v<=0 or v>10: return "❌ Risk باید بین 0 و 10 USDT باشد."
            c=load_control(gh); c["risk_usdt"]=str(v); save_control(gh,c); return f"✅ Risk cap به {v} USDT تغییر کرد."
        if cmd=="/collateral" and len(parts)>1:
            v=D(parts[1]);
            if v<=0 or v>100: return "❌ Max collateral باید بین 0 و 100 USDT باشد."
            c=load_control(gh); c["max_collateral_usdt"]=str(v); save_control(gh,c); return f"✅ سقف مارجین هر معامله به {v} USDT تغییر کرد."
        if cmd=="/maxtrades" and len(parts)>1:
            v=int(parts[1]);
            if v<1 or v>100: return "❌ Max trades باید بین 1 و 100 باشد."
            c=load_control(gh); c["max_open_trades"]=v; save_control(gh,c); return f"✅ حداکثر معاملات همزمان: {v}"
        if cmd=="/test_api":
            markets=nx.get_margin_markets().get("markets",{}); bal=nx.get_margin_usdt_balance(); return f"🧪 API OK\nMargin markets: {len(markets)}\nMargin USDT: {bal}"
        if cmd in ("/reconcile", "/sync"):
            reconcile_on_startup(nx,state); return "🔄 Reconciliation اجرا شد؛ گزارش کامل آن در تلگرام ارسال شد."
        if cmd in ("/protection", "/audit"):
            return audit_open_trade_protection(nx, state, flatten_on_failure=True)
        if cmd=="/logs":
            try:
                tail = Path("executor.log").read_text(encoding="utf-8", errors="replace").splitlines()[-25:]
                return "🧾 آخرین لاگ‌ها\n" + ("\n".join(tail) if tail else "(خالی)")
            except Exception as e:
                return f"❌ خواندن لاگ ناموفق بود: {e}"
        if cmd=="/close" and len(parts)>1:
            key_to_close = parts[1].upper()
            if key_to_close not in state.get("open_trades", {}):
                return f"❌ معامله {key_to_close} در state پیدا نشد."
            _close_remaining_market(nx, state, key_to_close, "admin_close")
            return f"🚨 فرمان بستن {key_to_close} اجرا شد."
        if cmd=="/closeall":
            keys=list(state.get("open_trades",{}).keys())
            for k in keys: _close_remaining_market(nx,state,k,"admin_close_all")
            return f"🚨 دستور بستن همه ارسال شد. تعداد state اولیه: {len(keys)}"
        return "❓ دستور ناشناخته. /menu را بزنید."
    except Exception as e:
        log.exception("bot command failed")
        return f"🚨 اجرای دستور ناموفق بود: {type(e).__name__}: {e}"

def main() -> int:
    global _gh_outbox
    acquire_single_instance_lock()

    _gh_outbox = GithubClient(GITHUB_REPO, GITHUB_PAT, GITHUB_BRANCH)
    gh = GithubClient(GITHUB_REPO, GITHUB_PAT, GITHUB_BRANCH)
    gh.validate_repository()
    nx = NobitexClient(NobitexConfig(
        public_key=NOBITEX_PUBLIC_KEY,
        private_key_b64=NOBITEX_PRIVATE_KEY,
        base_url=NOBITEX_BASE_URL,
        public_base_url=NOBITEX_PUBLIC_BASE_URL,
    ))
    state = load_state()

    # Telegram is deliberately NOT contacted from Windows.  The GitHub Actions
    # bridge is the only Telegram-facing component; it relays channel posts and
    # admin commands through signals.jsonl / commands.jsonl and relays outbox.jsonl
    # back to Telegram. This is important when Telegram is filtered on the Windows host.

    if "testnet" in NOBITEX_BASE_URL.lower():
        notify_admin("🚨 NOBITEX_BASE_URL هنوز TESTNET است؛ اجرای واقعی متوقف شد.")
        raise RuntimeError("Refusing to run on testnet configuration for this production executor")

    notify_admin(
        f"🟢 Executor started\nNobitex={NOBITEX_BASE_URL}\n"
        f"Risk cap=${load_control(gh).get('risk_usdt', DEFAULT_RISK_USDT)} · Leverage={LEVERAGE}x · GitHub={GITHUB_REPO}"
    )
    reconcile_on_startup(nx, state)

    consecutive_errors = 0
    while True:
        try:
            poll_commands(nx, gh, state)
            poll_once(nx, gh, state)
            update_runner_trailing(nx, state)
            consecutive_errors = 0
        except Exception as e:
            consecutive_errors += 1
            log.exception("main loop error #%s", consecutive_errors)
            if consecutive_errors in (1, 5) or consecutive_errors % 20 == 0:
                notify_admin(f"🚨 Main loop error #{consecutive_errors}: {e}")
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    sys.exit(main())
