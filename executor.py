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
from logging.handlers import RotatingFileHandler
import os
import re
import socket
import sys
import time
import uuid
from decimal import Decimal, ROUND_DOWN, ROUND_UP, InvalidOperation
from pathlib import Path
from typing import Any, Dict, Optional

from github_client import GithubClient
import user_store as us
import ui_text as ui
from nobitex_client import NobitexClient, NobitexConfig, NobitexAPIError, is_transient_error
from signal_parser import parse_message, ParsedSignal, ParsedEvent
import fwd_signal as fwd
from fwd_signal import target_weights as fwd_target_weights
from state_store import load_state, save_state as _raw_save_state, DEFAULT_PATH as _DEFAULT_STATE_PATH
import contextlib
import hashlib

# While a subscriber's account is being processed (_user_context), every
# save_state(state) call in this file - there are ~50 of them, none aware of
# multi-tenancy - must write THAT subscriber's own state file, never the
# admin's state.json (which would silently replace the admin's real open
# trades with a subscriber's). This wrapper redirects them all with no
# call-site changes; it is the admin's default file whenever no subscriber
# context is active.
_active_state_path: Optional[str] = None


def save_state(state: Dict[str, Any], path: Optional[str] = None) -> None:
    _raw_save_state(state, path or _active_state_path or _DEFAULT_STATE_PATH)

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        # Rotating instead of a plain FileHandler: after months of running
        # 24/7 a single ever-growing executor.log could eventually fill the
        # disk. 10MB x 5 backups keeps recent history (~50MB total) without
        # ever growing unbounded. /logs still reads the current file exactly
        # as before.
        RotatingFileHandler("executor.log", maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"),
    ],
)
log = logging.getLogger("executor")

# Bumped with each delivered fix set. Shown in the startup message and
# /status specifically so a "this doesn't seem to be applied" report can be
# checked in one glance against what was actually deployed - several past
# reports in this project turned out to be an older executor.py still
# running on Windows after only the GitHub copy (or nothing at all) had been
# updated.
EXECUTOR_BUILD = "2026-10-04-r33"

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
# Fallback/seed value only - the leverage actually used per trade is resolved
# dynamically per market (see resolve_leverage) and is admin-adjustable via
# control.json's "leverage" key (the /leverage command). This constant is just
# what a brand-new control.json starts with.
DEFAULT_LEVERAGE = Decimal(os.environ.get("DEFAULT_LEVERAGE", "10"))
# Absolute safety ceiling: whatever the admin sets via /leverage, and whatever
# Nobitex reports as a market's own max, the leverage actually used never
# exceeds this - a guard against e.g. a typo like "/leverage 500".
MAX_LEVERAGE_CAP = Decimal(os.environ.get("MAX_LEVERAGE_CAP", "20"))
DEFAULT_MAX_COLLATERAL_USDT = Decimal("1.25")
DEFAULT_MAX_OPEN_TRADES = 15
RISK_USDT = DEFAULT_RISK_USDT
W1, W2, W3, W4, WRUNNER = map(Decimal, ("0.20", "0.30", "0.15", "0.10", "0.25"))

# Every status Nobitex can report for a position that is definitively NOT
# open anymore. Used everywhere a position's live status decides whether to
# drop a trade from state. "expired" was missing until 2026-09-23: a position
# Nobitex reported as status="Expired" fell into neither the "open" branch
# nor this set, so it was treated as "unknown status - do nothing", and the
# trade sat in /positions forever (already closed on the exchange, but
# Executor kept believing otherwise). If Nobitex ever reports a new terminal
# status this list doesn't know about, the same silent-limbo bug recurs -
# _position_status_safe()/`/positions` is the way to notice; add the new
# status here rather than guessing whether "not open" implies "closed" (that
# guess is exactly what caused 11 positions to be wrongly dropped on 2026-09-21).
TERMINAL_POSITION_STATUSES = ("closed", "liquidated", "done", "past", "expired", "canceled", "cancelled")
TRAILING_R_MULT = Decimal("1.5")

POLL_INTERVAL_SECONDS = max(2, int(os.environ.get("POLL_INTERVAL_SECONDS", "2")))
TRAILING_CHECK_SECONDS = max(5, int(os.environ.get("TRAILING_CHECK_SECONDS", "10")))
STALE_GAP_ALERT_SECONDS = int(os.environ.get("STALE_GAP_ALERT_SECONDS", str(20 * 3600)))
STOP_LIMIT_BUFFER_PCT = Decimal(os.environ.get("STOP_LIMIT_BUFFER_PCT", "0.005"))
PROTECTION_VERIFY_RETRIES = max(2, int(os.environ.get("PROTECTION_VERIFY_RETRIES", "5")))
PROTECTION_VERIFY_DELAY_SECONDS = max(0.2, float(os.environ.get("PROTECTION_VERIFY_DELAY_SECONDS", "0.7")))
MIN_BALANCE_BUFFER_USDT = Decimal(os.environ.get("MIN_BALANCE_BUFFER_USDT", "0.05"))
# How often Executor announces it is still alive (see _write_heartbeat). Kept
# well below GitHub's write rate limits - one small commit per minute - while
# still letting bridge.py notice within a few minutes if the Windows process
# itself stops entirely (crash, power/internet loss), which nothing else in
# this system could otherwise detect.
HEARTBEAT_INTERVAL_SECONDS = max(20, int(os.environ.get("HEARTBEAT_INTERVAL_SECONDS", "60")))

# A signal that sat unprocessed (executor offline, GitHub relay backlog, etc.)
# for too long relative to its own candle timeframe no longer describes the
# current market - opening it now would mean entering at a stale price with a
# stop/targets computed for a level the market has likely already moved past.
# The allowed age scales with the signal's timeframe (a 5M signal goes stale
# far sooner than a 4H one), clamped to a sane floor/ceiling.
MAX_SIGNAL_AGE_MULTIPLIER = float(os.environ.get("MAX_SIGNAL_AGE_MULTIPLIER", "2"))
MIN_SIGNAL_AGE_MINUTES = float(os.environ.get("MIN_SIGNAL_AGE_MINUTES", "10"))
MAX_SIGNAL_AGE_MINUTES = float(os.environ.get("MAX_SIGNAL_AGE_MINUTES", "90"))

# Minimum time between automatic retries of a still-incomplete protection
# (see resolve_needs_protection). Keeps a persistently failing repair attempt
# from hammering the Nobitex API every single main-loop tick.
NEEDS_PROTECTION_RETRY_SECONDS = max(5, int(os.environ.get("NEEDS_PROTECTION_RETRY_SECONDS", "60")))

# Exchange-truth sync: how often each open trade is re-checked against Nobitex
# (target fills, definitive closure) and how often orphaned positions are looked for.
SYNC_INTERVAL_SECONDS = max(30, int(os.environ.get("SYNC_INTERVAL_SECONDS", "60")))
# Once a trade has banked a target, every further target moves the stop for the whole rest of the position. If the
# executor notices a fill late (60 s) on a fast 1M/5M move, price can spike through T3/T4 and fall back onto the OLD,
# lower stop - the trade then ends near +2R instead of >= +3R. Running trades are therefore checked more often.
RUNNING_SYNC_SECONDS = max(10, int(os.environ.get("RUNNING_SYNC_SECONDS", "15")))


def _sync_interval_for(trade: dict) -> float:
    try:
        if any((t or {}).get("hit") for t in (trade.get("targets") or {}).values()):
            return float(RUNNING_SYNC_SECONDS)
    except Exception:
        pass
    return float(SYNC_INTERVAL_SECONDS)
ORPHAN_CHECK_SECONDS = max(60, int(os.environ.get("ORPHAN_CHECK_SECONDS", "300")))
# How often an unmatched (still-not-found-in-channel) orphan re-alerts the admin.
ORPHAN_ALERT_COOLDOWN_SECONDS = max(300, int(os.environ.get("ORPHAN_ALERT_COOLDOWN_SECONDS", "1800")))
_nx_for_history: Optional[NobitexClient] = None
_last_orphan_check = 0.0
_last_transient_notice: Dict[str, float] = {}
# Global (not per-trade) tracker for total connectivity loss to Nobitex
# (DNS/VPN/internet down on the Windows host, code "NetworkError"). Distinct
# from ordinary rate-limit backoff: this is the case a screenshot showed -
# the exact same multi-line traceback repeated every 5 minutes forever from
# update_runner_trailing. Alerts now back off (30m, 1h, 2h, 4h, capped at 6h)
# instead of firing on a fixed 5-minute cadence, are one short line instead
# of the full traceback, and a single "back online" message fires on recovery.
_network_issue = {"since": 0.0, "last_alert": 0.0, "count": 0}
NETWORK_ALERT_BASE_SECONDS = 1800
NETWORK_ALERT_MAX_SECONDS = 6 * 3600

# ---------------------------------------------------------------------------
# Stop-loss guard (r31). Root cause it fixes: each target slice is protected by
# an OCO whose stop leg is a STOP-LIMIT (Nobitex OCO cannot use stop-market).
# When the price gaps through the limit, that slice's stop triggers but never
# fills, so only part of the trade (e.g. the stop-market runner) closes and the
# rest stays open. The guard watches the live price against the CURRENT stop of
# every open trade and, if the stop is breached and something is still open, it
# cancels the resting orders and closes the WHOLE remaining volume at market,
# verifying (with retries) that nothing is left.
# ---------------------------------------------------------------------------
STOP_GUARD_INTERVAL_SECONDS = max(1.0, float(os.environ.get("STOP_GUARD_INTERVAL_SECONDS", "3")))
STOP_GUARD_CONFIRM_SECONDS = max(2.0, float(os.environ.get("STOP_GUARD_CONFIRM_SECONDS", "4")))
STOP_GUARD_MIN_READINGS = max(2, int(os.environ.get("STOP_GUARD_MIN_READINGS", "2")))
PRICE_CACHE_SECONDS = max(1.0, float(os.environ.get("PRICE_CACHE_SECONDS", "2.5")))
FLATTEN_ATTEMPTS = max(2, int(os.environ.get("FLATTEN_ATTEMPTS", "6")))
FLATTEN_VERIFY_DELAY_SECONDS = max(0.3, float(os.environ.get("FLATTEN_VERIFY_DELAY_SECONDS", "0.8")))
FLATTEN_RETRY_SECONDS = max(5, int(os.environ.get("FLATTEN_RETRY_SECONDS", "10")))
# Anything smaller than this (in USDT notional) cannot be closed by the exchange
# because of its minimum order size; it is reported, never looped on forever.
DUST_NOTIONAL_USDT = Decimal(os.environ.get("DUST_NOTIONAL_USDT", "0.05"))
DUST_AUTO_ACCEPT_USDT = Decimal(os.environ.get("DUST_AUTO_ACCEPT_USDT", "1.0"))
# A stop with less than this many percent between it and the liquidation price
# gets the "increase safety distance" button (see cmd /liqsafe).
LIQ_GAP_WARN_PCT = Decimal(os.environ.get("LIQ_GAP_WARN_PCT", "5"))
_price_cache: Dict[str, tuple] = {}


def _note_network_trouble(context: str, err: Exception) -> None:
    # Local DNS/VPN/internet outages on the Windows host are common and not
    # actionable from inside a Telegram message, so this is log-only now (no
    # notify_admin) - the admin explicitly asked to stop seeing it. Executor
    # keeps retrying underneath exactly as before; nothing else changes.
    now = time.time()
    if _network_issue["since"] == 0.0:
        _network_issue["since"] = now
    log.warning("network trouble (%s): %s", context, err)


def _note_network_ok() -> None:
    if _network_issue["since"] != 0.0:
        down_for = time.time() - _network_issue["since"]
        log.warning("network connectivity restored after %.0fs", down_for)
        _network_issue["since"] = 0.0
        _network_issue["last_alert"] = 0.0
        _network_issue["count"] = 0

_SINGLE_INSTANCE_PORT = int(os.environ.get("SINGLE_INSTANCE_PORT", "47632"))
_singleton_socket: Optional[socket.socket] = None
_gh_outbox: Optional[GithubClient] = None
# Set by run_user_cycle() for the duration of ONE multi-tenant user's cycle,
# then always reset to None in a finally block - single-threaded main loop,
# so this simple module global (not a real contextvar) is enough. Lets every
# existing notify_admin(...) call across the whole codebase (hundreds of call
# sites: trade opened, target hit, protection issue, etc.) route to that
# user's own Telegram chat with ZERO changes to any of those call sites,
# instead of every one of them needing a chat_id threaded through. Stays None
# for the admin's own account cycle, so admin behavior is 100% unchanged.
_active_user_chat_id: Optional[str] = None
_active_user_auto_exit: bool = True     # this subscriber follows channel exit events automatically
_ui_uid: Optional[str] = None           # subscriber whose account the running handler acts on (None = admin's own)
_ui_admin_view: bool = False            # the ADMIN is looking at that subscriber's account (buttons must stay admin-side)
_caller_meta: Dict[str, Any] = {}        # Telegram profile info of whoever sent the command being handled
_admin_auto_exit_cache = {"ts": 0.0, "value": True}
_vault: Optional["us.UserVault"] = None
_pricing: Optional["us.PricingConfig"] = None
_discounts: Optional["us.DiscountStore"] = None
_ledger: Optional["us.PaymentLedger"] = None
_payment_info: Optional["us.PaymentInfo"] = None
_gh_main: Optional[GithubClient] = None
# Non-admin (multi-tenant) callers may only ever reach this fixed whitelist -
# every other command string, whatever it is, is refused before any admin
# logic runs. This check does not trust bridge.py's own filtering (defense
# in depth): even if bridge.py ever had a routing bug, a tagged non-admin
# command still cannot fall through to /close, /adduser, /removeuser, etc.
SELF_SERVICE_COMMANDS = {
    "/start", "/help", "/menu", "/terms", "/accept", "/decline", "/plans", "/prices", "/subscribe", "/cancelpay", "/paid",
    "/usediscount", "/cleardiscount",
    "/connect_enc", "/connectguide", "/support", "/mysubscription", "/mystatus",
    "/mytrades", "/myhistory", "/mypnl", "/mybalance", "/myclose", "/mycloseall", "/mypause", "/myresume",
    "/mysettings", "/myvalue", "/myset", "/myrisk", "/reqcap", "/myautoexit", "/mydailyreport",
    "/myliqsafe", "/myliqadd", "/mycolall", "/disconnect", "/myconfirmclose", "/mydismissclose",
}
# Prompts (the bridge asks for a value, then relays a command) that a subscriber's chat may start.
USER_ASK_PROMPTS = {"apikey", "receipt", "support", "discount", "setrisk", "setcol", "setlev", "setmax", "setdaily",
                    "colallpct", "colallto", "reqcap"}
_CONTROL_PATH = "control.json"
_COMMANDS_PATH = "commands.jsonl"
_HEARTBEAT_PATH = "heartbeat.json"
# A message notify_admin() cannot get onto GitHub right now (network outage,
# GitHub itself unreachable) is queued here on local disk instead of being
# dropped - see notify_admin()/flush_pending_outbox(). This is the fix for
# the 2026-09-24 incident: outbox.jsonl writes kept timing out
# ("Connection aborted"/SSLWantWriteError) for over an hour, and every one of
# those admin messages (including command confirmations) was silently lost,
# so commands appeared to "not get a response" even though they had already
# executed. Survives an executor restart since it lives on disk, not memory.
_LOCAL_OUTBOX_QUEUE_PATH = Path("outbox_pending.jsonl")
_last_heartbeat_write = 0.0


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


def write_heartbeat(state: Dict[str, Any]) -> None:
    """Announce that Executor is still alive and looping, at most once every
    HEARTBEAT_INTERVAL_SECONDS. bridge.py reads this file to detect and alert
    on a Windows-side outage (crash, power/internet loss) - something no
    other part of this system could otherwise notice, since a dead executor
    can't push its own "I'm down" message.
    """
    global _last_heartbeat_write
    now = time.time()
    if now - _last_heartbeat_write < HEARTBEAT_INTERVAL_SECONDS or _gh_outbox is None:
        return
    try:
        _gh_outbox.put_file(
            _HEARTBEAT_PATH,
            json.dumps({"ts": now, "open_trades": len(state.get("open_trades", {}))}, ensure_ascii=False),
            message="chore: executor heartbeat [skip ci]",
        )
        _last_heartbeat_write = now
    except Exception:
        log.exception("heartbeat write failed")



def _mid_of_line(line: str) -> Optional[str]:
    try:
        mid = json.loads(line).get("mid")
        return str(mid) if mid else None
    except Exception:
        return None


def _enqueue_outbox(payload: Dict[str, Any]) -> None:
    # Every outbox message carries a unique id. If the network hides the result of a write (timeout after GitHub
    # already stored it), the retry/flush paths check for this id first, and the bridge ignores an id it already
    # delivered - so one message can never reach Telegram twice.
    payload.setdefault("mid", uuid.uuid4().hex)
    line = json.dumps(payload, ensure_ascii=False)
    if _gh_outbox is None:
        _queue_outbox_locally(line)
        return
    try:
        _gh_outbox.append_line("outbox.jsonl", line, dedupe_marker=payload["mid"])
    except Exception as e:
        log.error("نوشتن outbox ناموفق بود؛ پیام محلی صف شد تا بعداً دوباره تلاش شود: %s", e)
        _queue_outbox_locally(line)


def notify_admin(message: str, *, key: Optional[str] = None, buttons: Optional[list] = None,
                  chat_id: Optional[str] = None, kb: Optional[str] = None) -> None:
    """Send a message to the admin via the outbox.

    key: the trade key (e.g. "ETH_5M") this message is about, if any. bridge.py
    uses this to reply-thread every message about the same trade under that
    trade's first ("signal received") message, so the full history of one
    trade is easy to find. Omitted for messages that aren't about one
    specific trade.
    buttons: optional inline-keyboard buttons - either a single row
    ([{"text":..., "callback_data":...}, ...]) or multiple rows
    ([[...], [...]]) - rendered under the message in Telegram so the admin
    can act in one tap instead of typing a command.

    Multi-tenant routing: an explicit chat_id sends to that subscriber's chat.
    Otherwise, while run_user_cycle()/_user_context() is active
    (_active_user_chat_id), the message goes to THAT subscriber's own chat
    with admin-only buttons removed (a subscriber's taps only ever reach the
    self-service commands), and any failure-type message (starting with the
    red-alert or cross emoji) is also mirrored to the admin, tagged with the
    subscriber id, so problems on a user's account are never invisible to
    the admin. With neither, it is the admin's own account: unchanged.
    """
    log.warning("ADMIN: %s", message)
    target = chat_id or _active_user_chat_id
    payload: Dict[str, Any] = {"ts": time.time(), "text": message}
    if key:
        payload["key"] = key
    if target:
        payload["chat_id"] = str(target)
        if buttons:
            # Anything sent to a subscriber's chat may only carry buttons a subscriber is allowed to
            # press (the bridge and the command handler enforce the same list again).
            rows = buttons if isinstance(buttons[0], list) else [buttons]
            kept = [[b for b in row if _user_btn_ok(str(b.get("callback_data", ""))) or _is_signup_btn(b)] for row in rows]
            kept = [row for row in kept if row]
            buttons = kept or None
    if buttons:
        payload["buttons"] = buttons
    if kb:
        payload["kb"] = kb
    _enqueue_outbox(payload)
    if _active_user_chat_id and not chat_id and message.lstrip().startswith(("🚨", "❌", "💰 موجودی کم")):
        _enqueue_outbox({"ts": time.time(), "text": f"[کاربر {_active_user_chat_id}] {message}"})


def _queue_outbox_locally(line: str) -> None:
    """Append one already-serialized outbox line to the on-disk fallback
    queue. Never raises - if even this fails (disk full, permissions), the
    message is still in the log via the ADMIN: line above."""
    try:
        with open(_LOCAL_OUTBOX_QUEUE_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        log.exception("even the local outbox fallback queue failed to write")


def flush_pending_outbox() -> None:
    """Retry every locally-queued message (oldest first) against the real
    GitHub outbox. Called once per main-loop tick, so a message queued during
    a network outage goes out automatically as soon as GitHub is reachable
    again - no admin action needed, nothing is lost, and order is preserved.
    Stops at the first line that still fails (keeps ordering; avoids hammering
    GitHub with the whole backlog every single tick) and leaves the rest
    queued for the next tick."""
    if _gh_outbox is None or not _LOCAL_OUTBOX_QUEUE_PATH.exists():
        return
    try:
        lines = _LOCAL_OUTBOX_QUEUE_PATH.read_text(encoding="utf-8").splitlines()
    except Exception:
        log.exception("could not read local outbox fallback queue")
        return
    lines = [l for l in lines if l.strip()]
    if not lines:
        _LOCAL_OUTBOX_QUEUE_PATH.unlink(missing_ok=True)
        return
    sent = 0
    for line in lines:
        try:
            _gh_outbox.append_line("outbox.jsonl", line, dedupe_marker=_mid_of_line(line))
            sent += 1
        except Exception as e:
            log.warning("flush_pending_outbox: still failing (%d/%d sent so far): %s", sent, len(lines), e)
            break
    remaining = lines[sent:]
    if remaining:
        try:
            _LOCAL_OUTBOX_QUEUE_PATH.write_text("\n".join(remaining) + "\n", encoding="utf-8")
        except Exception:
            log.exception("could not rewrite local outbox fallback queue")
    else:
        _LOCAL_OUTBOX_QUEUE_PATH.unlink(missing_ok=True)
        log.warning("flush_pending_outbox: all %d queued admin message(s) delivered", sent)


def _ack_button(key: str) -> list:
    """Standard 'reviewed, stop tracking automatically' buttons for a repeat
    alert about a specific trade key, plus a way back to the main menu."""
    return [
        [{"text": "✅ بررسی شد", "callback_data": f"ack:{key}"}, {"text": "📈 معاملات باز", "callback_data": "cmd:/positions"}],
        [{"text": "📋 منو", "callback_data": "menu"}],
    ]


def symbol_to_currencies(raw_symbol: str) -> tuple[str, str]:
    s = raw_symbol.upper().replace("/", "")
    for suffix in ("USDT", "USD", "IRT"):
        if s.endswith(suffix) and len(s) > len(suffix):
            return s[:-len(suffix)].lower(), suffix.lower()
    return s.lower(), "usdt"


def trade_key(symbol: str, timeframe: str) -> str:
    return f"{symbol.upper()}_{timeframe.upper()}"


def _tnums(trade: dict) -> list:
    """Target numbers a trade really has (channel trades: 1-4; forwarded signals: 1..N)."""
    nums = []
    for k in (trade.get("targets") or {}):
        try:
            nums.append(int(k))
        except (TypeError, ValueError):
            continue
    return sorted(nums) or [1, 2, 3, 4]


def _runner_pct(trade: dict) -> Decimal:
    """Share kept as the trailing runner. Channel trades: WRUNNER (25%). Forwarded signals: 0."""
    try:
        return D(trade["runner_pct"]) if trade.get("runner_pct") is not None else WRUNNER
    except (InvalidOperation, TypeError, KeyError):
        return WRUNNER


def _is_small_order_error(e: Exception) -> bool:
    """Nobitex refused an order because it is below the market's minimum size (SmallOrder / AmountTooLow)."""
    txt = f"{getattr(e, 'code', '')} {getattr(e, 'message', '')} {e}".lower().replace("_", "").replace(" ", "")
    return "smallorder" in txt or "amounttoolow" in txt


def _merged_pct(trade: dict) -> Decimal:
    """Share of the position whose own target order was too small for the exchange and was folded into the runner."""
    total = Decimal("0")
    for t in (trade.get("targets") or {}).values():
        if t.get("merged"):
            try:
                total += D(t.get("pct", "0"))
            except (InvalidOperation, TypeError):
                pass
    return total


def _runner_share(trade: dict) -> Decimal:
    """Everything the runner stop must cover: its own share plus every slice merged into it."""
    return _runner_pct(trade) + _merged_pct(trade)


def _merge_target_into_runner(trade: dict, n, t: dict) -> dict:
    """Turn target n into a 'merged' slice: no TP/SL orders of its own (the exchange would reject an order this
    small); its volume stays in the position and is protected by the runner stop. The target price is kept so a
    channel 'Target n HIT' message (or the price reaching it) still moves the stop exactly as before."""
    merged = {"target": int(n), "pct": t.get("pct"), "tp_price": t.get("tp_price"), "hit": bool(t.get("hit")),
              "amount": "0", "tp_order_id": None, "sl_order_id": None, "status": "merged", "merged": True}
    trade["targets"][str(n)] = merged
    log.warning("T%s of %s is below the exchange minimum order size: merged into the runner", n, trade.get("symbol"))
    return merged


class TransientAPIError(Exception):
    """Nobitex is rate-limiting / unreachable / answered something we cannot
    interpret. This says NOTHING about whether a position or order exists.
    Every caller must skip or retry - never remove state, never conclude
    "closed", never cancel-and-give-up. (Root cause of the 2026-09-21 incident:
    a TooManyRequests answer was read as "position not open" and 11 live
    positions were dropped from state.)"""


class SignalFinalized(Exception):
    """Raised once a channel signal has already resulted in a definitive,
    terminal exchange action - the position was either fully protected, or it
    could not be protected and was flagged for admin attention (never auto-closed).

    poll_once() catches this specifically and marks the signal processed
    either way, so the same channel message is never treated as a fresh,
    not-yet-opened signal again. Without this, a signal whose calculated size
    happens to fall below Nobitex's minimum order size on one of the four R:R
    targets would be reopened and re-flattened forever (observed live: the
    same SOL_1H signal cycling every ~15-20s), burning real fees on a real
    position each time. Any exception that is NOT SignalFinalized means
    nothing irreversible happened yet (e.g. the entry order itself could not
    be placed), so it is still safe and worthwhile for poll_once to retry.
    """


def _timeframe_to_minutes(timeframe: str) -> Optional[float]:
    m = re.match(r"^\s*(\d+)\s*([MHD])\s*$", str(timeframe or "").upper())
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2)
    return float(n) * {"M": 1, "H": 60, "D": 1440}[unit]


def max_signal_age_seconds(timeframe: str) -> float:
    """How old a not-yet-opened signal is allowed to be before it is treated
    as stale and skipped instead of opened. Scales with the signal's own
    candle timeframe (a 5M signal is stale far sooner than a 4H one, since the
    price/stop/targets it names were only ever meant to describe that one
    candle), clamped to a sane floor and ceiling.
    """
    tf_minutes = _timeframe_to_minutes(timeframe)
    if tf_minutes is None:
        tf_minutes = MIN_SIGNAL_AGE_MINUTES
    minutes = min(MAX_SIGNAL_AGE_MINUTES, max(MIN_SIGNAL_AGE_MINUTES, tf_minutes * MAX_SIGNAL_AGE_MULTIPLIER))
    return minutes * 60.0


# Recurring channel broadcasts that are informational only (daily/weekly
# performance recaps, etc.) and are never trade signals. parse_message()
# correctly returns None for these, but before this they were still logged as
# "could not be parsed" and forwarded to the admin every single time one was
# posted, which is noisy and irrelevant to running the executor.
_KNOWN_NON_SIGNAL_BROADCAST_MARKERS = (
    "SIGNAL PERFORMANCE",  # covers "DAILY SIGNAL PERFORMANCE", "WEEKLY SIGNAL PERFORMANCE", etc.
)


def _is_known_broadcast(text: str) -> bool:
    upper = (text or "").upper()
    return any(marker in upper for marker in _KNOWN_NON_SIGNAL_BROADCAST_MARKERS)


def _exchange_realized_pnl(trade: dict) -> tuple:
    """Realized P&L straight from Nobitex's own figure for the CLOSED position
    (never from strategy prices). Returns (Decimal|None, source). Best effort:
    any API problem returns (None, '') and the caller falls back to the sum of
    the real order fills (see _collect_fills) or, last, a labeled estimate."""
    nx = _nx_for_history
    if nx is None:
        return None, ""
    try:
        pos = nx.get_position_status(int(trade["position_id"])).get("position") or {}
        if str(pos.get("status", "")).lower() != "open":
            for f in ("PNL", "pnl", "realizedPNL", "realizedPnl", "realizedPnL"):
                v = pos.get(f)
                if v not in (None, ""):
                    try:
                        return D(v), "nobitex_position"
                    except (InvalidOperation, TypeError):
                        continue
    except Exception:
        pass
    return None, ""


_FILL_LABELS_FA = {
    "TP": "🎯 تارگت {n} (حد سود)",
    "SL": "🛑 حد ضرر (بخش تارگت {n})",
    "RUNNER_SL": "🏃 رانر / حد ضرر متحرک",
    "MARKET_CLOSE": "⚡ بستن با قیمت بازار",
    "LIMIT_CLOSE": "📌 بستن با قیمت مشخص",
    "REMAINDER": "📦 باقیمانده‌ی معامله (قیمت تخمینی)",
    "ADJ": "🧾 کارمزد/بهره و اختلاف تسویه",
}


def _fill_label(role: str) -> str:
    m = re.match(r"^T(\d)_(TP|SL)$", str(role))
    if m:
        return _FILL_LABELS_FA[m.group(2)].format(n=m.group(1))
    return _FILL_LABELS_FA.get(str(role), str(role))


def _role_sort_key(role: str) -> tuple:
    m = re.match(r"^T(\d)_(TP|SL)$", str(role))
    if m:
        return (int(m.group(1)), 0 if m.group(2) == "TP" else 1)
    if str(role).startswith("RUNNER"):
        return (5, 0)
    return (6, 0)


def _trade_order_refs(trade: dict) -> list:
    """Every order id this trade is known to have placed, with its role. Merges
    the persistent orders_log (r31+) with the ids still stored on the targets /
    runner / close list, so trades opened by an older build are covered too."""
    refs: Dict[int, str] = {}
    for e in trade.get("orders_log") or []:
        try:
            refs[int(e["id"])] = str(e.get("role") or "ORDER")
        except (KeyError, TypeError, ValueError):
            continue
    for n, t in (trade.get("targets") or {}).items():
        for fld, suffix in (("tp_order_id", "TP"), ("sl_order_id", "SL")):
            if t.get(fld):
                try:
                    refs.setdefault(int(t[fld]), f"T{n}_{suffix}")
                except (TypeError, ValueError):
                    pass
    r = trade.get("runner") or {}
    if r.get("order_id"):
        try:
            refs.setdefault(int(r["order_id"]), "RUNNER_SL")
        except (TypeError, ValueError):
            pass
    for oid in trade.get("close_order_ids") or []:
        try:
            refs.setdefault(int(oid), "MARKET_CLOSE")
        except (TypeError, ValueError):
            pass
    return sorted(refs.items(), key=lambda kv: (_role_sort_key(kv[1]), kv[0]))


def _slice_pnl(side: Optional[str], entry: Decimal, price: Decimal, amount: Decimal) -> Decimal:
    return ((price - entry) if side == "LONG" else (entry - price)) * amount


def _estimate_fill_lines(trade: dict, exit_price: Optional[Decimal]) -> list:
    """No usable exchange data: every hit target at its own price, and the rest
    at the exit (or current stop) price. Always labelled estimated."""
    side = trade.get("side")
    lines = []
    try:
        entry = D(trade.get("entry_actual", "0"))
        initial = D(trade.get("initial_amount", "0"))
        if entry <= 0 or initial <= 0:
            return []
        accounted = Decimal("0")
        for n in _tnums(trade):
            t = (trade.get("targets") or {}).get(str(n)) or {}
            if t.get("hit") and t.get("tp_price") and not t.get("merged"):
                amt = initial * D(t.get("pct", "0"))
                px = D(t["tp_price"])
                lines.append({"role": f"T{n}_TP", "label": _fill_label(f"T{n}_TP"), "amount": str(amt),
                              "price": str(px), "pnl": str(_slice_pnl(side, entry, px, amt)), "exact": False})
                accounted += amt
        rest = initial - accounted
        ref = exit_price if exit_price is not None else D(trade.get("stop_price", trade.get("original_stop", "0")))
        if rest > initial * Decimal("0.001") and ref and ref > 0:
            lines.append({"role": "REMAINDER", "label": _fill_label("REMAINDER"), "amount": str(rest),
                          "price": str(ref), "pnl": str(_slice_pnl(side, entry, ref, rest)), "exact": False})
    except (InvalidOperation, TypeError, ZeroDivisionError):
        return []
    return lines


def _collect_fills(nx: Optional[NobitexClient], trade: dict, exit_price: Optional[Decimal] = None,
                   budget_seconds: float = 20.0) -> dict:
    """Line-by-line fills of a finished trade, from what Nobitex really executed
    (matched amount x average price of every order this trade ever placed).
    Returns {"lines": [...], "exact": bool}. If the exchange cannot answer for
    some order, or part of the volume cannot be attributed, that part is added
    as an explicitly-labelled estimate - the report never silently drops volume."""
    side = trade.get("side")
    try:
        entry = D(trade.get("entry_actual", "0"))
        initial = D(trade.get("initial_amount", "0"))
    except (InvalidOperation, TypeError):
        return {"lines": [], "exact": False}
    if nx is None or entry <= 0:
        return {"lines": _estimate_fill_lines(trade, exit_price), "exact": False}

    lines: list = []
    exact = True
    matched_total = Decimal("0")
    started = time.time()
    for oid, role in _trade_order_refs(trade)[:40]:
        if time.time() - started > budget_seconds:
            exact = False
            break
        try:
            o = nx.get_order_status(int(oid)).get("order") or {}
        except Exception:
            exact = False
            continue
        try:
            matched = D(o.get("matchedAmount", "0") or "0")
            if matched <= 0:
                continue
            px = D(o.get("averagePrice") or "0")
            if px <= 0:
                px = D(o.get("price") or "0")
            if px <= 0:
                exact = False
                continue
        except (InvalidOperation, TypeError):
            exact = False
            continue
        lines.append({"role": role, "label": _fill_label(role), "amount": str(matched), "price": str(px),
                      "pnl": str(_slice_pnl(side, entry, px, matched)), "exact": True, "order_id": oid})
        matched_total += matched

    if not lines:
        return {"lines": _estimate_fill_lines(trade, exit_price), "exact": False}

    if initial > 0 and matched_total < initial * Decimal("0.995"):
        rest = initial - matched_total
        ref = exit_price if exit_price is not None else D(trade.get("stop_price", trade.get("original_stop", "0")))
        if ref and ref > 0:
            lines.append({"role": "REMAINDER", "label": _fill_label("REMAINDER"), "amount": str(rest),
                          "price": str(ref), "pnl": str(_slice_pnl(side, entry, ref, rest)), "exact": False})
            exact = False
    return {"lines": lines, "exact": exact}


def _realized_so_far(trade: dict) -> Decimal:
    """Profit already banked by hit targets of a still-open trade (reference
    target prices x planned slice volume; used for live progress messages)."""
    side = trade.get("side")
    total = Decimal("0")
    try:
        entry = D(trade.get("entry_actual", "0"))
        initial = D(trade.get("initial_amount", "0"))
        for t in (trade.get("targets") or {}).values():
            if t.get("hit") and t.get("tp_price") and not t.get("merged"):
                total += _slice_pnl(side, entry, D(t["tp_price"]), initial * D(t.get("pct", "0")))
    except (InvalidOperation, TypeError):
        pass
    return total


def _hist_value(h: dict) -> Optional[Decimal]:
    """Headline result of one history entry: Nobitex's real number when we have
    it, otherwise the labelled estimate."""
    raw = h.get("realized_usdt") if h.get("realized_usdt") is not None else h.get("realized_usdt_estimate")
    if raw is None:
        return None
    try:
        return D(raw)
    except (InvalidOperation, TypeError, ValueError):
        return None


def _cumulative_total(state: Dict[str, Any]) -> Decimal:
    """All-time realized total of this account (survives history trimming)."""
    total = D(state.get("history_base_total", "0") or "0")
    for h in state.get("trade_history") or []:
        v = _hist_value(h)
        if v is not None:
            total += v
    return total


def _record_trade_history(state: Dict[str, Any], key: str, trade: dict, reason: str, note: str = "",
                          exit_price: Optional[Decimal] = None) -> None:
    """Append a closed trade to the persistent history log (state["trade_history"]).
    Pure record-keeping for /history, /pnl and the closing report - it never
    reads back into any trading decision.

    Every entry now carries "fills": the exact line-by-line list of what closed
    (target / stop / runner / market close, each with amount, price and P&L), so
    reports can show each step with the running total. The headline result is
    Nobitex's own position PNL when available; otherwise the sum of the real
    fills; otherwise a labelled estimate."""
    targets = trade.get("targets", {}) or {}
    hit_list = [f"T{n}" for n in _tnums(trade) if (targets.get(str(n)) or {}).get("hit")]
    fills = _collect_fills(_nx_for_history, trade, exit_price)
    lines = fills["lines"]
    lines_sum: Optional[Decimal] = None
    if lines:
        try:
            lines_sum = sum((D(x["pnl"]) for x in lines), Decimal("0"))
        except (InvalidOperation, TypeError, KeyError):
            lines_sum = None

    exch, exch_src = _exchange_realized_pnl(trade)
    if exch is not None and lines and lines_sum is not None:
        diff = exch - lines_sum
        if abs(diff) > Decimal("0.000001"):
            lines.append({"role": "ADJ", "label": _fill_label("ADJ"), "amount": "", "price": "",
                          "pnl": str(diff), "exact": True})

    realized_usdt: Optional[Decimal] = lines_sum
    if exch is not None:
        realized_usdt = exch

    r_multiple = None
    if realized_usdt is not None:
        try:
            risk_cap_usdt = D(trade.get("risk_usdt", "0"))
            if risk_cap_usdt > 0:
                r_multiple = realized_usdt / risk_cap_usdt
        except (InvalidOperation, TypeError):
            pass

    if exit_price is None and lines:
        for ln in reversed(lines):
            if ln.get("role") not in ("ADJ",) and ln.get("price"):
                try:
                    exit_price = D(ln["price"])
                except InvalidOperation:
                    pass
                break

    entry = {
        "key": key,
        "symbol": trade.get("symbol"),
        "timeframe": trade.get("timeframe"),
        "side": trade.get("side"),
        "entry_actual": trade.get("entry_actual"),
        "original_stop": trade.get("original_stop"),
        "final_stop": trade.get("stop_price"),
        "targets_hit": hit_list,
        "closed_pct": trade.get("closed_pct"),
        "trailing_active": bool(trade.get("trailing_active")),
        "leverage": trade.get("leverage"),
        "risk_usdt": trade.get("risk_usdt"),
        "collateral": trade.get("collateral"),
        "initial_amount": trade.get("initial_amount"),
        "position_id": trade.get("position_id"),
        "opened_at": trade.get("opened_at"),
        "closed_at": time.time(),
        "reason": reason,
        "note": note,
        "signal_id": trade.get("signal_id"),
        "exit_price": str(exit_price) if exit_price is not None else None,
        "fills": lines,
        "fills_exact": bool(fills.get("exact")) and bool(lines),
    }
    if exch is not None:
        entry["realized_usdt"] = str(exch)
        entry["pnl_source"] = exch_src
        if r_multiple is not None:
            entry["r_multiple"] = str(r_multiple)
    elif realized_usdt is not None and fills.get("exact"):
        entry["realized_usdt"] = str(realized_usdt)
        entry["pnl_source"] = "fills"
        if r_multiple is not None:
            entry["r_multiple"] = str(r_multiple)
    else:
        entry["pnl_source"] = "estimate"
        if realized_usdt is not None:
            entry["realized_usdt_estimate"] = str(realized_usdt)
            if r_multiple is not None:
                entry["r_multiple_estimate"] = str(r_multiple)
    hist = state.setdefault("trade_history", [])
    hist.append(entry)
    if len(hist) > 400:
        # Keep the all-time running total correct even after old entries are trimmed.
        base = D(state.get("history_base_total", "0") or "0")
        for old_h in hist[:-400]:
            v = _hist_value(old_h)
            if v is not None:
                base += v
        state["history_base_total"] = str(base)
        hist = hist[-400:]
    state["trade_history"] = hist


def D(v: Any) -> Decimal:
    return Decimal(str(v))


def fmt_amount(v: Decimal) -> str:
    # Nobitex position liability can require up to 10 decimal places.
    return format(v.quantize(Decimal("0.0000000001"), rounding=ROUND_DOWN), "f")


def fmt_price(v: Decimal) -> str:
    # Preserve enough precision for small altcoins without inventing precision.
    q = Decimal("0.00000001") if abs(v) < 1 else Decimal("0.01")
    return format(v.quantize(q, rounding=ROUND_DOWN), "f")


def fmt_leverage(v: Decimal) -> str:
    # Always send a clean integer-style string ("10", never "10.0"/"10.00") -
    # a stray decimal format is one plausible way a requested leverage could
    # be silently rejected or defaulted by the exchange instead of erroring.
    v = v.quantize(Decimal("1"), rounding=ROUND_DOWN)
    return format(v, "f")


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


def calculate_position_size(sig: ParsedSignal, collateral_cap: Decimal, risk_cap: Decimal,
                            leverage: Decimal) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    entry = D(sig.entry); stop = D(sig.stop)
    distance = abs(entry - stop)
    if distance <= 0: raise ValueError("Entry و Stop نمی‌توانند برابر باشند")
    stop_pct = distance / entry
    # Keep collateral small enough to allow many concurrent positions, while
    # never allowing planned stop loss to exceed the configured risk ceiling.
    effective_risk = min(risk_cap, collateral_cap * stop_pct * leverage)
    amount = effective_risk / distance
    notional = amount * entry
    collateral = notional / leverage
    return amount, notional, collateral, effective_risk


def resolve_leverage(nx: NobitexClient, src: str, dst: str, control: dict) -> tuple[Decimal, Decimal, Optional[Decimal]]:
    """Leverage to use for this specific market: the admin's configured
    preference (control.json "leverage", changed via /leverage), bounded only
    by MAX_LEVERAGE_CAP as a typo guard (e.g. against "/leverage 500").

    Nobitex's /margin/markets/list "maxLeverage" field is READ and RETURNED
    for the admin-facing diagnostic message, but is no longer used to cap the
    requested leverage - confirmed live that it can under-report a market's
    real capability (it reported BTC's max as 5x while the same Nobitex
    account could manually select 10x for BTC on the exchange's own site,
    and the bot's own BTC/ETH trades were being held down to 5x because of
    it). Sending the admin's real preference and trusting the post-open
    leverage readback in _finalize_opened_position (which already handles a
    market genuinely applying something different than requested) is more
    accurate than pre-filtering against a field that can be wrong.

    Returns (leverage_to_use, admin_preference, market_max_or_None) so the
    caller can show the admin exactly what was requested vs what Nobitex
    reports as this market's general maximum, for transparency.
    """
    preferred = D(control.get("leverage", DEFAULT_LEVERAGE))
    preferred = max(Decimal("1"), min(preferred, MAX_LEVERAGE_CAP))
    try:
        market_max = nx.market_max_leverage(src, dst)
    except Exception:
        market_max = None
    return preferred, preferred, market_max


def load_control(gh: GithubClient) -> dict:
    default={"enabled":True,"risk_usdt":str(DEFAULT_RISK_USDT),"max_collateral_usdt":str(DEFAULT_MAX_COLLATERAL_USDT),
             "max_open_trades":DEFAULT_MAX_OPEN_TRADES,"leverage":str(DEFAULT_LEVERAGE)}
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


def _side_ok(position_side: Any, side: str) -> bool:
    """Does a Nobitex position's side match our LONG/SHORT? The exchange reports buy/sell; accept long/short too."""
    got = str(position_side or "").lower()
    return got in (("buy", "long") if str(side).upper() in ("LONG", "BUY") else ("sell", "short"))


def _get_new_position_after_open(nx: NobitexClient, src: str, dst: str, side: str, before_ids: set[int],
                                  max_attempts: int = 40) -> Optional[dict]:
    """Find the position created by this signal, not an older same-market position.

    Nobitex's positions/list endpoint can briefly lag behind a just-filled market
    order. The previous budget here (10 x 0.8s = 8s) was proven too short in
    production: a real BTC_5M and a real SOL_15M entry both exceeded it and were
    left with no positionId at all, meaning NO stop-loss/target orders were ever
    placed for those live, leveraged positions. We now retry patiently for
    roughly a minute, with a gentle backoff, and if the strict before/after diff
    still comes up empty we fall back to the newest open position on the exact
    same market+side before giving up. Any remaining failure is handled by the
    caller via the pending-protection queue (see resolve_pending_protection),
    never by silently walking away from an open position.
    """
    wanted_side = side.lower()
    for attempt in range(max_attempts):
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
            if not _side_ok(p.get("side"), side):
                continue
            candidates.append(p)
        if candidates:
            return candidates[-1]
        time.sleep(min(2.0, 0.8 + attempt * 0.1))

    # Strict id-diff never resolved (e.g. a snapshot was missed during a network
    # hiccup, exactly like the api.github.com/Nobitex timeouts already seen in
    # this deployment's logs). Adopting the newest open position on this exact
    # market/side is far safer than leaving a real position unprotected.
    try:
        return nx.find_open_position_by_market(src, dst, side)
    except NobitexAPIError:
        return None


def _position_status(nx: NobitexClient, trade: dict) -> Optional[dict]:
    """Live position dict from Nobitex.

    Returns None ONLY when Nobitex definitively answers "no such position"
    (HTTP 404 / NotFound). Any other failure (rate limit, network, 5xx,
    unknown error) raises TransientAPIError so callers can never mistake an
    API problem for a closed position."""
    try:
        result = nx.get_position_status(int(trade["position_id"])).get("position")
        _note_network_ok()
        return result
    except NobitexAPIError as e:
        if e.code in ("HTTP404", "NotFound") or e.http_status == 404:
            _note_network_ok()
            log.warning("position %s not found on Nobitex: %s", trade.get("position_id"), e)
            return None
        log.warning("position status unavailable %s: %s", trade.get("position_id"), e)
        if e.code == "NetworkError":
            _note_network_trouble(f"بررسی وضعیت پوزیشن {trade.get('symbol','?')}", e)
        raise TransientAPIError(f"{e.code}: {e.message}") from e


def _position_status_safe(nx: NobitexClient, trade: dict) -> Optional[dict]:
    """For informational use only (notes/messages): None on any failure."""
    try:
        return _position_status(nx, trade)
    except TransientAPIError:
        return None


def _pos_open(status: Optional[dict]) -> bool:
    return bool(status) and str(status.get("status", "")).lower() == "open"


def _tracked_position_ids(state: Dict[str, Any]) -> set:
    ids = set()
    for t in state.get("open_trades", {}).values():
        try:
            ids.add(int(t.get("position_id")))
        except (TypeError, ValueError):
            pass
    return ids


def _find_untracked_position(nx: NobitexClient, state: Dict[str, Any], src: str, dst: str, side: str) -> Optional[dict]:
    """Newest open position on this market/side that no tracked trade owns.
    (The old code adopted the newest position on the market even when it
    belonged to another timeframe's trade, which double-booked protection.)"""
    tracked = _tracked_position_ids(state)
    wanted = "buy" if side.upper() == "LONG" else "sell"
    positions = nx.list_positions(status="active", src_currency=src, dst_currency=dst).get("positions", [])
    cands = []
    for p in positions:
        try:
            pid = int(p.get("id"))
        except (TypeError, ValueError):
            continue
        if pid in tracked:
            continue
        if str(p.get("status", "")).lower() != "open" or str(p.get("side", "")).lower() != wanted:
            continue
        cands.append(p)
    cands.sort(key=lambda p: p.get("openedAt") or p.get("createdAt") or "", reverse=True)
    return cands[0] if cands else None


def _stop_is_tighter(side: str, new: Decimal, old: Decimal) -> bool:
    return new > old if side == "LONG" else new < old


def _expected_stop_for_hits(trade: dict) -> Optional[Decimal]:
    """Where the strategy says the stop must be, given which targets are hit:
    T1 -> entry, T2 -> T1 price, T3 -> T2 price, T4 -> T3 price."""
    if trade.get("fixed_stop"):
        return None            # forwarded signal: the provider's stop stays exactly where it was given
    targets = trade.get("targets") or {}
    top = 0
    for n in _tnums(trade):
        if (targets.get(str(n)) or {}).get("hit"):
            top = n
    if top == 0:
        return None
    try:
        if top == 1:
            return D(trade["entry_actual"])
        return D(targets[str(top - 1)]["tp_price"])
    except (KeyError, InvalidOperation, TypeError):
        return None


def _sync_targets_from_exchange(nx: NobitexClient, trade: dict) -> list:
    """Make the target 'hit' flags follow what Nobitex really filled, instead
    of depending only on channel messages (which can be missed during an
    offline period or a GitHub outage). Live case: BNB/DOGE/LINK/XLM had T1
    filled on the exchange while state said T1 pending, which made every
    verification 'fail' and every rebuild try to re-place an already-passed T1.
    Raises TransientAPIError if the exchange cannot answer."""
    newly = []
    targets = trade.get("targets") or {}
    for n in _tnums(trade):
        t = targets.get(str(n)) or {}
        if not t or t.get("hit") or not t.get("tp_order_id"):
            continue
        try:
            order = nx.get_order_status(int(t["tp_order_id"])).get("order") or {}
        except NobitexAPIError as e:
            if is_transient_error(e):
                raise TransientAPIError(f"{e.code}: {e.message}") from e
            continue
        status = str(order.get("status", "")).lower()
        try:
            planned = D(t.get("amount", "0"))
            matched = D(order.get("matchedAmount", "0"))
        except (InvalidOperation, TypeError):
            planned = matched = Decimal("0")
        if status == "done" or (planned > 0 and matched >= planned * Decimal("0.999")):
            t["hit"] = True
            t["filled_by_exchange"] = True
            newly.append(n)
    if newly:
        trade["closed_pct"] = str(sum((D(x.get("pct", "0")) for x in targets.values() if x.get("hit") and not x.get("merged")), Decimal("0")))
    return newly


def _order_position_id(o: dict) -> Optional[int]:
    for k in ("positionId", "position_id"):
        if o.get(k) not in (None, ""):
            try:
                return int(o[k])
            except (TypeError, ValueError):
                pass
    p = o.get("position")
    try:
        if isinstance(p, dict) and p.get("id") is not None:
            return int(p["id"])
        if isinstance(p, (int, str)) and str(p).isdigit():
            return int(p)
    except (TypeError, ValueError):
        pass
    return None


def _trade_price_levels(trade: dict) -> list:
    levels = []
    for t in (trade.get("targets") or {}).values():
        if t.get("tp_price"):
            levels.append(D(t["tp_price"]))
    for k in ("original_stop", "stop_price", "entry_actual"):
        if trade.get(k):
            try:
                levels.append(D(trade[k]))
            except (InvalidOperation, TypeError):
                pass
    out = []
    for p in levels:
        if p > 0:
            out.append(p)
            out.append(p * (Decimal("1") - STOP_LIMIT_BUFFER_PCT))
            out.append(p * (Decimal("1") + STOP_LIMIT_BUFFER_PCT))
    return out


def _find_position_orders(nx: NobitexClient, trade: dict) -> list:
    """Ids of open close-orders on the exchange that belong to this position
    but may not be recorded in state (e.g. created by an earlier rebuild that
    failed half-way). Matching: exact position id when the order carries one;
    otherwise only closing-side orders whose price/stop equals one of this
    trade's own price levels. Best effort - returns [] on any problem."""
    try:
        src, dst = symbol_to_currencies(trade["symbol"])
        resp = nx.list_orders(src_currency=src, dst_currency=dst, status="open", trade_type="margin", details=2)
    except Exception:
        return []
    orders = resp.get("orders") or []
    pid = int(trade["position_id"])
    closing = "sell" if trade.get("side") == "LONG" else "buy"
    levels = _trade_price_levels(trade)
    ids = []
    for o in orders:
        oid = o.get("id")
        if oid is None:
            continue
        op = _order_position_id(o)
        if op is not None:
            if op == pid:
                ids.append(int(oid))
            continue
        if str(o.get("type", "")).lower() != closing:
            continue
        for fld in ("price", "stopPrice", "stopLimitPrice"):
            v = o.get(fld)
            if v in (None, "", "0", 0):
                continue
            try:
                dv = D(v)
            except (InvalidOperation, TypeError):
                continue
            if any(abs(dv - lv) / lv <= Decimal("0.0002") for lv in levels):
                ids.append(int(oid))
                break
    return ids


def _leverage_ladder(lv: Decimal) -> list:
    out = [lv]
    for x in (Decimal("10"), Decimal("5"), Decimal("3"), Decimal("2")):
        if x < lv and x not in out:
            out.append(x)
    return out


def _liquidation_price(position_data: Optional[dict]) -> Optional[Decimal]:
    """Best-effort read of Nobitex's own reported liquidation price for a
    position. Tries a couple of plausible field names and returns None
    (never guesses, never raises) if none are present - so a wrong/missing
    field name just means the safety note below is silently skipped, it
    never blocks or alters anything.
    """
    if not position_data:
        return None
    for field in ("liquidationPrice", "liqPrice"):
        v = position_data.get(field)
        if v not in (None, "", "0", 0):
            try:
                d = D(v)
                if d > 0:
                    return d
            except (InvalidOperation, TypeError):
                continue
    return None


def _stop_vs_liquidation_note(trade: dict, position_data: Optional[dict]) -> str:
    """Read-only safety note comparing this trade's current stop-loss to
    Nobitex's own reported liquidation price - purely informational, appended
    to admin-facing messages. Never changes any order, stop price, or
    trading decision; if Nobitex doesn't return a usable liquidation price,
    this returns "" instead of guessing at one.
    """
    liq = _liquidation_price(position_data)
    if liq is None:
        return ""
    try:
        stop = D(trade.get("stop_price"))
    except (InvalidOperation, TypeError):
        return ""
    if stop <= 0:
        return ""
    if trade.get("side") == "LONG":
        gap_pct = (stop - liq) / liq * 100
        safe_side = stop > liq
    else:
        gap_pct = (liq - stop) / liq * 100
        safe_side = stop < liq
    if not safe_side:
        return (f"\n🚨 هشدار ایمنی: حد ضرر ({stop}) روی سمت نادرست قیمت لیکویید شدن گزارش‌شده توسط "
                f"نوبیتکس ({liq}) قرار دارد — یعنی ممکن است پوزیشن پیش از رسیدن قیمت به حد ضرر لیکویید "
                f"شود. لطفاً دستی بررسی کنید؛ Executor چیزی را خودکار تغییر نمی‌دهد.")
    if gap_pct < 5:
        return (f"\n⚠️ فاصله‌ی حد ضرر تا قیمت لیکویید شدن (طبق نوبیتکس: {liq}) فقط ٪{gap_pct:.1f} است — "
                f"فاصله‌ی کمی دارد.")
    return f"\nℹ️ فاصله‌ی حد ضرر تا قیمت لیکویید شدن (طبق نوبیتکس: {liq}): ٪{gap_pct:.1f} — وضعیت مناسب."


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


def _log_order(trade: dict, order_id: Optional[int], role: str, amount=None, price=None) -> None:
    """Remember EVERY order this trade ever placed (ids get replaced whenever the
    protection is rebuilt). At close time these ids are queried to build an exact,
    line-by-line report of what really filled and at what price."""
    if not order_id:
        return
    try:
        oid = int(order_id)
    except (TypeError, ValueError):
        return
    log_list = trade.setdefault("orders_log", [])
    if any(int(x.get("id", 0)) == oid for x in log_list):
        return
    log_list.append({"id": oid, "role": role,
                     "amount": str(amount) if amount is not None else None,
                     "price": str(price) if price is not None else None, "ts": time.time()})
    del log_list[:-80]


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
    _log_order(trade, tp_id, f"T{target_num}_TP", amount, tp_price)
    _log_order(trade, sl_id, f"T{target_num}_SL", amount, stop_price)
    return {
        "target": target_num,
        "amount": str(amount),
        "tp_price": str(tp_price),
        "stop_price": str(stop_price),
        "tp_order_id": tp_id,
        "sl_order_id": sl_id,
        "status": "open",
    }


def _create_runner_stop(nx: NobitexClient, trade: dict, amount: Decimal, stop_price: Decimal,
                        role: str = "RUNNER_SL") -> dict:
    resp = nx.place_position_close_stop_market(
        position_id=int(trade["position_id"]),
        amount=fmt_amount(amount),
        stop_price=fmt_price(stop_price)
    )
    _log_order(trade, order_id_from(resp), role, amount, stop_price)
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
        except NobitexAPIError as e:
            if is_transient_error(e):
                raise TransientAPIError(f"{label}: {e.code}: {e.message}") from e
            last_error = e
        except Exception as e:
            last_error = e
        time.sleep(PROTECTION_VERIFY_DELAY_SECONDS)
    raise RuntimeError(f"{label}: verification failed: {last_error}")


def _verify_trade_protection(nx: NobitexClient, trade: dict) -> None:
    # Verify the live position first. If it disappeared, there is nothing to protect.
    # (_position_status raises TransientAPIError on rate-limit/network problems,
    # so those are never reported as "position not open".)
    status = _position_status(nx, trade)
    if not _pos_open(status):
        raise RuntimeError("Position is not open while verifying protection")

    # Trust the exchange for which targets are already filled.
    _sync_targets_from_exchange(nx, trade)

    targets = trade.get("targets", {})
    if not targets:
        raise RuntimeError("Expected at least one target, got none")

    for n in _tnums(trade):
        t = targets.get(str(n)) or {}
        if t.get("merged"):
            continue               # no orders of its own by design: its volume sits under the runner stop
        if t.get("hit"):
            # Already closed - its TP leg filled and Nobitex auto-cancels the
            # paired SL leg as part of the OCO, so nothing live is left to verify.
            continue
        if t.get("tp_missing"):
            # TP price had already been passed when protection was rebuilt, so
            # this slice is protected by a stop only (see _rebuild_protection).
            if not t.get("sl_order_id"):
                raise RuntimeError(f"T{n}: stop order id missing")
            _verify_order_exists(nx, t["sl_order_id"], f"T{n} SL")
            continue
        if not t.get("tp_order_id") or not t.get("sl_order_id"):
            raise RuntimeError(f"T{n}: TP/SL order id missing")
        tp = _verify_order_exists(nx, t["tp_order_id"], f"T{n} TP")
        sl = _verify_order_exists(nx, t["sl_order_id"], f"T{n} SL")
        tp_exec = str(tp.get("execution", "")).lower()
        sl_exec = str(sl.get("execution", "")).lower()
        if "limit" not in tp_exec or "stop" not in sl_exec:
            raise RuntimeError(f"T{n}: invalid protection executions TP={tp_exec}, SL={sl_exec}")

    if _runner_share(trade) <= 0:
        return                 # no runner slice (forwarded signal): every slice has its own TP/SL pair
    runner = trade.get("runner") or {}
    if not runner.get("order_id"):
        raise RuntimeError("Runner stop order id missing")
    _verify_order_exists(nx, runner["order_id"], "Runner SL")


def _flag_protection_issue(nx: NobitexClient, state: Dict[str, Any], key: str, trade: dict, reason: str,
                           quiet: bool = False, extra_hint: str = "", extra_buttons: Optional[list] = None) -> None:
    """A trade's protection could not be created/verified.

    Per explicit instruction, Executor NEVER closes an open position on its
    own initiative - a trade only ends by hitting its own take-profit/stop-
    loss on the exchange, or by the admin explicitly choosing to close it
    (via /close or the button on this alert). This function never touches
    the exchange at all: it only records the problem in needs_protection (so
    resolve_needs_protection() keeps trying, in the background, to complete
    the missing protection - never to close anything) and alerts the admin
    with buttons that carry out whatever the admin decides.
    """
    if not quiet:
        log.error("PROTECTION ISSUE %s: %s", key, reason)
    else:
        log.error("PROTECTION ISSUE retry %s: %s", key, reason)

    rec = state.setdefault("needs_protection", {}).setdefault(key, {"first_seen": time.time(), "alerts_sent": 0})
    rec["reason"] = reason
    rec["last_attempt"] = time.time()
    save_state(state)
    if not quiet:
        buttons = [
            [{"text": "🔁 تلاش مجدد برای تکمیل محافظت", "callback_data": f"cmd:/fixprotection {key}"}],
            [{"text": "❌ بستن این معامله (با تصمیم خودم)", "callback_data": f"cmd:/close {key}"}],
            [{"text": "✅ بررسی شد", "callback_data": f"ack:{key}"}, {"text": "📈 معاملات باز", "callback_data": "cmd:/positions"}],
        ]
        if extra_buttons:
            buttons.append(extra_buttons)
        buttons.append([{"text": "📋 منو", "callback_data": "menu"}])
        notify_admin(
            f"🚨 {key}: مشکلی در ثبت/تأیید محافظت (حد ضرر/تارگت) پیش آمد:\n{reason}{extra_hint}\n\n"
            f"⚠️ طبق تنظیم شما، Executor هیچ معامله‌ای را خودکار نمی‌بندد — این پوزیشن دست‌نخورده "
            f"باقی می‌ماند و در پس‌زمینه مدام تلاش می‌شود محافظت آن کامل شود. اگر می‌خواهید خودتان "
            f"همین الان تصمیم بگیرید، از دکمه‌های زیر استفاده کنید:",
            key=key, buttons=buttons,
        )


def attempt_protection_repair(nx: NobitexClient, state: Dict[str, Any], key: str) -> str:
    """Try to complete this trade's protection (sync fills from the exchange,
    then rebuild whatever is still missing). Never closes the position.
    A rate-limit / network problem changes NOTHING (no state removal, no
    order cancelled) and is simply retried later."""
    trade = state.get("open_trades", {}).get(key)
    if not trade:
        state.get("needs_protection", {}).pop(key, None)
        save_state(state)
        return f"ℹ️ معامله‌ای با کلید {key} در state باز نیست."

    def _busy(e: Exception) -> str:
        rec = state.get("needs_protection", {}).get(key)
        if rec is not None:
            rec["last_attempt"] = time.time()
        save_state(state)
        return (f"⏳ {key}: نوبیتکس موقتاً محدودیت/خطای شبکه داد ({e}). هیچ سفارشی لغو نشد و هیچ چیزی از state حذف نشد؛ "
                f"Executor بعداً خودکار دوباره تلاش می‌کند.")

    try:
        status = _position_status(nx, trade)
    except TransientAPIError as e:
        return _busy(e)
    if not _pos_open(status):
        if status is None or str(status.get("status", "")).lower() in TERMINAL_POSITION_STATUSES:
            _finalize_closed(state, key, trade, "closed_on_exchange", "confirmed closed by Nobitex during protection repair")
            return f"ℹ️ {key}: نوبیتکس تأیید کرد پوزیشن دیگر باز نیست (احتمالاً به حد سود/ضرر خودش رسیده)؛ از صف پیگیری خارج شد."
        return _busy(RuntimeError(f"unexpected position status {status.get('status')!r}"))
    try:
        notes = _rebuild_protection(nx, trade, D(trade.get("stop_price", trade.get("original_stop"))), state)
        _verify_trade_protection(nx, trade)
        state.get("needs_protection", {}).pop(key, None)
        save_state(state)
        extra = ("\n" + "\n".join(notes)) if notes else ""
        return f"✅ {key}: محافظت با موفقیت بازسازی و تأیید شد.{extra}"
    except TransientAPIError as e:
        return _busy(e)
    except Exception as e:
        _flag_protection_issue(nx, state, key, trade, str(e), quiet=True)
        return (f"⚠️ {key}: تلاش برای تکمیل محافظت هنوز ناموفق بود: {e}\n"
                f"پوزیشن بسته نشد؛ Executor خودکار دوباره تلاش می‌کند.")


def _rebuild_protection(nx: NobitexClient, trade: dict, stop_price: Decimal, state: Optional[Dict[str, Any]] = None) -> list:
    """Rebuild all not-yet-hit target OCOs and the runner stop.

    Order of operations matters (all were real bugs before):
      1. Read the live position FIRST. A rate-limit/network problem raises
         TransientAPIError here, before a single order is cancelled (the old
         code cancelled everything first and then silently 'succeeded' with
         nothing placed when the status call failed).
      2. Sync which targets the exchange already filled, so an already-passed
         T1 is never re-placed (that produced PriceConditionFailed loops).
      3. Cancel old orders - including orphans that exist on the exchange but
         were never saved to state (they caused ExceedLiability).
      4. Recreate one order at a time, saving each id to state immediately, so
         a failure half-way can never leave untracked live orders again.
      5. If the strategy stop (e.g. entry after T1) is not valid against the
         current market, fall back to the previous stop instead of leaving the
         slice unprotected; if a TP price was already passed, that slice gets a
         stop-only order.
    Returns a list of human-readable notes for the admin."""
    notes: list = []

    def _save() -> None:
        if state is not None:
            save_state(state)

    status = _position_status(nx, trade)
    if not _pos_open(status):
        return notes
    liability = D(status.get("liability", "0"))
    if liability <= 0:
        return notes

    newly = _sync_targets_from_exchange(nx, trade)
    if newly:
        notes.append("ℹ️ طبق نوبیتکس این تارگت‌ها قبلاً Fill شده بودند: " + ", ".join(f"T{n}" for n in newly))

    old_stop = D(stop_price)
    candidates = [old_stop]
    tight = _expected_stop_for_hits(trade)
    if tight is not None and _stop_is_tighter(trade["side"], tight, old_stop):
        candidates = [tight, old_stop]
    _save()

    for t in trade.get("targets", {}).values():
        if not t.get("hit"):
            _cancel_oco(nx, t)
            if t.get("sl_order_id") and not t.get("tp_order_id"):
                _cancel_order_quiet(nx, t.get("sl_order_id"), f"T{t.get('target')}-sl-only")
    runner = trade.get("runner") or {}
    if runner.get("order_id"):
        _cancel_order_quiet(nx, runner.get("order_id"), "runner-stop")
    for oid in _find_position_orders(nx, trade):
        _cancel_order_quiet(nx, oid, "orphan-position-order")

    # Only slices that were REALLY closed leave the position; a merged slice that "hit" is still open under the runner.
    hit_pct = sum((D(t["pct"]) for t in trade.get("targets", {}).values() if t.get("hit") and not t.get("merged")), Decimal("0"))
    remaining_original = max(Decimal("0"), Decimal("1") - hit_pct)
    if remaining_original <= 0:
        return notes

    def _is_price_condition(e: NobitexAPIError) -> bool:
        return "pricecondition" in str(e.code).lower()

    def _with_stop(fn):
        """Try each candidate stop; only PriceCondition errors move on to the next."""
        last: Optional[NobitexAPIError] = None
        for sp in list(candidates):
            try:
                return fn(sp), sp
            except NobitexAPIError as e:
                last = e
                if _is_price_condition(e):
                    continue
                raise
        assert last is not None
        raise last

    used_stop: Optional[Decimal] = None
    for n, t in sorted(trade["targets"].items(), key=lambda kv: int(kv[0])):
        if t.get("hit") or t.get("merged"):
            continue
        pct = D(t["pct"]) / remaining_original
        amount = (liability * pct).quantize(Decimal("0.0000000001"), rounding=ROUND_DOWN)
        try:
            try:
                created, sp = _with_stop(lambda sp: _create_target_oco(nx, trade, int(n), amount, D(t["tp_price"]), sp))
            except NobitexAPIError as e:
                if _is_small_order_error(e) and amount > 0:
                    raise
                if not _is_price_condition(e) or amount <= 0:
                    raise
                # TP price already passed (or otherwise invalid): protect this slice with a stop only.
                r, sp = _with_stop(lambda sp, _n=n: _create_runner_stop(nx, trade, amount, sp, role=f"T{_n}_SL"))
                created = {"target": int(n), "amount": str(amount), "tp_price": t["tp_price"], "stop_price": str(sp),
                           "tp_order_id": None, "sl_order_id": r.get("order_id"), "status": "open", "tp_missing": True}
                notes.append(f"⚠️ T{n}: قیمت تارگت ({t['tp_price']}) دیگر برای سفارش حدی معتبر نبود؛ این بخش فقط با حد ضرر محافظت شد (بدون TP).")
        except NobitexAPIError as e:
            if not _is_small_order_error(e):
                raise
            # Below the exchange's minimum order size: retrying can never help. Fold the slice into the runner.
            _merge_target_into_runner(trade, n, t)
            notes.append(f"🔀 T{n}: سهم {D(t['pct']) * 100:.0f}٪ از حداقل سایز سفارش نوبیتکس کمتر بود؛ در رانر ادغام شد "
                         f"(بدون حد سود جدا؛ زیر حد ضرر رانر محافظت می‌شود و با رسیدن قیمت به T{n} حد ضرر طبق استراتژی جابه‌جا می‌شود).")
            _save()
            continue
        created["pct"] = t["pct"]
        created["hit"] = False
        trade["targets"][n] = created
        if used_stop is None:
            used_stop = sp
            candidates = [sp]
        trade["stop_price"] = str(used_stop)
        _save()

    runner_amount = (liability * (_runner_share(trade) / remaining_original)).quantize(Decimal("0.0000000001"), rounding=ROUND_DOWN)
    if runner_amount > 0:
        r, sp = _with_stop(lambda sp: _create_runner_stop(nx, trade, runner_amount, sp))
        trade["runner"] = r
        if used_stop is None:
            used_stop = sp
    else:
        trade["runner"] = {"order_id": None, "amount": "0", "stop_price": str(candidates[0]), "status": "none"}
    if used_stop is not None:
        trade["stop_price"] = str(used_stop)
        if tight is not None and used_stop != tight and _stop_is_tighter(trade["side"], tight, used_stop):
            notes.append(f"ℹ️ حد ضرر استراتژی ({tight}) با قیمت فعلی بازار معتبر نبود؛ حد ضرر قبلی ({used_stop}) نگه داشته شد.")
    _save()
    return notes


def _finalize_opened_position(nx: NobitexClient, state: Dict[str, Any], key: str, sig: ParsedSignal,
                               side: str, src: str, risk_cap: Decimal, collateral_cap: Decimal,
                               amount: Decimal, effective_risk: Decimal, notional: Decimal,
                               collateral: Decimal, position: dict, leverage: Decimal) -> None:
    """Given a live Nobitex position that corresponds to `sig`, size-check it,
    persist it into state, and place full TP/SL protection (4 target OCOs +
    runner stop), verifying every order before returning.

    This is the single implementation used both right after a normal market
    open and by resolve_pending_protection() when the positionId could only be
    located on a later retry. One shared code path means a delayed positionId
    lookup is never treated any differently from a fast one - the exact same
    sizing checks and the exact same never-auto-close fail-safe apply either
    way, and protection is placed the moment the position is actually found.
    `leverage` is the per-market value resolve_leverage() already picked for
    this trade (used consistently for both the original sizing and here, so
    there is never a mismatch between planned and actual collateral).
    """
    signal_uid = str(getattr(sig, "source_update_id", "") or f"{key}-{sig.entry}-{sig.stop}")
    safe_uid = "".join(ch if ch.isalnum() else "-" for ch in signal_uid)[-18:]
    prefix = f"tc-{key[:8]}-{safe_uid}".replace("/", "-")[:27]

    actual_entry = D(position.get("entryPrice", sig.entry))
    live_liability = D(position.get("liability", amount))
    if live_liability <= 0:
        notify_admin(f"⚠️ {key}: پوزیشن پیدا شد اما liability آن صفر بود (احتمالاً از قبل بسته شده)؛ اقدامی انجام نشد.", key=key)
        raise SignalFinalized("Nobitex returned an open position with zero liability")

    # Trust what Nobitex actually reports for this position over what we
    # requested when opening it. A report that BTC/ETH kept opening at 5x
    # despite /leverage 10 being set means the exchange may silently apply
    # its own leverage regardless of the value we send - if so, reading it
    # back here is the only way our own collateral/risk math (and what we
    # tell the admin) stays accurate instead of silently trusting a number
    # that was never actually applied.
    leverage_requested = leverage
    reported_leverage_raw = position.get("leverage")
    if reported_leverage_raw not in (None, "", "0", 0):
        try:
            reported_leverage = D(reported_leverage_raw)
            if reported_leverage > 0:
                leverage = reported_leverage
        except (InvalidOperation, TypeError):
            pass

    actual_notional = live_liability * actual_entry
    actual_collateral = actual_notional / leverage
    actual_risk = abs(actual_entry - D(sig.stop)) * live_liability
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
        "leverage": str(leverage),
        "initial_amount": str(live_liability),
        "initial_amount_requested": str(amount),
        "risk_unit": str(abs(D(sig.entry) - D(sig.stop))),
        "targets": {},
        "runner": {},
        "client_prefix": prefix,
        "signal_id": getattr(sig, "signal_id", None) or signal_uid,
        "closed_pct": "0",
        "last_trailing_check": 0,
        "peak": str(actual_entry),
        "trailing_active": False,
        "stop_price": str(sig.stop),
        "opened_at": time.time(),
    }
    # Channel signals: 4 targets (20/30/15/10%) + 25% runner. Forwarded signals bring their own
    # weights (any number of targets, no runner) and keep the provider's stop fixed.
    weights = getattr(sig, "weights", None) or {1: W1, 2: W2, 3: W3, 4: W4}
    runner_share = getattr(sig, "runner_pct", None)
    runner_share = WRUNNER if runner_share is None else D(runner_share)
    trade["runner_pct"] = str(runner_share)
    if getattr(sig, "fixed_stop", False):
        trade["fixed_stop"] = True
    if getattr(sig, "source", None):
        trade["source"] = str(sig.source)
    for n, pct in weights.items():
        trade["targets"][str(n)] = {
            "target": n, "pct": str(pct), "tp_price": str(D(sig.targets[n])),
            "hit": False, "tp_order_id": None, "sl_order_id": None,
        }
    # Persist the position immediately - before the cap checks below and
    # before placing any protection orders. If anything from here on fails
    # (including the position turning out bigger than the configured caps),
    # this trade stays visible in open_trades/needs_protection so the
    # automatic repair loop (and the admin, via the alert's buttons) can
    # still find and act on it, instead of a live, real position silently
    # falling out of tracking.
    state["open_trades"][key] = trade
    save_state(state)

    if actual_collateral > collateral_cap + MIN_BALANCE_BUFFER_USDT:
        _flag_protection_issue(nx, state, key, trade, f"actual collateral {actual_collateral} exceeds cap {collateral_cap}")
        raise SignalFinalized(f"actual collateral {actual_collateral} exceeds cap {collateral_cap}")
    if actual_risk > risk_cap + MIN_BALANCE_BUFFER_USDT:
        _flag_protection_issue(nx, state, key, trade, f"actual stop risk {actual_risk} exceeds cap {risk_cap}")
        raise SignalFinalized(f"actual stop risk {actual_risk} exceeds cap {risk_cap}")

    # The four OCO chunks + runner stop together consume exactly the live
    # liability (subject to 10-decimal downward rounding).
    pcts = dict(weights)
    remaining = live_liability
    try:
        for n, pct in pcts.items():
            chunk = (live_liability * pct).quantize(Decimal("0.0000000001"), rounding=ROUND_DOWN)
            if chunk <= 0:
                raise RuntimeError(f"T{n}: calculated protection amount is zero")
            try:
                created = _create_target_oco(nx, trade, n, chunk, D(sig.targets[n]), D(sig.stop))
            except NobitexAPIError as se:
                if not _is_small_order_error(se):
                    raise
                # Too small for the exchange: keep this slice inside the runner's protection instead of failing.
                _merge_target_into_runner(trade, n, trade["targets"][str(n)])
                save_state(state)
                continue
            remaining -= chunk
            created["pct"] = str(pct)
            created["hit"] = False
            trade["targets"][str(n)] = created
            save_state(state)

        if runner_share > 0 or _merged_pct(trade) > 0:
            runner_amount = max(Decimal("0"), remaining)
            if runner_amount <= 0:
                raise RuntimeError("Runner protection amount is zero")
            trade["runner"] = _create_runner_stop(nx, trade, runner_amount, D(sig.stop))
        else:
            trade["runner"] = {"order_id": None, "amount": "0", "stop_price": str(sig.stop), "status": "none"}
        save_state(state)

        # Critical invariant: do not accept/process the signal until every
        # target OCO and the runner stop are confirmed at Nobitex.
        _verify_trade_protection(nx, trade)

    except Exception as protection_error:
        hint = ""
        hint_buttons = None
        if "smallorder" in str(protection_error).lower():
            hint = (
                "\nℹ️ علت رایج این خطا: کوچک‌ترین تارگت (۱۰٪ حجم) از حداقل سایز سفارش نوبیتکس "
                "برای این بازار کمتر بوده. برای جلوگیری از تکرار، ریسک/مارجین یا اهرم را "
                + ("از «⚙️ تنظیمات» افزایش دهید." if _active_user_chat_id else "با /risk، /collateral یا /leverage افزایش دهید.")
            )
            if _active_user_chat_id:
                hint_buttons = [{"text": "⚙️ تنظیمات", "callback_data": "cmd:/mysettings"}]
            else:
                hint_buttons = [
                    {"text": "📌 ریسک", "callback_data": "risk_menu"},
                    {"text": "💵 مارجین", "callback_data": "collateral_menu"},
                    {"text": "📈 اهرم", "callback_data": "leverage_menu"},
                ]
        _flag_protection_issue(nx, state, key, trade, str(protection_error), extra_hint=hint, extra_buttons=hint_buttons)
        raise SignalFinalized(str(protection_error)) from protection_error

    lev_note = "" if leverage == leverage_requested else (
        f" (⚠️ درخواست‌شده {leverage_requested}x بود؛ نوبیتکس {leverage}x اعمال کرد و محاسبات با همان انجام شد)")
    real_col = position.get("collateral")
    if real_col not in (None, "", "0", 0):
        trade["collateral"] = str(real_col)
    liq_info = _liq_info(trade, position)
    text = ui.card_opened(
        key, side, actual_entry, sig.stop, leverage, lev_note, trade["targets"],
        trade["runner"].get("stop_price"), effective_risk, live_liability, src.upper(),
        trade.get("signal_id"), position["id"], _stop_vs_liquidation_note(trade, position),
        collateral=trade.get("collateral"), runner_pct=str(_runner_share(trade)), fixed_stop=bool(trade.get("fixed_stop")))
    btns = [[_btn_close(key), _btn_positions()]]
    liq_row = _liq_buttons(key, liq_info)
    if liq_row:
        btns.insert(0, liq_row)
    if _ui_uid is None:
        btns.append([{"text": "🛡️ بررسی محافظت", "callback_data": "cmd:/protection"}])
    btns.append([_btn_menu()])
    if trade["targets"] and all(t.get("merged") for t in trade["targets"].values()):
        text += ("\n\n⚠️ حجم این معامله برای حداقل سایز سفارش نوبیتکس آن‌قدر کوچک است که هیچ‌کدام از تارگت‌ها سفارش جدا نگرفتند؛ "
                 "کل پوزیشن فقط با حد ضرر (رانر) محافظت شد و سودی در تارگت‌ها برداشته نمی‌شود. برای اجرای کامل برنامه، ریسک/مارجین را بالا ببرید.")
    save_state(state)
    notify_admin(text, key=key, buttons=btns)


def resolve_pending_protection(nx: NobitexClient, state: Dict[str, Any]) -> None:
    """Fail-safe sweep for positions whose id could not be confirmed right after
    their market order was accepted (see _get_new_position_after_open). Runs
    every main-loop iteration until each pending entry is either found and
    fully protected, or the admin has closed/handled it manually on the
    exchange - it never silently drops a signal that already opened a real
    position.
    """
    pending = state.get("pending_protection", {})
    if not pending:
        return
    for key, rec in list(pending.items()):
        if key in state.get("open_trades", {}):
            pending.pop(key, None)
            continue
        try:
            position = _find_untracked_position(nx, state, rec["src"], rec["dst"], rec["side"])
            if not position:
                age = time.time() - float(rec.get("opened_at", time.time()))
                sent = int(rec.get("alerts_sent", 0))
                # Re-alert roughly every 5 minutes of continued failure instead
                # of only once (and then going silent) or on every 5s poll tick.
                if age > (sent + 1) * 300:
                    notify_admin(
                        f"🚨 هنوز پوزیشن {key} پیدا نشد ({age/60:.0f} دقیقه از سفارش ورود گذشته)؛ "
                        f"لطفاً حساب Nobitex را فوراً به‌صورت دستی بررسی کنید.",
                        key=key, buttons=_ack_button(key),
                    )
                    rec["alerts_sent"] = sent + 1
                continue
            sig = ParsedSignal(
                kind="entry", symbol=rec["symbol"], timeframe=rec["timeframe"], side=rec["side"],
                entry=float(rec["entry_signal"]), stop=float(rec["stop"]),
                targets={int(n): float(p) for n, p in rec["targets"].items()},
            )
            setattr(sig, "source_update_id", rec.get("source_update_id", ""))
            setattr(sig, "signal_id", rec.get("signal_id"))
            notify_admin(f"✅ {key}: positionId #{position.get('id')} پیدا شد؛ در حال ثبت SL/TP...", key=key)
            _finalize_opened_position(
                nx, state, key, sig, rec["side"], rec["src"],
                D(rec["risk_cap"]), D(rec["collateral_cap"]), D(rec["planned_amount"]),
                D(rec["planned_effective_risk"]), D(rec["planned_notional"]), D(rec["planned_collateral"]),
                position, D(rec.get("leverage", DEFAULT_LEVERAGE)),
            )
            pending.pop(key, None)
        except SignalFinalized:
            # Position was found and either fully protected or deliberately
            # flagged for admin attention - _finalize_opened_position/_flag_protection_issue
            # already notified the admin. Either way this entry is resolved.
            pending.pop(key, None)
        except (TransientAPIError, NobitexAPIError) as e:
            if isinstance(e, TransientAPIError) or is_transient_error(e):
                log.warning("resolve_pending_protection %s: exchange busy (%s); will retry", key, e)
                continue
            log.exception("resolve_pending_protection failed for %s", key)
            notify_admin(f"🚨 {key}: تلاش برای پیدا کردن/محافظت پوزیشن معلق شکست خورد: {e}", key=key)
        except Exception as e:
            log.exception("resolve_pending_protection failed for %s", key)
            notify_admin(f"🚨 {key}: تلاش برای پیدا کردن/محافظت پوزیشن معلق شکست خورد: {e}", key=key)
    state["pending_protection"] = pending
    save_state(state)


def resolve_needs_protection(nx: NobitexClient, state: Dict[str, Any]) -> None:
    """Keep retrying, on every main-loop iteration, to complete protection for
    trades flagged by _flag_protection_issue (e.g. the "SmallOrder"/
    "ExceedLiability" cases seen live). This only ever tries to finish
    building the missing TP/SL orders via attempt_protection_repair() - it
    never closes or cancels the position outright. If it keeps failing, the
    admin is re-alerted periodically with the same action buttons (retry,
    close-it-yourself, acknowledge) instead of the executor deciding anything
    on its own.
    """
    needs = state.get("needs_protection", {})
    if not needs:
        return
    for key, rec in list(needs.items()):
        if time.time() - float(rec.get("last_attempt", 0)) < NEEDS_PROTECTION_RETRY_SECONDS:
            continue
        if key not in state.get("open_trades", {}):
            needs.pop(key, None)
            continue
        if key in state.get("needs_flatten", {}):
            continue        # the trade is being closed; do not rebuild orders it is about to cancel
        try:
            attempt_protection_repair(nx, state, key)
        except Exception:
            log.exception("resolve_needs_protection failed for %s", key)
        needs = state.get("needs_protection", {})
        if key in needs:
            age = time.time() - float(needs[key].get("first_seen", time.time()))
            sent = int(needs[key].get("alerts_sent", 0))
            # Re-alert roughly every 5 minutes of continued failure instead of
            # only the first alert and then going quiet.
            if age > (sent + 1) * 300:
                notify_admin(
                    f"🚨 هنوز محافظت {key} کامل نشده ({age/60:.0f} دقیقه از اولین تلاش)؛ پوزیشن دست‌نخورده "
                    f"باقی مانده و هیچ بسته‌شدن خودکاری انجام نشده. تصمیم با شماست:",
                    key=key, buttons=[
                        [{"text": "🔁 تلاش مجدد برای تکمیل محافظت", "callback_data": f"cmd:/fixprotection {key}"}],
                        [{"text": "❌ بستن این معامله (با تصمیم خودم)", "callback_data": f"cmd:/close {key}"}],
                        [{"text": "✅ بررسی شد", "callback_data": f"ack:{key}"}, {"text": "📈 معاملات باز", "callback_data": "cmd:/positions"}],
                    ],
                )
                needs[key]["alerts_sent"] = sent + 1
    state["needs_protection"] = needs
    save_state(state)


def handle_signal(nx: NobitexClient, state: Dict[str, Any], sig: ParsedSignal, gh: GithubClient,
                   control_override: Optional[dict] = None) -> None:
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
                _rebuild_protection(nx, existing_trade, D(existing_trade.get("stop_price", sig.stop)), state)
                save_state(state)
                _verify_trade_protection(nx, existing_trade)
                notify_admin(f"♻️ Protection resumed and verified for existing {key}; no duplicate entry opened.", key=key)
            except Exception as e:
                _flag_protection_issue(nx, state, key, existing_trade, f"protection resume failed: {e}")
                raise SignalFinalized(str(e)) from e
        else:
            try:
                _verify_trade_protection(nx, existing_trade)
                notify_admin(f"⚠️ سیگنال تکراری/همپوشان برای {key} دریافت شد؛ معامله جدید باز نشد و protection تأیید شد.", key=key)
            except Exception as e:
                _flag_protection_issue(nx, state, key, existing_trade, f"duplicate-signal protection verification failed: {e}")
                raise SignalFinalized(str(e)) from e
        return

    control = control_override if control_override is not None else load_control(gh)
    if not bool(control.get("enabled", True)):
        if not _active_user_chat_id:
            notify_admin(f"⏸ {key}: ورودهای جدید غیرفعال است؛ سیگنال اجرا نشد.", key=key)
        return
    if len(state.get("open_trades", {})) >= int(control.get("max_open_trades", DEFAULT_MAX_OPEN_TRADES)):
        notify_admin(f"⛔ {key}: سقف معاملات همزمان پر است ({control.get('max_open_trades')}). "
                     f"این سیگنال اجرا نشد؛ برای معاملات بیشتر، سقف را در تنظیمات بالا ببرید.", key=key,
                     buttons=[[_cb_btn("⚙️ تنظیمات", "cmd:/mysettings" if _ui_uid else "menu")], [_btn_menu()]]); return
    try:
        _dl = D(control.get("daily_loss_usdt", "0") or "0")
    except (InvalidOperation, TypeError):
        _dl = Decimal("0")
    if _dl > 0 and -_today_realized(state) >= _dl:
        _day = time.strftime("%Y-%m-%d")
        if state.get("daily_stop_notified") != _day:
            state["daily_stop_notified"] = _day
            save_state(state)
            notify_admin(f"🛑 سقف ضرر روزانه‌ی شما ({num_s(_dl)} USDT) پر شد؛ برای محافظت از سرمایه‌تان تا فردا معامله‌ی جدیدی "
                         f"باز نمی‌شود (معاملات باز مثل همیشه مدیریت می‌شوند). می‌توانید سقف را در تنظیمات تغییر دهید.",
                         buttons=[[_cb_btn("⚙️ تنظیمات", "cmd:/mysettings" if _ui_uid else "menu")], [_btn_menu()]])
        return
    src, dst = symbol_to_currencies(sig.symbol)
    side = sig.side.upper()
    if dst != "usdt":
        notify_admin(f"⚠️ {key}: فقط بازارهای USDT برای این Executor فعال هستند؛ {src}/{dst} رد شد.", key=key)
        return

    collateral_cap = margin_balance = Decimal("0")
    try:
        if not nx.is_symbol_available(src, dst, "buy" if side == "LONG" else "sell"):
            notify_admin(f"❌ {key}: بازار تعهدی/جهت موردنظر در نوبیتکس فعال نیست.", key=key)
            return

        leverage, _leverage_pref, leverage_market_max = resolve_leverage(nx, src, dst, control)

        risk_cap=D(control.get("risk_usdt", DEFAULT_RISK_USDT)); collateral_cap=D(control.get("max_collateral_usdt", DEFAULT_MAX_COLLATERAL_USDT))
        margin_balance=nx.get_margin_usdt_balance()
        free_slots=max(1, int(control.get("max_open_trades", DEFAULT_MAX_OPEN_TRADES))-len(state.get("open_trades", {})))
        per_trade_budget=min(collateral_cap, margin_balance / Decimal(free_slots))
        if per_trade_budget <= MIN_BALANCE_BUFFER_USDT:
            spot = None
            try:
                spot = nx.get_spot_usdt_balance()
            except Exception:
                pass
            notify_admin(ui.msg_low_balance(collateral_cap, margin_balance, f"باز کردن سیگنال {key}", spot)
                         + "\n⏭ این سیگنال اجرا نشد.", key=key,
                         buttons=[[_btn_positions(), _btn_menu()]]); return
        low_note = ""
        if per_trade_budget < collateral_cap * Decimal("0.999"):
            low_note = ui.low_balance_signal_note(margin_balance, collateral_cap, per_trade_budget)
        amount, notional, collateral, effective_risk = calculate_position_size(sig, per_trade_budget, risk_cap, leverage)
        if amount <= 0:
            raise ValueError("حجم محاسبه‌شده صفر/منفی است")

        open_side = "buy" if side == "LONG" else "sell"
        leverage_detail = (
            f"Leverage={leverage}x (طبق ترجیح شما)\n"
            f"   (نوبیتکس برای این بازار عمومی {leverage_market_max if leverage_market_max is not None else 'نامشخص'}x اعلام می‌کند؛ "
            f"صرفاً اطلاعاتی است و مانع درخواست شما نمی‌شود — نتیجه‌ی واقعی پایین‌تر در همین پیام "
            f"بعد از باز شدن معامله تأیید می‌شود)"
        )

        # This is the first message about this trade - bridge.py uses it as the
        # thread root and reply-threads every later message about this same
        # key (adoption, protection, target hits, closes, alerts) underneath
        # it, so the full history of one trade is easy to find in Telegram.
        notify_admin(
            ui.card_signal_received(
                key, side, sig.entry, sig.stop, leverage, effective_risk, collateral, margin_balance,
                getattr(sig, "signal_id", None) or getattr(sig, "source_update_id", ""),
                detailed=not _active_user_chat_id, low_balance_note=low_note)
            + (("\n" + leverage_detail) if not _active_user_chat_id else ""),
            key=key,
        )

        # Crash recovery before the state file was written. We snapshot active
        # position IDs first, then identify the newly created position by ID.
        # This prevents accidentally adopting an older same-market position.
        active_before = nx.list_positions(status="active").get("positions", [])
        before_ids = {int(p["id"]) for p in active_before if p.get("id") is not None}
        position = None
        tracked_ids = _tracked_position_ids(state)
        for candidate in active_before:
            try:
                if int(candidate.get("id")) in tracked_ids:
                    continue  # already owned by another (e.g. other-timeframe) trade
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
            # Persist everything needed to finish protecting this trade BEFORE
            # placing the order (see resolve_pending_protection). If Nobitex
            # rejects the requested leverage for this market, step down
            # (20 -> 10 -> 5 -> 3 -> 2) and re-size for the leverage that is
            # actually used, instead of failing the signal or retrying it forever.
            ladder = _leverage_ladder(leverage)
            for idx, lv in enumerate(ladder):
                if lv != leverage or idx > 0:
                    amount, notional, collateral, effective_risk = calculate_position_size(sig, per_trade_budget, risk_cap, lv)
                    if amount <= 0:
                        raise ValueError("حجم محاسبه‌شده صفر/منفی است")
                leverage = lv
                state.setdefault("pending_protection", {})[key] = {
                    "symbol": sig.symbol, "timeframe": sig.timeframe, "side": side,
                    "src": src, "dst": dst,
                    "entry_signal": str(sig.entry), "stop": str(sig.stop),
                    "targets": {str(n): str(D(sig.targets[n])) for n in (1, 2, 3, 4)},
                    "source_update_id": str(getattr(sig, "source_update_id", "") or ""),
                    "signal_id": getattr(sig, "signal_id", None),
                    "planned_amount": str(amount), "planned_effective_risk": str(effective_risk),
                    "planned_notional": str(notional), "planned_collateral": str(collateral),
                    "risk_cap": str(risk_cap), "collateral_cap": str(collateral_cap),
                    "leverage": str(leverage), "opened_at": time.time(), "alerts_sent": 0,
                }
                save_state(state)
                try:
                    nx.open_position_market(
                        src_currency=src, dst_currency=dst, side=open_side,
                        amount=fmt_amount(amount), leverage=fmt_leverage(leverage),
                    )
                    break
                except NobitexAPIError as oe:
                    if is_transient_error(oe):
                        raise  # outcome unknown: keep the pending record, retry will adopt, never double-open
                    # Definitive rejection: nothing was opened, so the pending record must not linger
                    # (it would later adopt an unrelated position on this market).
                    state.get("pending_protection", {}).pop(key, None)
                    save_state(state)
                    txt = f"{oe.code} {oe.message}".lower()
                    if ("leverage" in txt or "اهرم" in txt) and idx < len(ladder) - 1:
                        notify_admin(f"⚠️ {key}: نوبیتکس اهرم {lv}x را برای این بازار نپذیرفت ({oe.code}: {oe.message}); "
                                     f"با اهرم {ladder[idx + 1]}x و محاسبه‌ی مجدد حجم دوباره تلاش می‌شود.", key=key)
                        continue
                    raise
            position = _get_new_position_after_open(nx, src, dst, side, before_ids)
        else:
            notify_admin(f"♻️ {key}: matching open position {position.get('id')} adopted; no duplicate entry opened.", key=key)

        if not position:
            notify_admin(
                f"🚨 {key}: سفارش ورود پذیرفته شد اما positionId هنوز پیدا نشد.\n"
                f"این معامله به صف پیگیری خودکار اضافه شد؛ Executor در هر چرخه (هر "
                f"{POLL_INTERVAL_SECONDS} ثانیه) دوباره تلاش می‌کند تا پوزیشن را پیدا کرده و "
                f"بلافاصله SL/TP آن را ثبت کند. اگر تا چند دقیقه دیگر تأیید نشد این هشدار "
                f"تکرار می‌شود. برای بررسی فوری دستی از /positions یا سایت Nobitex استفاده کنید.",
                key=key, buttons=_ack_button(key),
            )
            return

        # Found on the fast path - clear any (just-written) pending marker and
        # place protection through the single shared implementation.
        state.get("pending_protection", {}).pop(key, None)
        _finalize_opened_position(nx, state, key, sig, side, src, risk_cap, collateral_cap,
                                   amount, effective_risk, notional, collateral, position, leverage)

    except (NobitexAPIError, ValueError, InvalidOperation) as e:
        log.exception("handle_signal failed for %s", key)
        transient = isinstance(e, NobitexAPIError) and is_transient_error(e)
        now_t = time.time()
        throttle_key = f"{_active_user_chat_id or 'admin'}:{key}"
        if not transient or now_t - _last_transient_notice.get(throttle_key, 0) > 120:
            _etxt = str(e).lower()
            if isinstance(e, NobitexAPIError) and any(w in _etxt for w in ("insufficient", "balance", "موجودی")):
                notify_admin(ui.msg_low_balance(collateral_cap, margin_balance,
                                                f"باز کردن سیگنال {key}") + "\n⏭ این سیگنال اجرا نشد.", key=key,
                             buttons=[[_btn_positions(), _btn_menu()]])
            else:
                notify_admin(f"❌ اجرای سیگنال {key} شکست خورد: {e}", key=key)
            _last_transient_notice[throttle_key] = now_t
        if transient:
            raise  # retried next cycle; an already-opened position is adopted, never duplicated
        # Definitive rejection: never retry this same message every 2 seconds forever.
        raise SignalFinalized(str(e)) from e


# --------------------------------------------------------------------------- forwarded signals
# The admin forwards (or pastes) a third-party signal into the bot chat: symbol + entry + stop + targets.
# It is executed on the ADMIN's own account only, with the bot's own settings (risk, collateral cap,
# leverage, max trades, daily-loss stop). The entry is a LIMIT order at the signal's entry price; once
# it fills, the normal protection pipeline attaches the stop and the targets (see
# _finalize_opened_position). Subscribers' accounts are never touched by a forwarded signal.
FWD_TIMEFRAME = "FWD"
FWD_ENTRY_TTL_HOURS = float(os.environ.get("FWD_ENTRY_TTL_HOURS", "24"))
FWD_CHECK_SECONDS = 2.0
FWD_PRICE_CHECK_SECONDS = 15.0
FWD_MAX_PRICE_GAP = Decimal("0.25")     # market price this far from the signal's entry => wrong symbol/contract/stale
FWD_MARKET_SLIPPAGE = Decimal(os.environ.get("FWD_MARKET_SLIPPAGE", "0.0015"))   # 'market' entries: worst price we accept (0.15%)
FWD_MARKET_TTL_SECONDS = 120            # a marketable entry that did not fill within 2 minutes is cancelled
FWD_RETRY_WINDOW_SECONDS = 600          # connection problems before anything was placed: keep retrying this long
FWD_RETRY_GAP_SECONDS = 30


class FwdRetryLater(Exception):
    """Nobitex was unreachable (DNS / network / rate limit) BEFORE any order was placed: safe to try again later."""


def _fwd_read(fn, *args, **kwargs):
    """A read-only exchange call with 3 quick tries; a persistent connection problem becomes FwdRetryLater."""
    last = None
    for i in range(3):
        try:
            return fn(*args, **kwargs)
        except NobitexAPIError as e:
            if not is_transient_error(e):
                raise
            last = e
        except TransientAPIError as e:
            last = e
        if i < 2:
            time.sleep(1.5)
    raise FwdRetryLater(str(last))


def _fwd_signal_to_dict(fs) -> dict:
    return {"symbol": fs.symbol, "side": fs.side, "entry": (str(fs.entry) if fs.entry is not None else None),
            "stop": str(fs.stop), "targets": [str(x) for x in fs.targets], "order_type": fs.order_type,
            "warnings": list(fs.warnings or [])}


def _fwd_signal_from_dict(d: dict):
    return fwd.FwdSignal(symbol=d["symbol"], side=d["side"], entry=(D(d["entry"]) if d.get("entry") is not None else None),
                         stop=D(d["stop"]), targets=[D(x) for x in d["targets"]], order_type=d.get("order_type", "limit"),
                         warnings=list(d.get("warnings") or []))


def _queue_fwd_retry(state: Dict[str, Any], fs, signal_id: str, reason: str) -> str:
    key = trade_key(fs.symbol, FWD_TIMEFRAME)
    now = time.time()
    state.setdefault("fwd_retry", {})[key] = {
        "signal": _fwd_signal_to_dict(fs), "signal_id": signal_id, "created_at": now,
        "next_try": now + FWD_RETRY_GAP_SECONDS, "tries": 0, "last_error": str(reason)[:200],
    }
    save_state(state)
    return key


class _FwdSig:
    """ParsedSignal-compatible object for _finalize_opened_position / calculate_position_size."""

    def __init__(self, symbol, side, entry, stop, targets, weights, signal_id):
        self.kind = "entry"
        self.symbol = symbol
        self.timeframe = FWD_TIMEFRAME
        self.side = side
        self.entry = float(entry)
        self.stop = float(stop)
        self.targets = {int(n): float(p) for n, p in targets.items()}
        self.weights = dict(weights)
        self.runner_pct = Decimal("0")
        self.fixed_stop = True
        self.source = "forwarded"
        self.signal_id = signal_id
        self.source_update_id = signal_id


def _fwd_btn_cancel(key: str) -> dict:
    return {"text": "❌ لغو سفارش ورود", "callback_data": f"cmd:/cancelentry {key}"}


def _fwd_sig_from_rec(rec: dict) -> "_FwdSig":
    weights = {int(n): D(w) for n, w in rec["weights"].items()}
    targets = {int(n): D(p) for n, p in rec["targets"].items()}
    return _FwdSig(rec["symbol"], rec["side"], D(rec["entry"]), D(rec["stop"]), targets, weights, rec.get("signal_id"))


def open_forwarded_signal(nx: NobitexClient, state: Dict[str, Any], fs: "fwd.FwdSignal", gh: GithubClient,
                          signal_id: str, from_retry: bool = False) -> Any:
    """Validate + size + place the entry. Returns the reply for the admin (text or (text, buttons)).
    If Nobitex cannot be reached before anything was placed, the signal is kept and retried automatically
    (from_retry=True is used by the retry loop, which handles FwdRetryLater itself)."""
    try:
        return _open_forwarded_signal(nx, state, fs, gh, signal_id)
    except FwdRetryLater as e:
        if from_retry:
            raise
        key = _queue_fwd_retry(state, fs, signal_id, str(e))
        return (f"🌐 اتصال به نوبیتکس برقرار نشد ({str(e)[:160]}). هیچ سفارشی ثبت نشد.\n"
                f"سیگنال {key} نگه داشته شد و تا {FWD_RETRY_WINDOW_SECONDS // 60} دقیقه هر {FWD_RETRY_GAP_SECONDS} ثانیه خودکار دوباره امتحان می‌شود؛ "
                f"قیمت‌ها هر بار از نو بررسی می‌شود، پس سیگنال کهنه اجرا نمی‌شود. "
                f"اگر این پیام تکرار شد، DNS/VPN/اینترنت سیستمی که Executor روی آن اجرا می‌شود را بررسی کنید."), \
               [[_fwd_btn_cancel(key)], [_btn_positions(), _btn_menu()]]


def _open_forwarded_signal(nx: NobitexClient, state: Dict[str, Any], fs: "fwd.FwdSignal", gh: GithubClient,
                           signal_id: str) -> Any:
    key = trade_key(fs.symbol, FWD_TIMEFRAME)
    menu = [[_btn_positions(), _btn_menu()]]
    if key in state.get("open_trades", {}):
        return f"⚠️ برای {key} همین الان یک معامله‌ی باز دارید؛ سیگنال تکراری اجرا نشد.", menu
    if key in state.setdefault("pending_entries", {}):
        return (f"⚠️ برای {key} یک سفارش ورود در انتظار دارید؛ سیگنال تکراری اجرا نشد.",
                [[_fwd_btn_cancel(key)], [_btn_menu()]])
    if key in state.get("fwd_retry", {}) and not getattr(fs, "_from_retry", False):
        return (f"⚠️ سیگنال {key} همین الان در صف تلاش مجدد (قطعی اتصال) است؛ تکراری ثبت نشد.",
                [[_fwd_btn_cancel(key)], [_btn_menu()]])

    control = load_control(gh)
    if not bool(control.get("enabled", True)):
        return "⏸ ورودهای جدید غیرفعال است؛ سیگنال اجرا نشد. (از منو «فعال‌سازی ورود» را بزنید و دوباره فوروارد کنید.)", menu
    n_busy = len(state.get("open_trades", {})) + len(state.get("pending_entries", {}))
    if n_busy >= int(control.get("max_open_trades", DEFAULT_MAX_OPEN_TRADES)):
        return (f"⛔ سقف معاملات همزمان پر است ({control.get('max_open_trades')}؛ شامل سفارش‌های ورودِ در انتظار). "
                f"سیگنال اجرا نشد."), menu
    try:
        _dl = D(control.get("daily_loss_usdt", "0") or "0")
    except (InvalidOperation, TypeError):
        _dl = Decimal("0")
    if _dl > 0 and -_today_realized(state) >= _dl:
        return f"🛑 سقف ضرر روزانه ({num_s(_dl)} USDT) پر شده؛ تا فردا معامله‌ی جدیدی باز نمی‌شود.", menu

    src, dst = symbol_to_currencies(fs.symbol)
    side = fs.side
    open_side = "buy" if side == "LONG" else "sell"
    if dst != "usdt":
        return f"⚠️ فقط بازارهای USDT پشتیبانی می‌شوند؛ {src}/{dst} رد شد.", menu

    collateral_cap = margin_balance = Decimal("0")
    try:
        if not _fwd_read(nx.is_symbol_available, src, dst, open_side):
            return f"❌ بازار تعهدی {src.upper()}/USDT یا این جهت ({'خرید' if side == 'LONG' else 'فروش'}) در نوبیتکس فعال نیست.", menu
        last = _fwd_read(nx.get_last_trade_price, f"{src.upper()}{dst.upper()}")
        stop = D(fs.stop)
        first_tp = D(fs.targets[0])
        is_market = (fs.order_type == "market")
        if is_market:
            ref = D(fs.entry) if fs.entry is not None else None
            if ref is not None and abs(last - ref) / ref > Decimal("0.01"):
                return (f"❌ سیگنال بازاری است ولی قیمت فعلی ({num_s(last)}) بیش از ۱٪ با قیمت ذکرشده ({num_s(ref)}) فاصله دارد؛ "
                        f"سیگنال کهنه است. اجرا نشد."), menu
            # Market entry = a marketable LIMIT order capped at last price +/- FWD_MARKET_SLIPPAGE. The cap is also the
            # price used for sizing, so the real risk can never exceed the configured risk even in the worst fill.
            entry = last * (Decimal(1) + FWD_MARKET_SLIPPAGE) if side == "LONG" else last * (Decimal(1) - FWD_MARKET_SLIPPAGE)
        else:
            entry = D(fs.entry)
            if abs(last - entry) / entry > FWD_MAX_PRICE_GAP:
                return (f"❌ قیمت فعلی {src.upper()} در نوبیتکس ({num_s(last)}) با قیمت ورود سیگنال ({num_s(entry)}) "
                        f"بیش از {int(FWD_MAX_PRICE_GAP * 100)}٪ فاصله دارد؛ احتمالاً نماد/قرارداد اشتباه است یا سیگنال کهنه است. اجرا نشد."), menu
        if (side == "SHORT" and last >= stop) or (side == "LONG" and last <= stop):
            return f"❌ قیمت فعلی ({num_s(last)}) از حد ضرر ({num_s(stop)}) عبور کرده؛ سیگنال باطل است. اجرا نشد.", menu
        if (side == "SHORT" and last <= first_tp) or (side == "LONG" and last >= first_tp):
            return f"❌ قیمت فعلی ({num_s(last)}) به تارگت اول ({num_s(first_tp)}) رسیده/گذشته؛ فرصت ورود تمام شده. اجرا نشد.", menu

        leverage, _lp, lev_market_max = _fwd_read(resolve_leverage, nx, src, dst, control)
        risk_cap = D(control.get("risk_usdt", DEFAULT_RISK_USDT))
        collateral_cap = D(control.get("max_collateral_usdt", DEFAULT_MAX_COLLATERAL_USDT))
        margin_balance = _fwd_read(nx.get_margin_usdt_balance)
        free_slots = max(1, int(control.get("max_open_trades", DEFAULT_MAX_OPEN_TRADES)) - n_busy)
        per_trade_budget = min(collateral_cap, margin_balance / Decimal(free_slots))
        if per_trade_budget <= MIN_BALANCE_BUFFER_USDT:
            return ui.msg_low_balance(collateral_cap, margin_balance, f"باز کردن سیگنال {key}", None) + "\n⏭ سیگنال اجرا نشد.", menu

        weights = fwd_target_weights(len(fs.targets))
        targets = {i + 1: p for i, p in enumerate(fs.targets)}
        fsig = _FwdSig(fs.symbol, side, entry, stop, targets, weights, signal_id)
        amount, notional, collateral, effective_risk = calculate_position_size(fsig, per_trade_budget, risk_cap, leverage)
        if amount <= 0:
            raise ValueError("حجم محاسبه‌شده صفر/منفی است")

        active_before = _fwd_read(nx.list_positions, status="active").get("positions", [])
        before_ids = sorted(int(p["id"]) for p in active_before if p.get("id") is not None)
        rec = {
            "symbol": fs.symbol, "side": side, "src": src, "dst": dst,
            "entry": str(entry), "stop": str(stop),
            "targets": {str(n): str(p) for n, p in targets.items()},
            "weights": {str(n): str(w) for n, w in weights.items()},
            "signal_id": signal_id, "before_ids": before_ids, "order_id": None,
            "planned_amount": str(amount), "planned_effective_risk": str(effective_risk),
            "planned_notional": str(notional), "planned_collateral": str(collateral),
            "risk_cap": str(risk_cap), "collateral_cap": str(collateral_cap),
            "leverage": str(leverage), "created_at": time.time(),
            "order_type": ("market" if is_market else "limit"),
            "expires_at": time.time() + (FWD_MARKET_TTL_SECONDS if is_market else FWD_ENTRY_TTL_HOURS * 3600),
            "last_check": 0.0, "last_price_check": 0.0,
        }
        state["pending_entries"][key] = rec
        save_state(state)

        order_id = None
        ladder = _leverage_ladder(leverage)
        for idx, lv in enumerate(ladder):
            if idx > 0:
                amount, notional, collateral, effective_risk = calculate_position_size(fsig, per_trade_budget, risk_cap, lv)
                if amount <= 0:
                    raise ValueError("حجم محاسبه‌شده صفر/منفی است")
            leverage = lv
            rec.update(leverage=str(lv), planned_amount=str(amount), planned_effective_risk=str(effective_risk),
                       planned_notional=str(notional), planned_collateral=str(collateral))
            save_state(state)
            try:
                resp = nx.open_position_limit(src_currency=src, dst_currency=dst, side=open_side,
                                              amount=fmt_amount(amount), leverage=fmt_leverage(lv), price=fmt_price(entry))
                order_id = order_id_from(resp)
                break
            except NobitexAPIError as oe:
                if is_transient_error(oe):
                    # Outcome unknown: look for the order before concluding anything (never double-place).
                    order_id = _find_entry_order(nx, src, dst, open_side, entry, amount)
                    if order_id is None:
                        state["pending_entries"].pop(key, None)
                        save_state(state)
                        return (f"⚠️ ارتباط با نوبیتکس هنگام ثبت سفارش ورود قطع شد و سفارشی پیدا نشد ({oe.code}). "
                                f"قبل از ارسال دوباره، «سفارش‌های باز» را در نوبیتکس چک کنید تا دوبار ثبت نشود."), menu
                    break
                txt = f"{oe.code} {oe.message}".lower()
                if ("leverage" in txt or "اهرم" in txt) and idx < len(ladder) - 1:
                    continue
                state["pending_entries"].pop(key, None)
                save_state(state)
                raise
        if order_id is None:
            state["pending_entries"].pop(key, None)
            save_state(state)
            return "❌ نوبیتکس شناسه‌ی سفارش ورود را برنگرداند؛ لطفاً سفارش‌های باز را در نوبیتکس بررسی کنید.", menu
        rec["order_id"] = int(order_id)
        rec["leverage"] = str(leverage)
        save_state(state)
    except (NobitexAPIError, ValueError, InvalidOperation) as e:
        log.exception("open_forwarded_signal failed for %s", key)
        state.get("pending_entries", {}).pop(key, None)
        save_state(state)
        _etxt = str(e).lower()
        if isinstance(e, NobitexAPIError) and any(w in _etxt for w in ("insufficient", "balance", "موجودی")):
            return ui.msg_low_balance(collateral_cap, margin_balance, f"باز کردن سیگنال {key}") + "\n⏭ سیگنال اجرا نشد.", menu
        return f"❌ اجرای سیگنال {key} شکست خورد: {e}", menu

    tp_lines = "\n".join(f"   T{n}  {num_s(p)}   ({int(weights[n] * 100)}٪ حجم)" for n, p in targets.items())
    resting = (side == "SHORT" and last < entry) or (side == "LONG" and last > entry)
    state_line = ("⏳ سفارش حدی روی بازار نشسته و منتظر رسیدن قیمت به ورود است."
                  if resting else "⚡ قیمت از ورود عبور کرده؛ سفارش حدی بلافاصله اجرا می‌شود.")
    text = "\n".join([
        f"📥 سیگنال فوروارد‌شده ثبت شد — {key}", ui.DIVIDER,
        f"{'🟢 Long' if side == 'LONG' else '🔴 Short'}   |   قیمت فعلی: {num_s(last)}",
        (f"⚡ ورود بازاری (حداکثر قیمت قابل‌قبول با لغزش {FWD_MARKET_SLIPPAGE * 100:.2f}٪): {num_s(entry)}" if is_market
         else f"🎯 ورود (Limit): {num_s(entry)}"), f"🛑 حد ضرر: {num_s(stop)}", "🎯 تارگت‌ها:", tp_lines,
        f"⚙️ اهرم: {leverage}x   |   ریسک برنامه‌ریزی‌شده: ≈{num_s(effective_risk)} USDT   |   وثیقه: ≈{num_s(collateral)} USDT",
        state_line,
        (f"⌛ اگر تا {FWD_MARKET_TTL_SECONDS // 60} دقیقه پر نشود، خودکار لغو می‌شود." if is_market
         else f"⌛ اگر تا {int(FWD_ENTRY_TTL_HOURS)} ساعت یا قبل از رسیدن به تارگت اول پر نشود، خودکار لغو می‌شود."),
        "بعد از پر شدن، حد ضرر و تارگت‌ها روی نوبیتکس ثبت و تأیید می‌شوند.",
    ] + (["⚠️ " + w for w in fs.warnings] if fs.warnings else []))
    return text, [[_fwd_btn_cancel(key)], [_btn_positions(), _btn_menu()]]


def command_id_for_signal() -> str:
    """Short unique suffix for a forwarded signal's id (time-based; the Telegram message id is not known here)."""
    return str(int(time.time()))


def _is_transient_exception(e: BaseException) -> bool:
    """True for temporary connectivity/rate-limit failures that fix themselves (never for logic errors)."""
    import requests as _rq
    if isinstance(e, TransientAPIError):
        return True
    if isinstance(e, NobitexAPIError):
        return is_transient_error(e)
    if isinstance(e, (_rq.exceptions.Timeout, _rq.exceptions.ConnectionError, _rq.exceptions.ChunkedEncodingError)):
        return True
    if isinstance(e, _rq.exceptions.HTTPError):
        code = getattr(getattr(e, "response", None), "status_code", None)
        return code in (403, 408, 425, 429, 500, 502, 503, 504)
    return isinstance(e, (TimeoutError, ConnectionError))


def _find_entry_order(nx: NobitexClient, src: str, dst: str, open_side: str, entry: Decimal, amount: Decimal) -> Optional[int]:
    """After an unknown-outcome error: is our limit entry order on the book? (3 tries.)"""
    for _ in range(3):
        try:
            orders = nx.list_orders(src_currency=src, dst_currency=dst, status="open", trade_type="margin", details=2).get("orders") or []
            for o in orders:
                try:
                    if (str(o.get("type", "")).lower() == open_side and D(o.get("price") or "0") == entry
                            and abs(D(o.get("amount") or "0") - amount) <= amount * Decimal("0.01")):
                        return int(o["id"])
                except (InvalidOperation, TypeError, KeyError, ValueError):
                    continue
            return None
        except NobitexAPIError:
            time.sleep(1.5)
    return None


def _adopt_filled_entry(nx: NobitexClient, state: Dict[str, Any], key: str, rec: dict) -> bool:
    """The limit entry (fully or partly) filled: find the new position and attach stop + targets
    through the shared finalize path. Returns True when the pending record is resolved."""
    src, dst, side = rec["src"], rec["dst"], rec["side"]
    wanted = "buy" if side == "LONG" else "sell"
    tracked = _tracked_position_ids(state)
    before = {int(x) for x in rec.get("before_ids", [])}
    entry = D(rec["entry"])
    position = None
    positions = nx.list_positions(status="active", src_currency=src, dst_currency=dst).get("positions", [])
    cands = []
    for p in positions:
        try:
            pid = int(p.get("id"))
        except (TypeError, ValueError):
            continue
        if pid in before or pid in tracked:
            continue
        if str(p.get("status", "")).lower() != "open" or not _side_ok(p.get("side"), side):
            continue
        try:
            ep = D(p.get("entryPrice") or "0")
            if ep > 0 and abs(ep - entry) / entry > Decimal("0.02"):
                continue          # a different position on the same market (e.g. opened by a channel signal)
        except (InvalidOperation, TypeError):
            pass
        cands.append(p)
    cands.sort(key=lambda p: p.get("openedAt") or p.get("createdAt") or "", reverse=True)
    if cands:
        position = cands[0]
    if position is None:
        rec.setdefault("filled_at", time.time())
        age = time.time() - float(rec["filled_at"])
        sent = int(rec.get("alerts_sent", 0))
        if age > (sent + 1) * 120:
            notify_admin(f"🚨 {key}: سفارش ورود پر شد ولی positionId هنوز پیدا نشد ({age / 60:.0f} دقیقه). "
                         f"حساب نوبیتکس را فوراً دستی بررسی کنید؛ ربات هم ادامه می‌دهد.", key=key, buttons=_ack_button(key))
            rec["alerts_sent"] = sent + 1
        return False
    sig = _fwd_sig_from_rec(rec)
    notify_admin(f"✅ {key}: سفارش ورود پر شد (positionId #{position.get('id')})؛ در حال ثبت حد ضرر و تارگت‌ها...", key=key)
    try:
        _finalize_opened_position(nx, state, key, sig, side, src, D(rec["risk_cap"]), D(rec["collateral_cap"]),
                                  D(rec["planned_amount"]), D(rec["planned_effective_risk"]), D(rec["planned_notional"]),
                                  D(rec["planned_collateral"]), position, D(rec.get("leverage", DEFAULT_LEVERAGE)))
    except SignalFinalized:
        pass                      # trade is tracked (protected, or flagged for repair + admin alerted)
    except Exception:
        # The position is in open_trades from the first step of _finalize_opened_position, so the repair loops own it now.
        log.exception("finalize of forwarded entry %s raised", key)
        if key not in state.get("open_trades", {}):
            raise
    return True


def resolve_fwd_retries(nx: NobitexClient, state: Dict[str, Any], gh: GithubClient) -> None:
    """Forwarded signals that could not even start because Nobitex was unreachable are retried here. Nothing was
    placed in that case, and every retry re-validates the live price, so a stale signal can never be executed."""
    q = state.get("fwd_retry") or {}
    if not q:
        return
    now = time.time()
    for key, rec in list(q.items()):
        if now > float(rec.get("created_at", now)) + FWD_RETRY_WINDOW_SECONDS:
            q.pop(key, None)
            save_state(state)
            notify_admin(f"⌛ سیگنال {key}: {FWD_RETRY_WINDOW_SECONDS // 60} دقیقه تلاش شد ولی اتصال به نوبیتکس برقرار نشد؛ "
                         f"هیچ سفارشی ثبت نشد. اتصال را درست کنید و سیگنال را دوباره فوروارد کنید.", key=key, buttons=[[_btn_menu()]])
            continue
        if now < float(rec.get("next_try", 0)):
            continue
        rec["tries"] = int(rec.get("tries", 0)) + 1
        rec["next_try"] = now + FWD_RETRY_GAP_SECONDS
        try:
            fs = _fwd_signal_from_dict(rec["signal"])
            setattr(fs, "_from_retry", True)
            reply = open_forwarded_signal(nx, state, fs, gh, rec.get("signal_id") or f"FWD-{int(now)}", from_retry=True)
        except FwdRetryLater as e:
            rec["last_error"] = str(e)[:200]
            save_state(state)
            continue
        except Exception as e:
            log.exception("forwarded-signal retry for %s failed", key)
            q.pop(key, None)
            save_state(state)
            notify_admin(f"❌ سیگنال {key}: در تلاش مجدد خطا رخ داد و اجرا نشد: {e}", key=key, buttons=[[_btn_menu()]])
            continue
        q.pop(key, None)
        save_state(state)
        text, btns = reply if isinstance(reply, tuple) else (reply, [[_btn_positions(), _btn_menu()]])
        notify_admin("🔁 اتصال برگشت؛ سیگنال نگه‌داشته‌شده بررسی شد:\n" + text, key=key, buttons=btns)


def _drop_pending_entry(state: Dict[str, Any], key: str) -> None:
    state.get("pending_entries", {}).pop(key, None)
    save_state(state)


def _cancel_entry_and_settle(nx: NobitexClient, state: Dict[str, Any], key: str, rec: dict, why: str) -> bool:
    """Cancel the resting entry order. If part of it already filled, that part is adopted and protected;
    otherwise the pending record is removed. Returns True when resolved."""
    oid = rec.get("order_id")
    if oid:
        _cancel_order_quiet(nx, oid, "forwarded-entry")
        try:
            order = nx.get_order_status(int(oid)).get("order") or {}
        except NobitexAPIError as e:
            if is_transient_error(e):
                return False       # unknown: try again next loop, never guess
            order = {}
        matched = D(order.get("matchedAmount") or "0")
        status = str(order.get("status", "")).lower()
        if status in ("active", "inactive", "new", "partial", "partiallyfilled", "open") and matched <= 0:
            return False           # cancel not confirmed yet; retry
        if matched > 0:
            notify_admin(f"ℹ️ {key}: {why} — بخشی از سفارش ({num_s(matched)}) قبلاً پر شده بود؛ همان بخش محافظت می‌شود.", key=key)
            return _adopt_filled_entry(nx, state, key, rec) and (_drop_pending_entry(state, key) or True)
    _drop_pending_entry(state, key)
    notify_admin(f"🗑 سفارش ورود {key} لغو شد: {why}", key=key, buttons=[[_btn_positions(), _btn_menu()]])
    return True


def resolve_pending_entries(nx: NobitexClient, state: Dict[str, Any]) -> None:
    """Every loop: follow the resting limit entries of forwarded signals. Filled -> protect; expired,
    invalidated (price reached target 1 without filling) or cancelled -> clean up. Exchange hiccups
    never cause a decision - the check is simply repeated."""
    pend = state.get("pending_entries") or {}
    if not pend:
        return
    now = time.time()
    for key, rec in list(pend.items()):
        if not rec.get("order_id"):
            # Still being placed by open_forwarded_signal - unless the process died in between. After a minute,
            # look for the order on the book: found -> track it; not found -> nothing was placed, drop the record.
            if now - float(rec.get("created_at", now)) > 60:
                try:
                    oid = _find_entry_order(nx, rec["src"], rec["dst"], "buy" if rec["side"] == "LONG" else "sell",
                                            D(rec["entry"]), D(rec["planned_amount"]))
                except Exception:
                    oid = None
                if oid:
                    rec["order_id"] = int(oid)
                    save_state(state)
                    notify_admin(f"♻️ {key}: سفارش ورودِ ثبت‌شده پیدا و دوباره زیر نظر گرفته شد.", key=key)
                else:
                    _drop_pending_entry(state, key)
                    notify_admin(f"ℹ️ {key}: ثبت سفارش ورود ناتمام ماند و سفارشی روی نوبیتکس پیدا نشد؛ از لیست انتظار حذف شد. اگر هنوز می‌خواهید، سیگنال را دوباره فوروارد کنید.", key=key)
            continue
        if now - float(rec.get("last_check", 0)) < FWD_CHECK_SECONDS:
            continue
        rec["last_check"] = now
        try:
            order = nx.get_order_status(int(rec["order_id"])).get("order") or {}
            status = str(order.get("status", "")).lower()
            matched = D(order.get("matchedAmount") or "0")
            planned = D(rec["planned_amount"])
            if status == "done" or (planned > 0 and matched >= planned * Decimal("0.999")):
                if _adopt_filled_entry(nx, state, key, rec):
                    _drop_pending_entry(state, key)
                continue
            if status in ("canceled", "cancelled", "rejected", "expired"):
                if matched > 0:
                    if _adopt_filled_entry(nx, state, key, rec):
                        _drop_pending_entry(state, key)
                else:
                    _drop_pending_entry(state, key)
                    notify_admin(f"🗑 سفارش ورود {key} روی نوبیتکس لغو شد (بدون پر شدن)؛ از لیست انتظار حذف شد.", key=key)
                continue
            # still resting (or partly filled)
            if now > float(rec.get("expires_at", now + 1)):
                _cancel_entry_and_settle(nx, state, key, rec, (
                    "ورود بازاری ظرف ۲ دقیقه پر نشد (قیمت از سقف لغزش دور شد)" if rec.get("order_type") == "market"
                    else f"{int(FWD_ENTRY_TTL_HOURS)} ساعت گذشت و قیمت به ورود نرسید"))
                continue
            if now - float(rec.get("last_price_check", 0)) >= FWD_PRICE_CHECK_SECONDS:
                rec["last_price_check"] = now
                last = nx.get_last_trade_price(f"{rec['src'].upper()}{rec['dst'].upper()}")
                tp1 = D(rec["targets"]["1"])
                if (rec["side"] == "SHORT" and last <= tp1) or (rec["side"] == "LONG" and last >= tp1):
                    _cancel_entry_and_settle(nx, state, key, rec, f"قیمت ({num_s(last)}) بدون پر شدن به تارگت اول رسید؛ ستاپ باطل شد")
                    continue
            save_state(state)
        except NobitexAPIError as e:
            if is_transient_error(e):
                log.warning("pending entry %s: exchange busy (%s); will retry", key, e)
                continue
            log.exception("pending entry %s check failed", key)
            if now - float(rec.get("err_alert", 0)) > 600:
                rec["err_alert"] = now
                notify_admin(f"⚠️ بررسی سفارش ورود {key} ناموفق بود: {e.code} {e.message}", key=key,
                             buttons=[[_fwd_btn_cancel(key)]])
        except TransientAPIError as e:
            log.warning("pending entry %s: %s", key, e)
        except Exception:
            log.exception("pending entry %s failed", key)


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
            if is_transient_error(e):
                # Do NOT guess: treating "unknown" as "not filled" would cancel the TP and
                # market-sell a slice that may already have been sold.
                raise TransientAPIError(f"T{target_num} status: {e.code}: {e.message}") from e

    if t.get("merged"):
        # Nothing was ever placed for this slice (it lives under the runner stop), so there is nothing to
        # close or cancel: record that price reached it so the strategy stop moves, and tell the admin.
        t["hit"] = True
        notify_admin(f"🎯 {key}: T{target_num} رسید (قیمت {t.get('tp_price')}). سهم این تارگت کوچک بود و در رانر ادغام شده؛ "
                     f"چیزی بسته نشد و " + ("حد ضرر ثابت می‌ماند." if trade.get("fixed_stop") else "حد ضرر طبق استراتژی جابه‌جا می‌شود."), key=key)
        return

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
                notify_admin(f"⚠️ {key}: T{target_num} روی قیمت حدی کامل Fill نشد؛ بخش باقیمانده ({fmt_amount(remaining_target)}) با Market بسته شد.", key=key)
            except NobitexAPIError as e:
                notify_admin(f"🚨 {key}: T{target_num} hit شد ولی بستن fallback ناموفق بود: {e.code} - {e.message}", key=key)
                return

    t["hit"] = True
    trade["closed_pct"] = str(D(trade.get("closed_pct", "0")) + D(t["pct"]))
    try:
        _slice = _slice_pnl(trade.get("side"), D(trade.get("entry_actual", "0")), D(t["tp_price"]),
                            D(trade.get("initial_amount", "0")) * D(t["pct"]))
    except (InvalidOperation, TypeError):
        _slice = None
    _pos_now = _position_status_safe(nx, trade)
    _liq = _liq_info(trade, _pos_now)
    _tb = [_liq_buttons(key, _liq)] if _liq and _liq.get("close") else []
    notify_admin(
        ui.card_target_hit(key, trade.get("side"), target_num, t["pct"], t["tp_price"],
                           _stop_vs_liquidation_note(trade, _pos_now),
                           slice_pnl=_slice, realized_total=_realized_so_far(trade)),
        key=key, buttons=(_tb + [[_btn_positions(), _btn_menu()]]),
    )


def _find_open_trade_for_event(state: Dict[str, Any], ev: ParsedEvent) -> Optional[str]:
    """Fallback when symbol+timeframe does not name an open trade. Manual / admin-forwarded signals are posted
    without a timeframe, and the channel's own signal id (hashtag) always identifies the exact trade."""
    sid = str(getattr(ev, "signal_id", "") or "").upper()
    opens = state.get("open_trades", {})
    if sid:
        hits = [k for k, t in opens.items() if str(t.get("signal_id") or "").upper() == sid]
        if len(hits) == 1:
            return hits[0]
    if str(ev.timeframe).upper() == "MANUAL":
        same = [k for k, t in opens.items()
                if str(t.get("symbol", "")).upper() == str(ev.symbol).upper() and not k.endswith("_" + FWD_TIMEFRAME)]
        if len(same) == 1:
            return same[0]
    return None


def handle_event(nx: NobitexClient, state: Dict[str, Any], ev: ParsedEvent) -> None:
    key = trade_key(ev.symbol, ev.timeframe)
    trade = state["open_trades"].get(key)
    if trade is None:
        _alt_key = _find_open_trade_for_event(state, ev)
        if _alt_key:
            key, trade = _alt_key, state["open_trades"][_alt_key]

    if trade is None and _active_user_chat_id:
        log.info("subscriber %s: event %s for %s ignored (no such open trade on this account)", _active_user_chat_id, ev.kind, key)
        return
    if trade is None:
        recent = [h for h in (state.get("trade_history") or [])[-10:]
                  if h.get("key") == key and time.time() - float(h.get("closed_at") or 0) < 6 * 3600]
        if recent:
            log.info("event %s for %s ignored: that trade already closed (%s)", ev.kind, key, recent[-1].get("reason"))
            return
        notify_admin(f"⚠️ رویداد {ev.kind} برای {key} رسید ولی معامله‌ای در state باز نیست. "
                     f"اگر روی نوبیتکس پوزیشن باز دارید /recover را بزنید.", key=key,
                     buttons=[[{"text": "♻️ بازیابی پوزیشن‌های بی‌صاحب", "callback_data": "cmd:/recover"}]])
        return

    if key in state.get("needs_flatten", {}):
        log.info("event %s for %s ignored: the trade is already being closed", ev.kind, key)
        return

    ev_sid = getattr(ev, "signal_id", None)
    tr_sid = str(trade.get("signal_id") or "")
    if ev_sid and tr_sid.upper().startswith(("TC-", "ALT-")) and ev_sid.upper() != tr_sid.upper():
        notify_admin(f"⚠️ رویداد {ev.kind} با Signal ID {ev_sid} رسید ولی معامله‌ی باز {key} مربوط به {tr_sid} است؛ "
                     f"برای جلوگیری از اشتباه، اقدامی انجام نشد.", key=key)
        return

    if ev.kind == "target_hit":
        _lvl_hit_before = bool((trade["targets"].get(str(int(ev.level))) or {}).get("hit"))
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

        # The exchange-truth sync may already have applied this hit (and moved the
        # stop) before the channel message arrived - don't cancel/recreate again.
        if _lvl_hit_before:
            if int(ev.level) < 4 and not _stop_is_tighter(trade["side"], new_stop, D(trade["stop_price"])):
                save_state(state)
                return
            if int(ev.level) == 4 and trade.get("trailing_active"):
                save_state(state)
                return

        try:
            if int(ev.level) < 4:
                _rebuild_protection(nx, trade, new_stop, state)
            else:
                # Read the live position BEFORE touching anything (raises
                # TransientAPIError on rate limits, so the event is retried).
                status = _position_status(nx, trade)
                # T4 implies T1/T2/T3/T4 have all been reached even if one or
                # more channel result messages were missed. Mark them
                # accordingly, then leave only the live runner liability on
                # the exchange.
                for n, t in trade["targets"].items():
                    t["hit"] = True
                trade["closed_pct"] = str(max(Decimal("0"), Decimal("1") - _runner_share(trade)))
                # Cancel every fixed-target OCO; their actual fills, if any,
                # are already reflected by the live position liability below.
                for t in trade["targets"].values():
                    _cancel_oco(nx, t)
                trade["trailing_active"] = True
                _cancel_order_quiet(nx, trade.get("runner", {}).get("order_id"), "runner-before-trailing")
                if _pos_open(status):
                    liability = D(status.get("liability", "0"))
                    market = symbol_to_currencies(trade["symbol"])
                    try:
                        last = nx.get_last_trade_price(f"{market[0].upper()}{market[1].upper()}")
                    except Exception:
                        last = D(trade["targets"]["4"]["tp_price"])
                    r = D(trade["risk_unit"])
                    entry = D(trade["entry_actual"])
                    # The signal bot starts the trailing stop from the real peak, which is at least the Target 4 price
                    # (that is what "T4 hit" means). Using only the current price here could start the runner's stop
                    # lower than the bot's when the channel message arrives after a small pullback.
                    try:
                        tp4 = D(trade["targets"]["4"]["tp_price"])
                    except Exception:
                        tp4 = None
                    if trade["side"] == "LONG":
                        peak = max(entry, last, tp4 if tp4 is not None else last)
                        runner_stop = max(new_stop, peak - TRAILING_R_MULT * r)
                        if runner_stop >= last:                       # never a stop above the market (exchange would refuse it)
                            peak = max(entry, last)
                            runner_stop = max(new_stop, peak - TRAILING_R_MULT * r)
                    else:
                        peak = min(entry, last, tp4 if tp4 is not None else last)
                        runner_stop = min(new_stop, peak + TRAILING_R_MULT * r)
                        if runner_stop <= last:
                            peak = min(entry, last)
                            runner_stop = min(new_stop, peak + TRAILING_R_MULT * r)
                    trade["peak"] = str(peak)
                    trade["runner"] = _create_runner_stop(nx, trade, liability, runner_stop)
                    trade["stop_price"] = str(runner_stop)
                else:
                    trade["stop_price"] = str(new_stop)
            # Every not-yet-hit target's stop now protects the FULL remaining
            # live liability (rebuilt from a fresh exchange query inside
            # _rebuild_protection/_create_runner_stop above, not the original
            # per-target slice), exactly matching the trade plan: a target
            # hit moves the stop for everything still open, never just part
            # of it. (Not re-running _verify_trade_protection here: it
            # expects a fresh 4-target setup and would misfire on any
            # already-hit target's now-cancelled SL leg.)
        except TransientAPIError:
            raise  # nothing was changed on the exchange; poll_once retries this event
        except Exception as rebuild_error:
            # Same doctrine as everywhere else: never close the position -
            # flag it for admin attention and keep retrying to complete
            # protection automatically in the background.
            _flag_protection_issue(nx, state, key, trade, f"post-target-hit protection rebuild failed: {rebuild_error}")
            save_state(state)
            raise SignalFinalized(str(rebuild_error)) from rebuild_error

        save_state(state)
        return

    if ev.kind == "breakeven":
        _request_close_confirmation(state, key, "breakeven", D(trade["entry_actual"]), nx=nx)
        return

    if ev.kind in ("sl_after_t2", "sl_after_t3"):
        # These messages say the remaining position should be settled at
        # T1/T2 - record which targets that implies as hit (bookkeeping
        # only, touches nothing on the exchange), then ask before actually
        # closing anything.
        # "STOP AFTER TARGET 2" => T1,T2 were reached; "STOP AFTER TARGET 3" => T1,T2,T3 were reached (T4 was NOT:
        # the old list here said "4", which recorded a target the price never touched - inflating the estimated R of
        # the trade and making history show "T4 hit" for trades that stopped after T3).
        implied_hits = ["1", "2"] if ev.kind == "sl_after_t2" else ["1", "2", "3"]
        for h in implied_hits:
            if h in trade.get("targets", {}):
                trade["targets"][h]["hit"] = True
        save_state(state)
        close_price = D(trade["targets"]["1"]["tp_price"] if ev.kind == "sl_after_t2" else trade["targets"]["2"]["tp_price"])
        _request_close_confirmation(state, key, ev.kind, close_price, nx=nx)
        return

    if ev.kind in ("stop", "runner_closed", "forced_close"):
        _request_close_confirmation(state, key, ev.kind, None, nx=nx)
        return


_CLOSE_EVENT_LABELS_FA = {
    "breakeven": "رسیدن به Breakeven (پیشنهاد کانال: بستن باقیمانده روی نقطه‌ی ورود)",
    "sl_after_t2": "SL بعد از T2 (پیشنهاد کانال: بستن باقیمانده روی تارگت 1)",
    "sl_after_t3": "SL بعد از T3 (پیشنهاد کانال: بستن باقیمانده روی تارگت 2)",
    "stop": "خوردن حد ضرر نهایی طبق کانال",
    "runner_closed": "بسته‌شدن Runner طبق کانال",
    "forced_close": "بستن اجباری طبق کانال سیگنال",
}


def _auto_exit_enabled() -> bool:
    """Should channel exit events (Breakeven, SL after T2/T3, final stop,
    runner closed, forced close) be carried out automatically? Per
    subscriber: their own /myautoexit setting. For the admin's own account:
    control.json "auto_exit" (default ON, /autoexit on|off). When OFF, the
    old ask-first flow (confirm/dismiss buttons) is used instead."""
    if _active_user_chat_id:
        return bool(_active_user_auto_exit)
    now = time.time()
    if now - _admin_auto_exit_cache["ts"] > 60 and _gh_main is not None:
        try:
            _admin_auto_exit_cache["value"] = bool(load_control(_gh_main).get("auto_exit", True))
            _admin_auto_exit_cache["ts"] = now
        except Exception:
            pass
    return bool(_admin_auto_exit_cache["value"])


def _execute_channel_exit(nx: NobitexClient, state: Dict[str, Any], key: str, kind: str, price: Optional[Decimal]) -> None:
    """Carry out a channel exit exactly like the admin's /confirmclose would,
    but at most once: the pending entry is removed BEFORE acting, the closers
    verify the live position first (already closed -> just records it), and a
    closed trade disappears from state so a repeated event finds nothing."""
    state.get("pending_close_confirm", {}).pop(key, None)
    if kind in ("breakeven", "sl_after_t2", "sl_after_t3") and price is not None:
        _close_remaining_at_price(nx, state, key, kind, price)
    else:
        _close_remaining_market(nx, state, key, kind)


def _request_close_confirmation(state: Dict[str, Any], key: str, kind: str, price: Optional[Decimal],
                                 nx: Optional[NobitexClient] = None) -> None:
    """A channel event says this trade should be closed (stop/breakeven/
    forced-close/etc.), but per explicit instruction Executor never closes an
    open trade on its own initiative, under any circumstance, without the
    admin's explicit confirmation. This only records the request and asks -
    it never touches the exchange. The trade's own TP/SL orders already
    resting on Nobitex are completely untouched while this is pending, so
    the position stays exactly as protected as it already was.

    With auto-exit ON (default; /autoexit, /myautoexit) the exit is instead
    carried out automatically right here (see _execute_channel_exit).
    """
    if nx is not None and _auto_exit_enabled():
        trade = state.get("open_trades", {}).get(key)
        if trade:
            _position_status(nx, trade)     # transient error -> raised, event is retried, nothing changed yet
            _execute_channel_exit(nx, state, key, kind, price)
        return
    if _active_user_chat_id:
        # A subscriber's account never waits on an admin-style confirmation:
        # nothing is closed automatically (their stop/target orders keep
        # protecting the position exactly as before) and they are simply
        # told, with a one-tap manual close if they want it.
        label = _CLOSE_EVENT_LABELS_FA.get(kind, kind)
        notify_admin(
            f"📡 {key}: کانال سیگنال یک رویداد فرستاد:\n{label}\n\n"
            f"حد ضرر/تارگت‌های شما روی نوبیتکس دست‌نخورده و فعال هستند و چیزی به‌صورت خودکار بسته نشد. "
            f"اگر خودتان می‌خواهید همین الان ببندید:",
            key=key, buttons=[[{"text": f"❌ بستن {key}", "callback_data": f"cmd:/myclose {key}"}]],
        )
        return
    rec = {"kind": kind, "price": str(price) if price is not None else "",
           "requested_at": time.time(), "alerts_sent": 0}
    state.setdefault("pending_close_confirm", {})[key] = rec
    save_state(state)
    label = _CLOSE_EVENT_LABELS_FA.get(kind, kind)
    notify_admin(
        f"📡 {key}: کانال سیگنال یک رویداد فرستاد:\n{label}\n\n"
        f"⚠️ طبق تنظیم شما، Executor بدون تأیید شما هیچ معامله‌ای را نمی‌بندد.\n"
        f"حد ضرر/تارگت‌های ثبت‌شده روی نوبیتکس دست‌نخورده و فعال باقی مانده‌اند؛ این فقط درباره‌ی "
        f"بستن دستی طبق همین پیام کانال است — تصمیم با شماست:",
        key=key, buttons=[
            [{"text": "✅ بله، طبق کانال ببند", "callback_data": _cb("confirmclose", key)}],
            [{"text": "❌ نه، نگه دار", "callback_data": _cb("dismissclose", key)}],
            [{"text": "📈 معاملات باز", "callback_data": "cmd:/positions"}, {"text": "📋 منو", "callback_data": "menu"}],
        ],
    )


def resolve_pending_close_confirm(state: Dict[str, Any], nx: Optional[NobitexClient] = None) -> None:
    """Periodically remind the admin about a still-undecided channel close
    request, so it can't silently get lost. Never acts on its own - only
    re-sends the same confirm/dismiss buttons."""
    pending = state.get("pending_close_confirm", {})
    if not pending:
        return
    if nx is not None and _auto_exit_enabled():
        for key, rec in list(pending.items()):
            trade = state.get("open_trades", {}).get(key)
            if not trade:
                pending.pop(key, None)
                continue
            try:
                _position_status(nx, trade)
                pr = rec.get("price")
                _execute_channel_exit(nx, state, key, rec.get("kind"), D(pr) if pr not in (None, "", "None") else None)
            except TransientAPIError:
                continue        # exchange busy: try again next tick
            except Exception:
                log.exception("auto exit of %s failed", key)
        save_state(state)
        return
    for key, rec in list(pending.items()):
        if key not in state.get("open_trades", {}):
            pending.pop(key, None)
            continue
        age = time.time() - float(rec.get("requested_at", time.time()))
        sent = int(rec.get("alerts_sent", 0))
        if age > (sent + 1) * 600:  # every ~10 minutes of no decision
            label = _CLOSE_EVENT_LABELS_FA.get(rec.get("kind"), rec.get("kind"))
            notify_admin(
                f"📡 یادآوری: هنوز منتظر تصمیم شما درباره‌ی {key} هستیم\n"
                f"رویداد کانال: {label}\n"
                f"({age/60:.0f} دقیقه از دریافت این پیام گذشته)",
                key=key, buttons=[
                    [{"text": "✅ بله، طبق کانال ببند", "callback_data": _cb("confirmclose", key)}],
                    [{"text": "❌ نه، نگه دار", "callback_data": _cb("dismissclose", key)}],
                    [{"text": "📋 منو", "callback_data": "menu"}],
                ],
            )
            rec["alerts_sent"] = sent + 1
    state["pending_close_confirm"] = pending
    save_state(state)



def _signup_btn() -> dict:
    """URL button that opens the Nobitex sign-up page (with the service's referral code) for people without an account."""
    return {"text": "🆕 ثبت‌نام در نوبیتکس", "url": ui.NOBITEX_SIGNUP_URL}


def _is_signup_btn(b: dict) -> bool:
    return isinstance(b, dict) and b.get("url") == ui.NOBITEX_SIGNUP_URL and not b.get("callback_data")


def _user_btn_ok(cb: str) -> bool:
    """Callback data a SUBSCRIBER's chat is allowed to carry (the bridge enforces the
    same rule again; the executor re-checks every command it receives)."""
    cb = str(cb or "")
    if cb.startswith("ask:"):
        return cb.split(":")[1] in USER_ASK_PROMPTS
    if cb.startswith("cmd:"):
        parts = cb[4:].strip().split()
        return bool(parts) and parts[0].lower() in SELF_SERVICE_COMMANDS
    return False


def _cb(name: str, *args) -> str:
    """callback_data for an action button, correct for WHO is looking:
    the admin on his own account (/close KEY), a subscriber (/myclose KEY), or the
    admin looking at a subscriber's account (/userclose UID KEY)."""
    a = " ".join(str(x) for x in args if x not in (None, ""))
    if _ui_uid is None:
        cmd = f"/{name}"
    elif _ui_admin_view:
        cmd = f"/user{name} {_ui_uid}"
    else:
        cmd = f"/my{name}"
    return f"cmd:{cmd} {a}".strip()


def _ask_cb(name: str, *args) -> str:
    """callback_data that makes the bridge ask the person for a value (no typing of commands)."""
    a = ":".join(str(x) for x in args if x not in (None, ""))
    if _ui_uid is not None and _ui_admin_view:
        return f"ask:u{name}:{_ui_uid}" + (f":{a}" if a else "")
    return f"ask:{name}" + (f":{a}" if a else "")


def _btn_close(key: str) -> dict:
    return {"text": f"❌ بستن {key}", "callback_data": _cb("close", key)}


def _btn_positions() -> dict:
    if _ui_uid is None:
        return {"text": "📈 معاملات باز", "callback_data": "cmd:/positions"}
    return {"text": "📈 معاملات باز", "callback_data": f"cmd:/usertrades {_ui_uid}" if _ui_admin_view else "cmd:/mytrades"}


def _btn_history() -> dict:
    if _ui_uid is None:
        return {"text": "📜 تاریخچه", "callback_data": "cmd:/history"}
    return {"text": "📜 تاریخچه", "callback_data": f"cmd:/userhistory {_ui_uid}" if _ui_admin_view else "cmd:/myhistory"}


def _btn_pnl() -> dict:
    if _ui_uid is None:
        return {"text": "📊 سود و زیان", "callback_data": "cmd:/pnl"}
    return {"text": "📊 سود و زیان", "callback_data": f"cmd:/userpnl {_ui_uid}" if _ui_admin_view else "cmd:/mypnl"}


def _btn_menu() -> dict:
    if _ui_uid is None:
        return {"text": "📋 منو", "callback_data": "menu"}
    if _ui_admin_view:
        return {"text": "👤 پنل این کاربر", "callback_data": f"cmd:/user {_ui_uid}"}
    return {"text": "📋 منوی من", "callback_data": "cmd:/menu"}


# ---------------------------------------------------------------------------
# Live prices (shared cache, one bulk call for all open markets)
# ---------------------------------------------------------------------------
def _trade_market(trade: dict) -> str:
    src, dst = symbol_to_currencies(trade["symbol"])
    return f"{src.upper()}{dst.upper()}"


def _get_prices(nx: NobitexClient, symbols) -> Dict[str, Decimal]:
    """Latest trade price per market (e.g. "BTCUSDT"). Uses a short cache and ONE
    /market/stats call for every market that needs a refresh; only markets that
    call did not return fall back to the per-market orderbook endpoint."""
    now = time.time()
    out: Dict[str, Decimal] = {}
    need = []
    for sym in symbols:
        c = _price_cache.get(sym)
        if c and now - c[0] < PRICE_CACHE_SECONDS:
            out[sym] = c[1]
        else:
            need.append(sym)
    if not need:
        return out
    fetched: Dict[str, Decimal] = {}
    by_dst: Dict[str, list] = {}
    for sym in need:
        for suffix in ("USDT", "IRT", "USD"):
            if sym.endswith(suffix) and len(sym) > len(suffix):
                by_dst.setdefault(suffix.lower(), []).append(sym[:-len(suffix)].lower())
                break
    for dst, srcs in by_dst.items():
        try:
            fetched.update(nx.get_market_latest_prices(srcs, dst))
            _note_network_ok()
        except Exception as e:
            log.warning("bulk price fetch failed (%s); falling back per market", e)
    fallback_budget = 6
    for sym in need:
        p = fetched.get(sym)
        if p is None and fallback_budget > 0:
            fallback_budget -= 1
            try:
                p = nx.get_last_trade_price(sym)
            except Exception as e:
                log.warning("price for %s unavailable: %s", sym, e)
                p = None
        if p is not None and p > 0:
            _price_cache[sym] = (now, p)
            out[sym] = p
    return out


def _is_dust(liability: Decimal, price: Optional[Decimal] = None) -> bool:
    if liability <= 0:
        return True
    if liability < Decimal("0.000000001"):
        return True
    if price is not None and price > 0 and liability * price < DUST_NOTIONAL_USDT:
        return True
    return False


# ---------------------------------------------------------------------------
# Closing: ONE verified path that guarantees the WHOLE trade is closed
# ---------------------------------------------------------------------------
def _hist_notify_buttons() -> list:
    return [[_btn_history(), _btn_positions()], [_btn_menu()]]


def _finalize_closed(state: Dict[str, Any], key: str, trade: dict, reason: str, note: str = "",
                     exit_price: Optional[Decimal] = None, notify: bool = True) -> None:
    """Trade is definitively closed: record history (with real fills), forget it,
    clear every queue that still mentioned it, and send the closing report."""
    _record_trade_history(state, key, trade, reason, note, exit_price=exit_price)
    state.get("open_trades", {}).pop(key, None)
    for q in ("needs_protection", "needs_flatten", "pending_close_confirm", "pending_protection"):
        state.get(q, {}).pop(key, None)
    save_state(state)
    if notify:
        _notify_closed(state, key, reason, note)


def _notify_closed(state: Dict[str, Any], key: str, reason: str, note: str = "") -> None:
    """One consistent 'trade closed' report built from the history entry that
    _record_trade_history just appended: every fill on its own line with the
    running total, the trade result and the account's all-time total."""
    hist = state.get("trade_history") or []
    h = hist[-1] if hist and hist[-1].get("key") == key else None
    if not h:
        notify_admin(f"🏁 {key}: معامله بسته شد — {ui.REASON_FA.get(reason, reason)}", key=key,
                     buttons=_hist_notify_buttons())
        return
    notify_admin(ui.card_closed_entry(h, cum_total=_cumulative_total(state), note=note), key=key,
                 buttons=_hist_notify_buttons())


def _cancel_all_trade_orders(nx: NobitexClient, trade: dict) -> None:
    for t in (trade.get("targets") or {}).values():
        if not t.get("hit"):
            _cancel_oco(nx, t)
    runner = trade.get("runner") or {}
    if runner.get("order_id"):
        _cancel_order_quiet(nx, runner.get("order_id"), "flatten-runner")
    for oid in _find_position_orders(nx, trade):
        _cancel_order_quiet(nx, oid, "flatten-orphan-order")


def _flatten_trade(nx: NobitexClient, state: Dict[str, Any], key: str, reason: str, note: str = "") -> bool:
    """Close EVERYTHING that is left of this trade, and prove it.

    1. read the live position (a rate-limit / network error changes nothing);
    2. cancel every resting order of the trade (they reserve liability);
    3. market-close the ENTIRE live liability, re-read the position, and repeat
       until the exchange says it is closed (or only un-closable dust remains);
    4. only then record history, forget the trade and send the report.

    If it cannot be finished the trade stays in open_trades + needs_flatten and
    resolve_needs_flatten() keeps retrying every FLATTEN_RETRY_SECONDS - the
    position is never silently forgotten. Returns True when fully closed."""
    trade = state.get("open_trades", {}).get(key)
    if not trade:
        state.get("needs_flatten", {}).pop(key, None)
        return True
    try:
        status = _position_status(nx, trade)
    except TransientAPIError as e:
        rec = state.setdefault("needs_flatten", {}).setdefault(key, {"first_seen": time.time(), "alerts_sent": 0, "fails": 0})
        rec.update(reason=reason, note=note, last_attempt=time.time(), error=str(e))
        save_state(state)
        log.warning("flatten %s postponed (exchange busy): %s", key, e)
        return False
    if status is None or str(status.get("status", "")).lower() in TERMINAL_POSITION_STATUSES:
        _finalize_closed(state, key, trade, reason if reason != "stop_guard" else "closed_on_exchange",
                         note or "Nobitex reports the position closed")
        return True
    if not _pos_open(status):
        notify_admin(f"⚠️ {key}: وضعیت پوزیشن نامشخص است ({status.get('status')!r}); چیزی بسته/لغو نشد.", key=key)
        return False

    rec = state.setdefault("needs_flatten", {}).setdefault(key, {"first_seen": time.time(), "alerts_sent": 0, "fails": 0})
    rec.update(reason=reason, note=note, last_attempt=time.time())
    save_state(state)

    _cancel_all_trade_orders(nx, trade)

    last_err: Optional[Exception] = None
    closed = False
    dust_left: Optional[Decimal] = None
    price_now: Optional[Decimal] = None
    try:
        price_now = _get_prices(nx, {_trade_market(trade)}).get(_trade_market(trade))
    except Exception:
        price_now = None
    for attempt in range(FLATTEN_ATTEMPTS):
        try:
            st = _position_status(nx, trade)
        except TransientAPIError as e:
            last_err = e
            time.sleep(1.5)
            continue
        if st is None or not _pos_open(st):
            closed = True
            break
        liability = D(st.get("liability", "0") or "0")
        if _is_dust(liability, price_now):
            closed = True
            dust_left = liability if liability > 0 else None
            break
        try:
            resp = nx.close_position_market(int(trade["position_id"]), amount=fmt_amount(liability))
            oid = order_id_from(resp) if isinstance(resp, dict) else None
            if oid:
                trade.setdefault("close_order_ids", []).append(oid)
                _log_order(trade, oid, "MARKET_CLOSE", liability)
                save_state(state)
        except NobitexAPIError as e:
            last_err = e
            txt = f"{e.code} {e.message}".lower()
            if is_transient_error(e):
                time.sleep(2.0)
                continue
            if "exceedliability" in txt.replace("_", "").replace(" ", ""):
                _cancel_all_trade_orders(nx, trade)
                time.sleep(1.0)
                continue
            if "smallorder" in txt.replace("_", "").replace(" ", ""):
                # Below the exchange's minimum order size: retrying cannot help.
                dust_left = liability
                break
            time.sleep(1.0)
            continue
        time.sleep(FLATTEN_VERIFY_DELAY_SECONDS)

    if not closed:
        # One last look - the final market order may have executed just now.
        try:
            st = _position_status(nx, trade)
            if st is None or not _pos_open(st) or _is_dust(D(st.get("liability", "0") or "0"), price_now):
                closed = True
        except TransientAPIError:
            pass

    if closed or (dust_left is not None and (price_now is None or dust_left * price_now < DUST_AUTO_ACCEPT_USDT)):
        extra = ""
        if dust_left is not None and dust_left > 0:
            extra = (f"\n⚠️ باقیمانده‌ی بسیار کوچک ({fmt_amount(dust_left)}) به‌خاطر حداقل سایز سفارش نوبیتکس قابل بستن نبود؛ "
                     f"اگر در اپ نوبیتکس هنوز نمایش داده شد، دستی ببندید.")
        exit_ref = None
        for oid in reversed(trade.get("close_order_ids") or []):
            try:
                o = nx.get_order_status(int(oid)).get("order") or {}
                ap = D(o.get("averagePrice") or "0")
                if ap > 0:
                    exit_ref = ap
                    break
            except Exception:
                continue
        if exit_ref is None:
            exit_ref = price_now
        _finalize_closed(state, key, trade, reason, (note or "closed via market order") + extra, exit_price=exit_ref)
        return True

    # Not closed. The resting orders were cancelled to free the liability, so re-arm ONE protective
    # stop-market on whatever is still open (best effort) - the position must never sit unprotected
    # while the retries continue. The next attempt cancels it again before closing.
    try:
        st_left = _position_status(nx, trade)
        if _pos_open(st_left):
            left = D(st_left.get("liability", "0") or "0")
            if not _is_dust(left, price_now):
                stop_ref = D(trade.get("stop_price") or trade.get("original_stop"))
                trade["runner"] = _create_runner_stop(nx, trade, left, stop_ref, role="RUNNER_SL")
    except Exception as e:
        log.warning("could not re-arm a protective stop for %s while flatten is pending: %s", key, e)
    rec = state.setdefault("needs_flatten", {}).setdefault(key, {"first_seen": time.time(), "alerts_sent": 0, "fails": 0})
    rec["fails"] = int(rec.get("fails", 0)) + 1
    rec.update(reason=reason, note=note, last_attempt=time.time(), error=str(last_err) if last_err else "")
    save_state(state)
    log.error("flatten %s not finished (%s): %s", key, reason, last_err)
    return False


def resolve_needs_flatten(nx: NobitexClient, state: Dict[str, Any]) -> None:
    """A trade whose full close could not be completed is retried here every
    FLATTEN_RETRY_SECONDS until Nobitex confirms it is closed, and the owner is
    told (with buttons) every ~5 minutes while it is still open."""
    needs = state.get("needs_flatten", {})
    if not needs:
        return
    for key, rec in list(needs.items()):
        if key not in state.get("open_trades", {}):
            needs.pop(key, None)
            continue
        if time.time() - float(rec.get("last_attempt", 0)) < FLATTEN_RETRY_SECONDS:
            continue
        try:
            done = _flatten_trade(nx, state, key, rec.get("reason") or "flatten_retry", rec.get("note") or "")
        except Exception:
            log.exception("resolve_needs_flatten failed for %s", key)
            done = False
        rec = state.get("needs_flatten", {}).get(key)
        if not done and rec is not None:
            age = time.time() - float(rec.get("first_seen", time.time()))
            sent = int(rec.get("alerts_sent", 0))
            if age > (sent + 1) * 300:
                notify_admin(ui.msg_flatten_stuck(key, age / 60.0, rec.get("error") or ""), key=key,
                             buttons=[[{"text": "🔁 تلاش مجدد برای بستن", "callback_data":
                                        (f"cmd:/myclose {key}" if _active_user_chat_id else f"cmd:/close {key}")}],
                                      [_btn_positions(), _btn_menu()]])
                rec["alerts_sent"] = sent + 1
    save_state(state)


def _close_remaining_at_price(nx: NobitexClient, state: Dict[str, Any], key: str, reason: str, price: Decimal) -> None:
    trade = state["open_trades"].get(key)
    if not trade:
        return
    try:
        status = _position_status(nx, trade)
    except TransientAPIError as e:
        notify_admin(f"⏳ {key}: بستن ({reason}) انجام نشد چون نوبیتکس موقتاً پاسخ/اجازه نداد ({e}). "
                     f"هیچ سفارشی لغو نشد؛ دوباره تأیید کنید.", key=key,
                     buttons=[[{"text": "✅ بله، ببند", "callback_data": _cb("confirmclose", key)}]])
        state.setdefault("pending_close_confirm", {})[key] = {"kind": reason, "price": str(price), "requested_at": time.time(), "alerts_sent": 0}
        save_state(state)
        return
    if status is None or str(status.get("status", "")).lower() in TERMINAL_POSITION_STATUSES:
        _finalize_closed(state, key, trade, reason, "confirmed closed by Nobitex before this event was processed")
        return
    if not _pos_open(status):
        notify_admin(f"⚠️ {key}: وضعیت پوزیشن نامشخص است ({status.get('status')!r}); چیزی بسته/لغو نشد.", key=key)
        return
    liability = D(status.get("liability", "0"))
    if liability <= 0:
        _finalize_closed(state, key, trade, reason, "liability already zero")
        return

    # Try the strategy price first (limit), exactly like before...
    _cancel_all_trade_orders(nx, trade)
    filled = False
    try:
        resp = nx.place_position_close_limit(position_id=int(trade["position_id"]),
                                             amount=fmt_amount(liability), price=fmt_price(price))
        oid = order_id_from(resp)
        if oid:
            trade.setdefault("close_order_ids", []).append(oid)
            _log_order(trade, oid, "LIMIT_CLOSE", liability, price)
            save_state(state)
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
    except NobitexAPIError as e:
        log.warning("limit close at strategy price failed for %s (%s); closing at market", key, e)
    if filled:
        # ...and it may have filled only in part: verify, never assume.
        try:
            st2 = _position_status(nx, trade)
        except TransientAPIError:
            st2 = status
        if st2 is None or not _pos_open(st2) or _is_dust(D(st2.get("liability", "0") or "0")):
            _finalize_closed(state, key, trade, reason, f"closed at strategy price {price}", exit_price=price)
            return
    # ...anything left (limit not filled / partial) is closed by the verified full-close path.
    _flatten_trade(nx, state, key, reason, f"strategy price {price} not reached; closed at market")


def _close_remaining_market(nx: NobitexClient, state: Dict[str, Any], key: str, reason: str) -> None:
    """Close the WHOLE remaining trade at market (admin/user close, channel exit,
    stop guard). Verified: see _flatten_trade."""
    trade = state["open_trades"].get(key)
    if not trade:
        return True
    return _flatten_trade(nx, state, key, reason)


# ---------------------------------------------------------------------------
# Stop-loss guard
# ---------------------------------------------------------------------------
def _stop_breached(side: str, price: Decimal, stop: Decimal) -> bool:
    return price <= stop if side == "LONG" else price >= stop


def _stop_leg_filled(nx: NobitexClient, trade: dict) -> bool:
    """True if ANY stop order of this trade has (even partly) executed.
    Raises TransientAPIError when the exchange cannot answer."""
    ids = []
    for t in (trade.get("targets") or {}).values():
        if not t.get("hit") and t.get("sl_order_id"):
            ids.append(t["sl_order_id"])
    r = trade.get("runner") or {}
    if r.get("order_id"):
        ids.append(r["order_id"])
    for oid in ids:
        try:
            o = nx.get_order_status(int(oid)).get("order") or {}
        except NobitexAPIError as e:
            if is_transient_error(e):
                raise TransientAPIError(f"{e.code}: {e.message}") from e
            continue
        try:
            if D(o.get("matchedAmount", "0") or "0") > 0:
                return True
        except (InvalidOperation, TypeError):
            continue
    return False


def enforce_stop_guard(nx: NobitexClient, state: Dict[str, Any]) -> None:
    """Whenever the live price is beyond a trade's CURRENT stop for a confirmed
    moment and any volume is still open, close the WHOLE trade at market.
    (Fixes: a stop that fires on only some slices - typically because a
    stop-limit leg never filled after a price gap - leaving part of the trade
    open.) A wick that recovers within STOP_GUARD_CONFIRM_SECONDS is ignored, so
    the exchange's own stop orders always get first chance to do their job."""
    trades = state.get("open_trades", {})
    if not trades:
        return
    now = time.time()
    flat = state.get("needs_flatten", {})
    due = [(k, t) for k, t in trades.items()
           if k not in flat and now - float(t.get("last_guard", 0)) >= STOP_GUARD_INTERVAL_SECONDS]
    if not due:
        return
    try:
        markets = {_trade_market(t) for _, t in due}
        prices = _get_prices(nx, markets)
    except Exception as e:
        log.warning("stop guard: prices unavailable (%s)", e)
        return
    for key, trade in due:
        trade["last_guard"] = now
        try:
            price = prices.get(_trade_market(trade))
            if price is None:
                continue
            side = trade.get("side")
            stop = D(trade.get("stop_price") or trade.get("original_stop"))
            if stop <= 0:
                continue
            if not _stop_breached(side, price, stop):
                if "stop_breach_since" in trade:
                    trade.pop("stop_breach_since", None)
                    trade.pop("stop_breach_n", None)
                continue
            since = float(trade.setdefault("stop_breach_since", now))
            n = int(trade.get("stop_breach_n", 0)) + 1
            trade["stop_breach_n"] = n
            if now - since < STOP_GUARD_CONFIRM_SECONDS or n < STOP_GUARD_MIN_READINGS:
                continue
            status = _position_status(nx, trade)          # TransientAPIError -> retried next round
            if status is None or str(status.get("status", "")).lower() in TERMINAL_POSITION_STATUSES:
                continue                                  # already closed on the exchange: sync records it
            if not _pos_open(status):
                continue
            liability = D(status.get("liability", "0") or "0")
            if _is_dust(liability, price):
                continue
            notify_admin(ui.msg_stop_guard(key, side, price, stop, liability), key=key)
            trade.pop("stop_breach_since", None)
            trade.pop("stop_breach_n", None)
            _flatten_trade(nx, state, key, "stop_guard", "stop level breached; whole remaining volume closed")
        except TransientAPIError as e:
            log.warning("stop guard %s skipped (exchange busy): %s", key, e)
        except Exception:
            log.exception("stop guard failed for %s", key)
    save_state(state)


def _check_stop_fill_and_flatten(nx: NobitexClient, state: Dict[str, Any], key: str, trade: dict, status: dict) -> bool:
    """Second line of defence (runs with the periodic exchange sync): if the live
    volume is smaller than the plan allows for the targets that are really filled,
    and one of the trade's STOP orders has executed, the stop has been hit on
    part of the trade - close the rest now, whatever the price does next.
    Returns True if it took over the trade (closed or queued a retry)."""
    if trade.get("recovered") or not trade.get("initial_amount"):
        return False
    try:
        initial = D(trade["initial_amount"])
        hit_pct = sum((D(t.get("pct", "0")) for t in (trade.get("targets") or {}).values() if t.get("hit")), Decimal("0"))
        expected = initial * max(Decimal("0"), Decimal("1") - hit_pct)
        actual = D(status.get("liability", "0") or "0")
    except (InvalidOperation, TypeError, KeyError):
        return False
    if expected <= 0 or actual >= expected * Decimal("0.98"):
        return False
    if _stop_leg_filled(nx, trade):
        notify_admin(ui.msg_stop_partial(key, trade.get("side"), actual, expected), key=key)
        _flatten_trade(nx, state, key, "stop_guard", "a stop order executed on part of the trade; the rest was closed")
        return True
    if not trade.get("manual_partial_notified"):
        trade["manual_partial_notified"] = True
        notify_admin(f"ℹ️ {key}: حجم باز پوزیشن ({fmt_amount(actual)}) کمتر از برنامه ({fmt_amount(expected)}) است و "
                     f"حد ضرری اجرا نشده؛ احتمالاً بخشی را خودتان دستی بسته‌اید. ربات چیزی را خودکار نمی‌بندد.", key=key)
    return False


def update_runner_trailing(nx: NobitexClient, state: Dict[str, Any]) -> None:
    now = time.time()
    for key, trade in list(state["open_trades"].items()):
        if not trade.get("trailing_active"):
            continue
        if key in state.get("needs_flatten", {}):
            continue
        if now - float(trade.get("last_trailing_check", 0)) < TRAILING_CHECK_SECONDS:
            continue
        trade["last_trailing_check"] = now
        try:
            market = symbol_to_currencies(trade["symbol"])
            try:
                last = nx.get_last_trade_price(f"{market[0].upper()}{market[1].upper()}")
                _note_network_ok()
            except NobitexAPIError as pe:
                if pe.code == "NetworkError":
                    _note_network_trouble(f"آپدیت Runner {key}", pe)
                    raise TransientAPIError(f"{pe.code}: {pe.message}") from pe
                raise
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

            status = _position_status(nx, trade)   # raises TransientAPIError -> skipped below
            if status is None or str(status.get("status", "")).lower() in TERMINAL_POSITION_STATUSES:
                _finalize_closed(state, key, trade, "closed_on_exchange", "confirmed closed by Nobitex during runner trailing update")
                continue
            if not _pos_open(status):
                continue
            liability = D(status.get("liability", "0"))
            if liability <= 0:
                _finalize_closed(state, key, trade, "closed_on_exchange", "liability zero during runner trailing update")
                continue

            _cancel_order_quiet(nx, trade.get("runner", {}).get("order_id"), "runner-trail")
            try:
                trade["runner"] = _create_runner_stop(nx, trade, liability, candidate)
            except Exception as ce:
                _flag_protection_issue(nx, state, key, trade, f"runner trailing re-place failed: {ce}", quiet=True)
                raise
            trade["peak"] = str(peak)
            trade["stop_price"] = str(candidate)
            save_state(state)
            notify_admin(
                ui.card_trailing(key, trade.get("side"), last, peak, candidate, liability,
                                 _stop_vs_liquidation_note(trade, status)),
                key=key,
            )
        except TransientAPIError as e:
            log.warning("runner trailing skipped for %s (exchange busy): %s", key, e)
            continue
        except Exception as e:
            log.exception("runner update failed for %s", key)
            last_alert = float(trade.get("trailing_error_alerted_at", 0))
            if now - last_alert >= 300:  # at most once every 5 minutes per trade
                notify_admin(f"⚠️ Runner trailing update failed for {key}: {e}", key=key)
                trade["trailing_error_alerted_at"] = now


def audit_open_trade_protection(nx: NobitexClient, state: Dict[str, Any]) -> str:
    if not state.get("open_trades"):
        return "🛡️ Protection audit: no open trades in executor state."
    lines = [f"🛡️ Protection audit: {len(state['open_trades'])} trade(s)"]
    for key, trade in list(state["open_trades"].items()):
        try:
            _verify_trade_protection(nx, trade)
            save_state(state)
            lines.append(f"✅ {key}: all TP/SL OCOs + runner stop verified")
        except TransientAPIError as e:
            lines.append(f"⏳ {key}: بررسی به‌دلیل محدودیت/خطای موقت نوبیتکس انجام نشد ({e}) — مشکلی ثبت نشد")
        except Exception as e:
            lines.append(f"🚨 {key}: protection issue — {e}")
            _flag_protection_issue(nx, state, key, trade, f"manual protection audit failed: {e}")
    save_state(state)
    return "\n".join(lines)[:4000]


def _sync_and_tighten(nx: NobitexClient, state: Dict[str, Any], key: str, trade: dict) -> list:
    """Align a trade with what Nobitex really did: mark filled targets as hit
    and, if the strategy stop is tighter than the one in force, move it (via the
    same rebuild used after a channel target-hit message). Raises
    TransientAPIError if the exchange cannot answer (nothing is changed then)."""
    newly = _sync_targets_from_exchange(nx, trade)
    if newly:
        save_state(state)
        notify_admin(f"🎯 {key}: طبق خود نوبیتکس تارگت " + ", ".join(f"T{n}" for n in newly) +
                     " قبلاً Fill شده بود (پیام کانال نرسیده/پردازش نشده بود)؛ وضعیت هماهنگ شد.", key=key)
    # A merged slice has no TP order whose fill could be detected, so detect it by price instead - exactly what a
    # real limit order at that price would have reacted to.
    pending_merged = [(n, t) for n, t in (trade.get("targets") or {}).items() if t.get("merged") and not t.get("hit")]
    if pending_merged:
        try:
            _m = symbol_to_currencies(trade["symbol"])
            _last = nx.get_last_trade_price(f"{_m[0].upper()}{_m[1].upper()}")
        except NobitexAPIError as pe:
            if is_transient_error(pe):
                raise TransientAPIError(f"{pe.code}: {pe.message}") from pe
            _last = None
        if _last is not None:
            hit_now = []
            for n, t in sorted(pending_merged, key=lambda kv: int(kv[0])):
                try:
                    tp = D(t["tp_price"])
                except (InvalidOperation, TypeError, KeyError):
                    continue
                if (trade["side"] == "LONG" and _last >= tp) or (trade["side"] == "SHORT" and _last <= tp):
                    t["hit"] = True
                    t["hit_by_price"] = True
                    hit_now.append(n)
            if hit_now:
                save_state(state)
                notify_admin(f"🎯 {key}: قیمت به " + ", ".join(f"T{n}" for n in hit_now) +
                             " رسید (این سهم‌ها در رانر ادغام‌اند؛ چیزی بسته نشد)؛ "
                             + ("حد ضرر ثابت می‌ماند." if trade.get("fixed_stop") else "حد ضرر طبق استراتژی جابه‌جا می‌شود."), key=key)
    tight = _expected_stop_for_hits(trade)
    if tight is not None and _stop_is_tighter(trade["side"], tight, D(trade["stop_price"])):
        notes = _rebuild_protection(nx, trade, D(trade["stop_price"]), state)
        save_state(state)
        notify_admin(ui.card_stop_moved(key, trade.get("side"), trade.get("stop_price"),
                                        "\n".join(notes) if notes else ""), key=key)
    return newly


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
    pending = state.get("pending_protection", {})
    needs_protection = state.get("needs_protection", {})
    lines = [f"🔄 Startup reconciliation: state={len(state['open_trades'])}, Nobitex active={len(active)}, "
             f"pending_protection={len(pending)}, needs_protection={len(needs_protection)}"]
    if pending:
        lines.append(f"⏳ {len(pending)} pending-protection entry(ies) will be retried immediately: {', '.join(pending.keys())}")
    if needs_protection:
        lines.append(f"🚨 {len(needs_protection)} incomplete-protection entry(ies) will be retried immediately: {', '.join(needs_protection.keys())}")
    for key, trade in list(state["open_trades"].items()):
        pid = int(trade["position_id"])
        if pid not in active_by_id:
            # list_positions answered successfully and this id is not in it -> definitive.
            _finalize_closed(state, key, trade, "closed_on_exchange", "not active on Nobitex at startup reconciliation")
            lines.append(f"❌ {key}: position {pid} no longer active; removed from state.")
            continue
        p = active_by_id[pid]
        trade["entry_actual"] = str(p.get("entryPrice", trade.get("entry_actual")))
        try:
            _sync_and_tighten(nx, state, key, trade)
            _verify_trade_protection(nx, trade)
            lines.append(f"✅ {key}: position {pid} open and protection verified; liability={p.get('liability')}, stop={trade.get('stop_price')}")
        except TransientAPIError as e:
            lines.append(f"⏳ {key}: position {pid} open; بررسی محافظت به‌دلیل محدودیت/خطای موقت نوبیتکس انجام نشد ({e}) — چیزی تغییر نکرد.")
        except Exception as protection_error:
            lines.append(f"🚨 {key}: position {pid} is open but protection is NOT fully verified: {protection_error}")
            _flag_protection_issue(nx, state, key, trade, f"startup protection verification failed: {protection_error}", quiet=True)

    save_state(state)
    notify_admin("\n".join(lines))
    if pending:
        resolve_pending_protection(nx, state)
    if needs_protection:
        resolve_needs_protection(nx, state)


def sync_trades_with_exchange(nx: NobitexClient, state: Dict[str, Any], max_per_call: int = 2) -> None:
    """Every SYNC_INTERVAL_SECONDS per trade (a couple of trades per loop tick,
    so commands stay responsive): confirm the position is still open, sync filled
    targets, and apply the strategy stop for them. Independent of channel
    messages, so a missed/late 'Target hit' message can no longer leave the
    executor's picture wrong. Any API problem just skips the trade this round."""
    now = time.time()
    due = sorted((float(t.get("last_sync", 0)), k) for k, t in state.get("open_trades", {}).items()
                 if now - float(t.get("last_sync", 0)) >= _sync_interval_for(t))
    for _, key in due[:max(max_per_call, 3)]:
        trade = state["open_trades"].get(key)
        if not trade:
            continue
        trade["last_sync"] = now
        try:
            status = _position_status(nx, trade)
            if status is None or str(status.get("status", "")).lower() in TERMINAL_POSITION_STATUSES:
                _finalize_closed(state, key, trade, "closed_on_exchange", "Nobitex reports the position closed (TP/SL/liquidation on the exchange)")
                continue
            if not _pos_open(status) or D(status.get("liability", "0")) <= 0:
                continue
            if key in state.get("needs_flatten", {}):
                continue  # the closing loop owns this trade right now
            if _check_stop_fill_and_flatten(nx, state, key, trade, status):
                continue
            if key in state.get("needs_protection", {}):
                continue  # the repair loop owns this trade right now
            _sync_and_tighten(nx, state, key, trade)
        except TransientAPIError as e:
            log.warning("sync %s skipped (exchange busy): %s", key, e)
        except Exception as e:
            log.exception("sync failed for %s", key)
    save_state(state)


def _load_entry_signals(gh: GithubClient) -> list:
    content, _sha = gh.get_file("signals.jsonl", allow_missing=True)
    out = []
    for line in (content or "").splitlines():
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
            parsed = parse_message(obj["text"])
        except Exception:
            continue
        if isinstance(parsed, ParsedSignal):
            setattr(parsed, "source_update_id", str(obj.get("signal_id") or obj.get("update_id") or ""))
            out.append(parsed)
    return out


def _infer_hits_from_done_orders(nx: NobitexClient, trade: dict) -> list:
    """For an adopted position whose order ids are unknown: a target counts as
    hit if Nobitex has a DONE closing order at (about) that target's price."""
    try:
        src, dst = symbol_to_currencies(trade["symbol"])
        orders = nx.list_orders(src_currency=src, dst_currency=dst, status="done", trade_type="margin", details=2).get("orders") or []
    except Exception:
        return []
    closing = "sell" if trade["side"] == "LONG" else "buy"
    pid = int(trade["position_id"])
    hits = []
    for n in _tnums(trade):
        tp = D(trade["targets"][str(n)]["tp_price"])
        for o in orders:
            if str(o.get("type", "")).lower() != closing or str(o.get("status", "")).lower() != "done":
                continue
            op = _order_position_id(o)
            if op is not None and op != pid:
                continue
            try:
                if D(o.get("price") or "0") > 0 and abs(D(o["price"]) - tp) / tp <= Decimal("0.0005") and D(o.get("matchedAmount") or "0") > 0:
                    hits.append(n)
                    break
            except (InvalidOperation, TypeError, KeyError):
                continue
    return hits


def _close_raw_position(nx: NobitexClient, state: Dict[str, Any], position_id: int) -> str:
    """Market-close a position that has NO tracked trade in state (an
    untracked/unmatched orphan the admin chose to close by hand via the
    ❌ بستن button). Cancels any of its own open orders first - both ones
    Nobitex tags with this position id and, as a fallback, close-side orders
    on the same market/side (an untracked position was, by definition, never
    opened by this executor, so its orders were never recorded anywhere)."""
    try:
        status = nx.get_position_status(position_id).get("position")
    except NobitexAPIError as e:
        if e.code in ("HTTP404", "NotFound") or e.http_status == 404:
            return f"ℹ️ پوزیشن #{position_id} روی نوبیتکس پیدا نشد (احتمالاً قبلاً بسته شده)."
        return f"⏳ #{position_id}: نوبیتکس موقتاً پاسخ نداد ({e}); دوباره امتحان کنید."
    if not status or str(status.get("status", "")).lower() != "open":
        return f"ℹ️ پوزیشن #{position_id} دیگر باز نیست؛ کاری انجام نشد."
    src = str(status.get("srcCurrency", "")).lower()
    dst = str(status.get("dstCurrency", "")).lower()
    closing = "sell" if str(status.get("side", "")).lower() == "buy" else "buy"
    try:
        orders = nx.list_orders(src_currency=src, dst_currency=dst, status="open", trade_type="margin", details=2).get("orders") or []
    except Exception:
        orders = []
    cancelled = 0
    for o in orders:
        op = _order_position_id(o)
        if op is not None:
            if op != position_id:
                continue
        elif str(o.get("type", "")).lower() != closing:
            continue
        if o.get("id") is not None and _cancel_order_quiet(nx, o["id"], f"orphan-close-{position_id}"):
            cancelled += 1
    liability = D(status.get("liability", "0"))
    if liability <= 0:
        return f"ℹ️ #{position_id}: liability صفر است؛ چیزی برای بستن نبود ({cancelled} سفارش لغو شد)."
    try:
        nx.close_position_market(position_id, amount=fmt_amount(liability))
    except NobitexAPIError as e:
        return f"🚨 #{position_id}: بستن Market شکست خورد ({cancelled} سفارش لغو شده بود): {e.code} - {e.message}"
    state.get("unmatched_orphans", {}).pop(str(position_id), None)
    save_state(state)
    return f"✅ #{position_id} با Market بسته شد ({cancelled} سفارش قدیمی هم لغو شد)."


def recover_orphan_positions(nx: NobitexClient, gh: GithubClient, state: Dict[str, Any], only_known: bool = False,
                              position_ids: Optional[set] = None) -> tuple:
    """Re-adopt open Nobitex positions that the executor no longer tracks
    (state wiped by the old rate-limit bug, state.json lost, etc.).
    Each orphan is matched to its original channel signal (symbol + side + entry
    within 1.5%, and the timeframe from history when known) to restore targets,
    stop and Signal ID; then protection is rebuilt from the live exchange picture.
    only_known=True (automatic mode) only adopts positions with a history entry,
    and only re-alerts an unmatched one every ORPHAN_ALERT_COOLDOWN_SECONDS (never
    if the admin already tapped "بررسی شد"). only_known=False (the /recover
    command) always reports every unmatched orphan, ack or not, since the admin
    explicitly asked. position_ids restricts the check to specific position(s)
    (e.g. "/recover 119056412"). Never closes anything - closing an unmatched
    orphan is the admin's explicit ❌ بستن button, handled by _close_raw_position.
    Returns (message_text, buttons)."""
    active = nx.list_positions(status="active").get("positions", [])
    tracked = _tracked_position_ids(state)
    orphans = []
    for p in active:
        try:
            pid = int(p.get("id"))
        except (TypeError, ValueError):
            continue
        if pid in tracked or str(p.get("status", "")).lower() != "open":
            continue
        if position_ids and pid not in position_ids:
            continue
        orphans.append(p)
    unmatched = state.setdefault("unmatched_orphans", {})
    if not orphans:
        if position_ids:
            return f"ℹ️ پوزیشن(های) {', '.join(str(x) for x in position_ids)} روی نوبیتکس فعال/بی‌صاحب پیدا نشد.", None
        return ("" if only_known else "✅ هیچ پوزیشن بی‌صاحبی روی نوبیتکس نیست؛ همه‌ی پوزیشن‌های باز در state هستند."), None

    live_pids = {int(p["id"]) for p in orphans}
    # A previously-unmatched orphan that is no longer live (closed/adopted elsewhere) - drop its bookkeeping.
    for stale in [k for k in unmatched if int(k) not in live_pids and not position_ids]:
        unmatched.pop(stale, None)

    hist = state.get("trade_history", [])
    signals = None
    lines = []
    buttons = []
    for p in orphans:
        pid = int(p["id"])
        h = next((x for x in reversed(hist) if x.get("position_id") == pid), None)
        if only_known and not h:
            continue
        src = str(p.get("srcCurrency", "")).lower()
        dst = str(p.get("dstCurrency", "")).lower()
        side = "LONG" if str(p.get("side", "")).lower() == "buy" else "SHORT"
        symbol = src.upper()
        try:
            entry = D(p.get("entryPrice"))
        except (InvalidOperation, TypeError):
            lines.append(f"❓ #{pid} {symbol}: قیمت ورود نامشخص؛ دست نخورد.")
            continue
        if dst != "usdt" or entry <= 0:
            lines.append(f"❓ #{pid} {symbol}/{dst}: بازار USDT نیست؛ دست نخورد.")
            continue
        if signals is None:
            signals = _load_entry_signals(gh)
        cands = [s_ for s_ in signals if s_.symbol.upper() == symbol and s_.side.upper() == side
                 and abs(D(s_.entry) - entry) / entry <= Decimal("0.015")
                 and (not h or str(s_.timeframe).upper() == str(h.get("timeframe", s_.timeframe)).upper())]
        if not cands:
            rec = unmatched.setdefault(str(pid), {"first_seen": time.time(), "alerts_sent": 0})
            rec["symbol"], rec["side"], rec["entry"] = symbol, side, str(entry)
            if not position_ids:
                if rec.get("acked"):
                    continue
                if only_known and time.time() - float(rec.get("last_alert", 0)) < ORPHAN_ALERT_COOLDOWN_SECONDS:
                    continue
            rec["last_alert"] = time.time()
            rec["alerts_sent"] = int(rec.get("alerts_sent", 0)) + 1
            lines.append(f"❓ #{pid} {symbol} {side} (ورود≈{entry}): سیگنال مطابق در کانال پیدا نشد؛ دست نخورد "
                         f"(اگر دستی باز کرده‌اید طبیعی است) — با دکمه‌های زیر تصمیم بگیرید:")
            buttons.append([
                {"text": "✅ بررسی شد", "callback_data": f"cmd:/ack_orphan {pid}"},
                {"text": "❌ بستن این پوزیشن", "callback_data": f"cmd:/closeorphan {pid}"},
            ])
            buttons.append([
                {"text": "🔁 تلاش دوباره", "callback_data": f"cmd:/recover {pid}"},
                {"text": "🛡 محافظت دستی", "callback_data": f"cmd:/protecthelp {pid}"},
            ])
            continue
        best = min(cands, key=lambda s_: (abs(D(s_.entry) - entry), 0))
        # among equally close ones prefer the most recent
        best_diff = abs(D(best.entry) - entry)
        for s_ in cands:
            if abs(D(s_.entry) - entry) <= best_diff:
                best = s_
        unmatched.pop(str(pid), None)
        key = trade_key(best.symbol, best.timeframe)
        if key in state["open_trades"]:
            lines.append(f"⚠️ #{pid}: کلید {key} قبلاً برای معامله‌ی دیگری در state هست؛ دست نخورد.")
            continue
        live_liab = D(p.get("liability", "0"))
        if live_liab <= 0:
            continue
        lev = p.get("leverage") or (h or {}).get("leverage") or "5"
        stop_from_hist = (h or {}).get("final_stop")
        trade = {
            "symbol": best.symbol, "timeframe": best.timeframe, "side": side,
            "position_id": pid,
            "entry_signal": str(best.entry), "entry_actual": str(entry),
            "original_stop": str(best.stop),
            "risk_usdt": str((h or {}).get("risk_usdt") or abs(entry - D(best.stop)) * live_liab),
            "risk_cap_usdt": str((h or {}).get("risk_usdt") or ""),
            "collateral": str((h or {}).get("collateral") or (live_liab * entry / D(lev))),
            "leverage": str(lev),
            "initial_amount": str(live_liab), "initial_amount_requested": str(live_liab),
            "risk_unit": str(abs(D(best.entry) - D(best.stop))),
            "targets": {}, "runner": {},
            "client_prefix": f"tc-{key[:8]}-recovered",
            "signal_id": getattr(best, "signal_id", None) or getattr(best, "source_update_id", None),
            "closed_pct": "0", "last_trailing_check": 0, "peak": str(entry), "trailing_active": False,
            "stop_price": str(stop_from_hist or best.stop),
            "opened_at": (h or {}).get("opened_at") or time.time(),
            "recovered": True,
        }
        for n, pct in {1: W1, 2: W2, 3: W3, 4: W4}.items():
            trade["targets"][str(n)] = {"target": n, "pct": str(pct), "tp_price": str(D(best.targets[n])),
                                        "hit": False, "tp_order_id": None, "sl_order_id": None}
        hit_set = {int(x[1:]) for x in ((h or {}).get("targets_hit") or []) if str(x).startswith("T")}
        hit_set |= set(_infer_hits_from_done_orders(nx, trade))
        if hit_set:
            top = max(hit_set)
            for n in range(1, top + 1):     # targets are reached in order
                trade["targets"][str(n)]["hit"] = True
        trade["closed_pct"] = str(sum((D(t["pct"]) for t in trade["targets"].values() if t["hit"]), Decimal("0")))
        state["open_trades"][key] = trade
        # The history rows written by the old false-"closed" bug for this very position are wrong: drop them.
        state["trade_history"] = [x for x in state.get("trade_history", [])
                                  if not (x.get("position_id") == pid and x.get("reason") == "closed_externally")]
        save_state(state)
        try:
            notes = _rebuild_protection(nx, trade, D(trade["stop_price"]), state)
            _verify_trade_protection(nx, trade)
            state.get("needs_protection", {}).pop(key, None)
            save_state(state)
            hits_txt = ", ".join(f"T{n}" for n in _tnums(trade) if trade["targets"][str(n)]["hit"]) or "هیچ‌کدام"
            lines.append(f"✅ {key} (#{pid}) بازیابی و محافظت‌شده — تارگت‌های رسیده: {hits_txt}، حد ضرر: {trade['stop_price']}، "
                         f"🆔 {trade['signal_id']}" + ("".join("\n   " + n_ for n_ in notes)))
        except TransientAPIError as e:
            _flag_protection_issue(nx, state, key, trade, f"recovered but protection pending (exchange busy): {e}", quiet=True)
            lines.append(f"⏳ {key} (#{pid}) به state برگشت؛ ثبت محافظت به‌خاطر محدودیت نوبیتکس به تلاش خودکار بعدی موکول شد.")
        except Exception as e:
            _flag_protection_issue(nx, state, key, trade, f"recovered but protection failed: {e}", quiet=True)
            lines.append(f"⚠️ {key} (#{pid}) به state برگشت ولی محافظت کامل نشد ({e}); تلاش خودکار ادامه دارد.")
    save_state(state)
    if not lines:
        return "", None
    buttons_out = buttons if buttons else None
    if buttons_out:
        buttons_out = buttons_out + [[{"text": "📋 منو", "callback_data": "menu"}]]
    return "♻️ بازیابی پوزیشن‌های بی‌صاحب\n" + "\n".join(lines), buttons_out


def auto_recover_orphans(nx: NobitexClient, gh: GithubClient, state: Dict[str, Any]) -> None:
    global _last_orphan_check
    now = time.time()
    if now - _last_orphan_check < ORPHAN_CHECK_SECONDS:
        return
    _last_orphan_check = now
    try:
        msg, buttons = recover_orphan_positions(nx, gh, state, only_known=True)
    except Exception as e:
        log.warning("auto orphan check skipped: %s", e)
        return
    if msg:
        notify_admin(msg, buttons=buttons)


def _normalize_result(result: Any) -> list:
    """A command handler may return a str, (text, buttons), or a list of those (several messages).
    Always yields [(text, buttons_or_None), ...] with every text clipped to Telegram's size."""
    if result is None:
        return []
    if isinstance(result, str):
        return [(ui.clip(result), None)]
    if isinstance(result, tuple) and len(result) == 2 and isinstance(result[0], str):
        return [(ui.clip(result[0]), result[1])]
    out = []
    for item in result:
        out.extend(_normalize_result(item))
    return out


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
            # Present ONLY when bridge.py relayed this from a non-admin chat
            # (its self-service path). Admin commands never carry this field -
            # see the caller_chat_id docstring on handle_bot_command for why
            # that absence, not a value, is what authorizes the full command set.
            caller_chat_id = obj.get("chat_id")
            if not command_id or not command:
                continue
        except Exception:
            continue
        if command_id in processed:
            continue
        global _caller_meta
        # At-most-once: the id is recorded on disk BEFORE the command runs. If the program is killed or restarts
        # in the middle (power cut, window closed, GitHub outage) the command is NOT executed a second time on
        # restart - a double 'close' / 'subscribe' / 'confirm payment' is far worse than one lost tap.
        processed.add(command_id)
        state["processed_command_ids"] = list(processed)[-5000:]
        save_state(state)
        try:
            _caller_meta = obj.get("meta") if isinstance(obj.get("meta"), dict) else {}
            result = handle_bot_command(command, state, nx, gh, caller_chat_id=caller_chat_id)
            messages = _normalize_result(result)
            if caller_chat_id is not None:
                # Self-service reply goes to the subscriber's own chat, never the admin chat.
                for text, btns in messages:
                    notify_admin(text, chat_id=caller_chat_id, buttons=btns or [[{"text": "📋 منوی من", "callback_data": "cmd:/menu"}]])
            else:
                for text, btns in messages:
                    notify_admin(text, buttons=btns or [[{"text": "📋 منو", "callback_data": "menu"}]])
            processed.add(command_id)
        except Exception as e:
            if caller_chat_id is not None:
                notify_admin("❌ خطای داخلی؛ لطفاً دوباره امتحان کنید.", chat_id=caller_chat_id,
                             buttons=[[{"text": "📋 منوی من", "callback_data": "cmd:/menu"}]])
                log.exception("self-service command failed for chat %s: %s", caller_chat_id, command)
            else:
                notify_admin(f"🚨 اجرای فرمان ناموفق بود: {type(e).__name__}: {e}", buttons=[[{"text": "📋 منو", "callback_data": "menu"}]])
            # Mark it processed after reporting, so a permanently bad command
            # cannot loop forever.
            processed.add(command_id)
        finally:
            _caller_meta = {}
        changed = True
    if changed:
        state["processed_command_ids"] = list(processed)[-5000:]
        save_state(state)


def poll_once(nx: NobitexClient, gh: GithubClient, state: Dict[str, Any],
              control_override: Optional[dict] = None) -> None:
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
            if not _active_user_chat_id:
                notify_admin(f"⚠️ Invalid JSON line in signals.jsonl: {line[:200]}")
            continue

        processed_ids = state.setdefault("processed_signal_ids", [])
        if signal_uid in processed_ids:
            continue
        if update_id and update_id <= last_update_id and not message_id:
            continue

        parsed = parse_message(text)
        if parsed is None:
            if _is_known_broadcast(text):
                # A recurring informational broadcast (e.g. the daily/weekly
                # performance recap), not a trade signal - not relevant to
                # this executor, so skip it quietly instead of alerting.
                processed_ids.append(signal_uid)
                state["processed_signal_ids"] = processed_ids[-5000:]
                if update_id:
                    last_update_id = max(last_update_id, update_id)
                continue
            state.setdefault("unparsed_messages", []).append({"ts": time.time(), "text": text[:1000], "update_id": update_id})
            state["unparsed_messages"] = state["unparsed_messages"][-200:]
            if not _active_user_chat_id:
                notify_admin(f"⚠️ Channel message could not be parsed; no exchange action taken:\n{text[:500]}")
            processed_ids.append(signal_uid)
            state["processed_signal_ids"] = processed_ids[-5000:]
            if update_id:
                last_update_id = max(last_update_id, update_id)
            continue

        if isinstance(parsed, ParsedSignal):
            # A signal that sat unprocessed for too long (executor offline,
            # GitHub relay backlog, etc.) no longer describes the current
            # market - opening it now would enter at a stale price with a
            # stop/targets meant for a candle that has already passed. Instead
            # of silently dropping it forever, the admin is notified exactly
            # ONCE (this signal_uid is marked processed either way, so it is
            # never re-alerted) with how far the current price now sits from
            # the signal's entry, and can open it anyway in one tap if it is
            # still close enough - it then opens exactly like any other signal
            # (same sizing/stop/targets/protection), it is not a shortcut.
            posted_at = obj.get("posted_at") or obj.get("ts")
            if posted_at:
                age_seconds = time.time() - float(posted_at)
                max_age = max_signal_age_seconds(parsed.timeframe)
                if age_seconds > max_age and _active_user_chat_id:
                    log.info("subscriber %s: stale signal %s skipped (%.0f min old)", _active_user_chat_id, signal_uid, age_seconds / 60)
                    processed_ids.append(signal_uid)
                    state["processed_signal_ids"] = processed_ids[-5000:]
                    if update_id:
                        last_update_id = max(last_update_id, update_id)
                    continue
                if age_seconds > max_age:
                    key_late = trade_key(parsed.symbol, parsed.timeframe)
                    drift_txt = ""
                    try:
                        src_l, dst_l = symbol_to_currencies(parsed.symbol)
                        live_price = nx.get_last_trade_price(f"{src_l.upper()}{dst_l.upper()}")
                        drift_pct = abs(live_price - D(parsed.entry)) / D(parsed.entry) * 100
                        drift_txt = f"\nقیمت فعلی بازار: {live_price} (فاصله با نقطه‌ی ورود سیگنال: {drift_pct:.2f}٪)"
                    except Exception:
                        pass
                    state.setdefault("pending_late_signal", {})[key_late] = {
                        "symbol": parsed.symbol, "timeframe": parsed.timeframe, "side": parsed.side,
                        "entry": str(parsed.entry), "stop": str(parsed.stop),
                        "targets": {str(n): str(parsed.targets[n]) for n in (1, 2, 3, 4)},
                        "signal_id": getattr(parsed, "signal_id", None),
                        "source_update_id": signal_uid, "posted_at": float(posted_at), "age_minutes": age_seconds / 60,
                    }
                    save_state(state)
                    notify_admin(
                        f"⏭️ سیگنال {key_late} به دلیل قدیمی بودن خودکار باز نشد "
                        f"({age_seconds/60:.1f} دقیقه از انتشار آن گذشته؛ سقف مجاز برای این تایم‌فریم "
                        f"~{max_age/60:.0f} دقیقه است).{drift_txt}\n"
                        f"ورود سیگنال: {parsed.entry} | حد ضرر: {parsed.stop}\n"
                        f"اگر فاصله‌ی قیمت زیاد نیست، می‌توانید با دکمه‌ی زیر آن را دقیقاً مثل یک سیگنال تازه با قیمت "
                        f"فعلی بازار باز کنید (فقط یک بار این هشدار می‌آید):",
                        key=key_late,
                        buttons=[[
                            {"text": "✅ باز کن (قیمت فعلی)", "callback_data": f"cmd:/openlate {key_late}"},
                            {"text": "❌ نادیده بگیر", "callback_data": f"cmd:/dismisslate {key_late}"},
                        ]],
                    )
                    processed_ids.append(signal_uid)
                    state["processed_signal_ids"] = processed_ids[-5000:]
                    if update_id:
                        last_update_id = max(last_update_id, update_id)
                    continue

        try:
            if isinstance(parsed, ParsedSignal):
                setattr(parsed, "source_update_id", signal_uid)
                handle_signal(nx, state, parsed, gh, control_override=control_override)
            elif isinstance(parsed, ParsedEvent):
                handle_event(nx, state, parsed)
            processed_ids.append(signal_uid)
            state["processed_signal_ids"] = processed_ids[-5000:]
            if update_id:
                last_update_id = max(last_update_id, update_id)
        except TransientAPIError as e:
            # Nobitex rate-limit / network: nothing was decided or changed. Leave this
            # message unprocessed and retry next cycle (no admin spam).
            log.warning("message %s postponed (exchange busy): %s", signal_uid, e)
            break
        except SignalFinalized as e:
            # A definitive, terminal exchange action was already taken
            # (protected, or flagged for admin attention because it
            # couldn't be protected) - mark this message processed either way
            # so it is never retried as a fresh, not-yet-opened signal, and
            # keep going with the rest of this batch.
            log.warning("Signal %s finalized via fail-safe: %s", signal_uid, e)
            processed_ids.append(signal_uid)
            state["processed_signal_ids"] = processed_ids[-5000:]
            if update_id:
                last_update_id = max(last_update_id, update_id)
        except Exception as e:
            # Nothing irreversible happened yet (e.g. the entry order itself
            # could not even be placed) - safe and worthwhile to retry on the
            # next cycle, so this signal is deliberately left unprocessed.
            log.exception("message processing failed")
            notify_admin(f"🚨 Unexpected executor error for channel signal={signal_uid} update_id={update_id}: {e}")
            break

    state["last_signal_update_id"] = last_update_id
    state["github_signals_processed_lines"] = len(lines)  # informational only
    state["last_successful_poll_ts"] = time.time()
    save_state(state)



# =====================================================================
# Multi-tenant engine: one isolated trading account per subscriber.
# The admin's own account is never touched by any of this - it keeps its
# own NobitexClient/state/control exactly as before, and this code only
# ever runs the SAME already-tested trading functions again with the
# subscriber's own client, own state file and own risk settings.
# =====================================================================
USER_STATES_DIR = Path(os.environ.get("USER_STATES_DIR", "user_states"))
_user_states: Dict[str, Dict[str, Any]] = {}
_user_clients: Dict[str, tuple] = {}          # uid -> (key fingerprint, NobitexClient)
_user_error_notice: Dict[str, float] = {}


def _safe_uid(uid: str) -> str:
    uid = str(uid).strip()
    if not uid.isdigit():
        raise ValueError("شناسه‌ی کاربر باید فقط عدد باشد (Telegram user id).")
    return uid


def user_state_path(uid: str) -> str:
    return str(USER_STATES_DIR / f"{_safe_uid(uid)}.json")


def _signal_uid_of(obj: dict) -> str:
    update_id = int(obj.get("update_id", 0) or 0)
    message_id = int(obj.get("message_id", 0) or 0)
    return str(obj.get("signal_id") or (f"{TELEGRAM_CHANNEL_ID}:{message_id}" if message_id else update_id))


def _seed_new_user_state(gh: GithubClient, state: Dict[str, Any]) -> None:
    """A brand-new subscriber must start from NOW. Without this, their empty
    state would look like 'nothing processed yet' and poll_once would replay
    the channel's whole signals.jsonl history against their real account."""
    content, _sha = gh.get_file("signals.jsonl", allow_missing=True)
    ids, max_update = [], 0
    for line in content.splitlines():
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
            ids.append(_signal_uid_of(obj))
            max_update = max(max_update, int(obj.get("update_id", 0) or 0))
        except Exception:
            continue
    state["processed_signal_ids"] = ids[-5000:]
    state["last_signal_update_id"] = max_update
    state["seeded_at"] = time.time()


def _get_user_state(rec, gh: Optional[GithubClient] = None) -> Dict[str, Any]:
    uid = _safe_uid(rec.user_id)
    st = _user_states.get(uid)
    if st is not None:
        return st
    path = user_state_path(uid)
    fresh = not os.path.exists(path)
    st = load_state(path)
    gh = gh or _gh_main
    if fresh:
        if gh is None:
            raise RuntimeError("cannot create a subscriber state without a GitHub client (would replay channel history)")
        _seed_new_user_state(gh, st)
        _raw_save_state(st, path)
    _user_states[uid] = st
    return st


def _get_user_client(rec) -> NobitexClient:
    """The subscriber's own Nobitex client, built from their decrypted keys
    (cached; rebuilt automatically if they reconnect with a new key)."""
    uid = _safe_uid(rec.user_id)
    fp = hashlib.sha256((rec.public_key_enc or "").encode()).hexdigest()
    cached = _user_clients.get(uid)
    if cached and cached[0] == fp:
        return cached[1]
    pub, priv = _vault.get_decrypted_keys(uid)
    nx_user = NobitexClient(NobitexConfig(public_key=pub, private_key_b64=priv,
                                          base_url=NOBITEX_BASE_URL, public_base_url=NOBITEX_PUBLIC_BASE_URL))
    _user_clients[uid] = (fp, nx_user)
    return nx_user


@contextlib.contextmanager
def _user_context(rec, gh: Optional[GithubClient] = None):
    """Everything inside runs as this subscriber: their client, their state,
    their state file for every save_state(), their chat for every
    notify_admin(), their client for realized-P&L lookups. Always restored,
    even on error, so a failure can never leak into the next account."""
    global _active_user_chat_id, _active_state_path, _nx_for_history, _active_user_auto_exit, _ui_uid
    prev = (_active_user_chat_id, _active_state_path, _nx_for_history, _active_user_auto_exit, _ui_uid)
    nx_user = _get_user_client(rec)
    st = _get_user_state(rec, gh)
    try:
        _active_user_chat_id = str(rec.notify_chat_id or rec.user_id)
        _active_state_path = user_state_path(rec.user_id)
        _nx_for_history = nx_user
        _active_user_auto_exit = bool(getattr(rec, "auto_exit", True))
        _ui_uid = str(rec.user_id)
        yield nx_user, st
    finally:
        _active_user_chat_id, _active_state_path, _nx_for_history, _active_user_auto_exit, _ui_uid = prev


def _fmt_amt(amount, currency) -> str:
    try:
        return f"{int(round(float(amount))):,} تومان" if str(currency) == "toman" else f"{float(amount):g} USDT"
    except (TypeError, ValueError):
        return f"{amount} {currency}"


def num_s(x) -> str:
    return ui.num(x, 2)


def user_control(rec) -> dict:
    """Per-subscriber replacement for control.json. New entries are allowed
    only while the subscription is valid and they have not paused."""
    return {"enabled": bool(rec.is_active() and not rec.paused),
            "risk_usdt": rec.risk_usdt, "max_collateral_usdt": rec.max_collateral_usdt,
            "max_open_trades": int(rec.max_open_trades), "leverage": rec.leverage,
            "daily_loss_usdt": getattr(rec, "daily_loss_usdt", "0")}


def _user_open_count(uid: str) -> int:
    st = _user_states.get(str(uid))
    if st is None:
        p = user_state_path(uid)
        if not os.path.exists(p):
            return 0
        st = load_state(p)
    return len(st.get("open_trades", {}))


_user_key_health: Dict[str, Dict[str, Any]] = {}
KEY_PROBE_SECONDS = 600


def _is_auth_error(e: Exception) -> bool:
    if not isinstance(e, NobitexAPIError):
        return False
    code = str(getattr(e, "code", "")).lower()
    return (getattr(e, "http_status", None) in (401, 403)
            or any(w in code for w in ("unauthor", "apikey", "api_key", "forbidden", "permission", "invalidkey")))


def _probe_user_key(uid: str, nx_u) -> bool:
    """Cheap authenticated call every KEY_PROBE_SECONDS (and every cycle while
    the key is known-bad, so recovery is noticed). Returns False when the
    subscriber's key was revoked/rotated/expired on Nobitex - the exchange-
    side stop/target orders already resting on Nobitex keep protecting any
    open position, but this executor can no longer act for them, so both the
    subscriber and the admin are told ONCE and the cycle is skipped until the
    key works again (or they /connect a new one)."""
    h = _user_key_health.setdefault(uid, {"last_probe": 0.0, "bad": False})
    now = time.time()
    if not h["bad"] and now - h["last_probe"] < KEY_PROBE_SECONDS:
        return True
    h["last_probe"] = now
    try:
        nx_u.get_margin_usdt_balance()
    except Exception as e:
        if _is_auth_error(e):
            if not h["bad"]:
                h["bad"] = True
                _vault.set_last_error(uid, f"کلید API نامعتبر/لغو شده: {e}")
                notify_admin("🚨 کلید API شما در نوبیتکس دیگر معتبر نیست (لغو، تغییر یا منقضی شده).\n"
                             "سفارش‌های حد ضرر/تارگتی که قبلاً روی نوبیتکس ثبت شده‌اند همچنان فعال‌اند، ولی ربات دیگر نمی‌تواند "
                             "معامله‌ی جدید باز کند یا محافظت را به‌روز کند.\nیک کلید جدید بسازید و از دکمه‌ی زیر وصل کنید.",
                             chat_id=_active_user_chat_id or uid,
                             buttons=[[{"text": "🔑 اتصال کلید جدید", "callback_data": "ask:apikey"}], [{"text": "🆘 پشتیبانی", "callback_data": "ask:support"}]])
                notify_admin(f"🚨 کلید API کاربر {uid} نامعتبر شد ({_user_open_count(uid)} معامله‌ی باز).")
            return False
        return True          # transient/other: let the normal cycle deal with it
    if h["bad"]:
        h["bad"] = False
        _vault.set_last_error(uid, None)
        notify_admin("✅ اتصال کلید API شما دوباره برقرار شد.", chat_id=_active_user_chat_id or uid)
    return True


def run_user_cycle(gh: GithubClient, rec) -> None:
    """One pass of the trading engine for one subscriber. Signals/events
    still arrive only while active or while they have open trades (open
    trades must keep receiving stop moves, trailing and protection repair
    even after expiry - never abandon a live position); new entries are
    blocked by user_control() when expired/suspended/paused."""
    uid = _safe_uid(rec.user_id)
    has_open = _user_open_count(uid) > 0
    if not rec.public_key_enc or not (rec.is_active() or has_open):
        return
    try:
        with _user_context(rec, gh) as (nx_u, st):
            if not _probe_user_key(uid, nx_u):
                return
            _verify_protection_once(uid, nx_u, st)
            enforce_stop_guard(nx_u, st)
            resolve_needs_flatten(nx_u, st)
            poll_once(nx_u, gh, st, control_override=user_control(rec))
            resolve_pending_protection(nx_u, st)
            resolve_needs_protection(nx_u, st)
            enforce_stop_guard(nx_u, st)
            sync_trades_with_exchange(nx_u, st)
            resolve_pending_close_confirm(st, nx_u)
            update_runner_trailing(nx_u, st)
        if rec.last_error:
            _vault.set_last_error(uid, None)
    except Exception as e:
        msg = f"{type(e).__name__}: {e}"
        log.exception("subscriber %s cycle failed", uid)
        transient = (isinstance(e, TransientAPIError)
                     or (isinstance(e, NobitexAPIError) and is_transient_error(e))
                     or type(e).__module__.startswith(("requests", "urllib3")))
        if not transient:
            if msg != rec.last_error:
                _vault.set_last_error(uid, msg)
            now = time.time()
            if now - _user_error_notice.get(uid, 0) > 3600:
                _user_error_notice[uid] = now
                notify_admin(f"🚨 خطا در حساب کاربر {uid}: {msg}")


class _SignalsSnapshot:
    """Wraps the GitHub client so that within ONE loop tick the shared
    signals.jsonl is downloaded once and served to every subscriber's cycle
    (N subscribers must not mean N GitHub API calls per tick - that would
    exhaust the API rate limit long before Nobitex limits matter)."""
    def __init__(self, gh: GithubClient):
        self._gh = gh
        self._signals = None

    def get_file(self, path, allow_missing=False):
        if path == "signals.jsonl":
            if self._signals is None:
                self._signals = self._gh.get_file(path, allow_missing=allow_missing)
            return self._signals
        return self._gh.get_file(path, allow_missing=allow_missing)

    def __getattr__(self, name):
        return getattr(self._gh, name)


_user_startup_verified: set = set()


def _verify_protection_once(uid: str, nx_u, st: Dict[str, Any]) -> None:
    """First cycle after this process starts: re-check that every open trade of
    the subscriber still has its TP/SL/runner orders on Nobitex (same check the
    admin gets at startup), silently queueing a repair for anything missing."""
    if uid in _user_startup_verified:
        return
    _user_startup_verified.add(uid)
    for key, trade in list(st.get("open_trades", {}).items()):
        try:
            _verify_trade_protection(nx_u, trade)
        except TransientAPIError:
            _user_startup_verified.discard(uid)      # try again next cycle
            return
        except Exception as e:
            _flag_protection_issue(nx_u, st, key, trade, f"startup verification failed: {e}", quiet=True)


def run_all_user_cycles(gh: GithubClient, between=None) -> None:
    """One trading pass per subscriber. `between` (optional) runs after each subscriber - the main loop uses it
    to answer waiting Telegram commands in between, so a slow pass over many subscribers can no longer make
    every button wait for the whole pass to finish (that was a main cause of slow replies)."""
    gh = _SignalsSnapshot(gh)
    for uid in [r.user_id for r in list(_vault.all())]:
        rec = _vault.get(uid)            # fresh copy: a command handled in `between` may have changed it
        if rec is None:
            continue
        try:
            run_user_cycle(gh, rec)
        except Exception:
            log.exception("run_user_cycle wrapper failed for %s", getattr(rec, "user_id", "?"))
        if between is not None:
            try:
                between()
            except Exception:
                log.exception("command poll between subscriber cycles failed")


_last_throttled_poll = 0.0


def _poll_commands_throttled(nx: NobitexClient, gh: GithubClient, state: Dict[str, Any], min_gap: float = 1.5) -> None:
    global _last_throttled_poll
    if time.time() - _last_throttled_poll < min_gap:
        return
    _last_throttled_poll = time.time()
    write_heartbeat(state)          # rate-limited inside; keeps "Executor is alive" true during a long pass over many subscribers
    poll_commands(nx, gh, state)


_DAILY_REPORT_HOUR = int(os.environ.get("DAILY_REPORT_HOUR", "22"))


def send_daily_reports() -> None:
    """Once a day (after DAILY_REPORT_HOUR local time) send each opted-in subscriber a short summary."""
    now = time.localtime()
    if now.tm_hour < _DAILY_REPORT_HOUR:
        return
    today = time.strftime("%Y-%m-%d", now)
    for rec in list(_vault.all()):
        try:
            if not rec.daily_report or not rec.public_key_enc or rec.last_report_day == today:
                continue
            st = _u_state_ro(rec.user_id)
            _vault.set_field(rec.user_id, last_report_day=today)
            n_today = sum(1 for h in st.get("trade_history", [])
                          if time.strftime("%Y-%m-%d", time.localtime(float(h.get("closed_at") or 0))) == today)
            text = "\n".join(["📰 گزارش امروز", ui.DIVIDER,
                              f"🏁 معاملات بسته‌شده‌ی امروز: {n_today}",
                              f"{ui.pnl_marker(_today_realized(st))} سود/زیان امروز: {ui.usdt(_today_realized(st), 4)}",
                              f"📈 معاملات باز: {len(st.get('open_trades', {}))}",
                              f"📚 مجموع کل از ابتدا: {ui.usdt(_cumulative_total(st), 4)}"])
            notify_admin(text, chat_id=rec.notify_chat_id or rec.user_id,
                         buttons=[[{"text": "📜 تاریخچه", "callback_data": "cmd:/myhistory"}, {"text": "📈 معاملات باز", "callback_data": "cmd:/mytrades"}],
                                  [{"text": "📋 منوی من", "callback_data": "cmd:/menu"}]])
        except Exception:
            log.exception("daily report failed for %s", getattr(rec, "user_id", "?"))


def manage_subscriptions() -> None:
    """Called every loop tick: flips expired subscriptions (once), and warns
    each user once when 3 days or less remain."""
    for rec in _vault.sweep_expired():
        chat = rec.notify_chat_id or rec.user_id
        open_n = _user_open_count(rec.user_id)
        notify_admin("⏳ اشتراک شما به پایان رسید؛ ورود معاملات جدید متوقف شد."
                     + (f"\nمعاملات بازِ فعلی‌تان ({open_n}) همچنان تا بسته شدن با همان حد ضرر/تارگت/رانر مدیریت می‌شوند."
                        if open_n else "")
                     + "\nبرای ادامه، اشتراک را تمدید کنید 👇", chat_id=chat,
                     buttons=[[{"text": "💳 تمدید اشتراک", "callback_data": "cmd:/plans"}], [{"text": "📋 منوی من", "callback_data": "cmd:/menu"}]])
        notify_admin(f"⏳ اشتراک کاربر {rec.user_id}" + (f" ({rec.display_name})" if rec.display_name else "")
                     + f" منقضی شد ({open_n} معامله‌ی باز).",
                     buttons=[[{"text": "➕ ۳۰ روز تمدید", "callback_data": f"cmd:/extenduser {rec.user_id} 30"},
                               {"text": "👤 پنل کاربر", "callback_data": f"cmd:/user {rec.user_id}"}]])
    for rec in _vault.due_soon(3 * 86400):
        chat = rec.notify_chat_id or rec.user_id
        notify_admin(f"🔔 فقط {rec.days_left():.1f} روز از اشتراک شما مانده. برای اینکه معاملات قطع نشود، همین حالا تمدید کنید 👇", chat_id=chat,
                     buttons=[[{"text": "💳 تمدید اشتراک", "callback_data": "cmd:/plans"}]])
        _vault.mark_notice_sent(rec.user_id)




def _run_as_user(rec, gh: GithubClient, command: str) -> str:
    with _user_context(rec, gh) as (nx_u, st):
        return handle_bot_command(command, st, nx_u, gh)


def _run_as_user_silent(rec, command: str) -> str:
    """Admin oversight read of a subscriber's account: uses their client and
    state, but any message generated along the way must not be copied to the
    subscriber's chat, so the ambient chat routing is switched off (the
    returned text goes to the admin as the command's normal reply)."""
    global _active_user_chat_id, _ui_admin_view
    with _user_context(rec, _gh_main) as (nx_u, st):
        saved, _active_user_chat_id = _active_user_chat_id, None
        _ui_admin_view = True
        try:
            return handle_bot_command(command, st, nx_u, _gh_main)
        finally:
            _active_user_chat_id = saved
            _ui_admin_view = False


_ADMIN_REMAP = {"/mytrades": "/usertrades", "/myhistory": "/userhistory", "/mypnl": "/userpnl", "/myclose": "/userclose",
                "/mycloseall": "/usercloseall", "/mycolall": "/usercolall", "/myliqsafe": "/userliqsafe",
                "/myliqadd": "/userliqadd", "/myconfirmclose": "/userconfirmclose", "/mydismissclose": "/userdismissclose",
                "/mybalance": "/userbalance"}


def _remap_for_admin(result: Any, uid: str) -> Any:
    """A handler run 'as the subscriber' builds SUBSCRIBER buttons (/myclose ...). When the ADMIN is
    the one reading the reply, re-point every button at the matching admin command for that user."""
    def fix_btn(b: dict) -> Optional[dict]:
        cb = str(b.get("callback_data", ""))
        if cb.startswith("cmd:"):
            parts = cb[4:].split(None, 1)
            name = parts[0].lower()
            rest = parts[1] if len(parts) > 1 else ""
            if name == "/menu":
                return {**b, "callback_data": f"cmd:/user {uid}"}
            if name in _ADMIN_REMAP:
                return {**b, "callback_data": f"cmd:{_ADMIN_REMAP[name]} {uid} {rest}".strip()}
            return None
        if cb.startswith("ask:"):
            bits = cb.split(":")
            if bits[1] in ("colallpct", "colallto"):
                return {**b, "callback_data": f"ask:u{bits[1]}:{uid}"}
            return None
        return b
    out = []
    for text, btns in _normalize_result(result):
        if btns:
            rows = btns if isinstance(btns[0], list) else [btns]
            new_rows = [[nb for nb in (fix_btn(x) for x in row) if nb] for row in rows]
            btns = [r for r in new_rows if r] or None
        out.append((f"[کاربر {uid}] " + text if out == [] else text, btns))
    return out


def _trade_close_buttons(uid: str) -> Optional[list]:
    st = _user_states.get(str(uid)) or (load_state(user_state_path(uid)) if os.path.exists(user_state_path(uid)) else {})
    keys = sorted((st.get("open_trades") or {}).keys())
    if not keys:
        return None
    return [[{"text": f"❌ بستن {k}", "callback_data": f"cmd:/myclose {k}"}] for k in keys]


# ---------------------------------------------------------------------------
# Liquidation safety: add collateral (never touches stops / targets)
# ---------------------------------------------------------------------------
LIQ_TARGET_GAPS = ((8, "متعادل (پیشنهادی)"), (15, "امن‌تر"))
LIQ_MAX_ADD_FRACTION = Decimal(os.environ.get("LIQ_MAX_ADD_FRACTION", "1.0"))   # at most +100% of the current collateral per action
_CENT = Decimal("0.01")


def _liq_info(trade: dict, position_data: Optional[dict]) -> Optional[dict]:
    liq = _liquidation_price(position_data)
    if liq is None:
        return None
    try:
        stop = D(trade.get("stop_price"))
    except (InvalidOperation, TypeError):
        return None
    if stop <= 0:
        return None
    long = trade.get("side") == "LONG"
    gap = ((stop - liq) / liq * 100) if long else ((liq - stop) / liq * 100)
    safe = (stop > liq) if long else (stop < liq)
    return {"liq": liq, "gap": gap, "safe": safe, "close": (not safe) or gap < LIQ_GAP_WARN_PCT}


def _liq_buttons(key: str, info: Optional[dict]) -> list:
    if info and info.get("close"):
        return [{"text": "🛡 افزایش فاصله تا لیکویید", "callback_data": _cb("liqsafe", key)}]
    return []


def _extract_bounds(resp: Any) -> tuple:
    """(min, max) collateral from Nobitex's edit-collateral/options answer. The exact shape is
    read defensively: any numeric field whose name contains 'min' / 'max'. (None, None) if unknown."""
    lo = hi = None

    def walk(o):
        nonlocal lo, hi
        if isinstance(o, dict):
            for k, v in o.items():
                kl = str(k).lower()
                if isinstance(v, (dict, list)):
                    walk(v)
                    continue
                try:
                    d = D(v)
                except (InvalidOperation, TypeError, ValueError):
                    continue
                if "max" in kl and hi is None:
                    hi = d
                elif "min" in kl and lo is None:
                    lo = d
        elif isinstance(o, list):
            for x in o:
                walk(x)
    walk(resp)
    return lo, hi


def _new_liq_estimate(side: str, liq: Decimal, liability: Decimal, add: Decimal) -> Decimal:
    """Each extra USDT of collateral moves the liquidation price by 1/liability price units
    (isolated margin). The real value is re-read from Nobitex after the change."""
    if liability <= 0:
        return liq
    delta = add / liability
    return max(Decimal("0"), liq - delta) if side == "LONG" else liq + delta


def _liq_plan(trade: dict, st: dict, free: Decimal, bounds: tuple) -> dict:
    side = trade.get("side")
    liab = D(st.get("liability", "0") or "0")
    col = D(st.get("collateral", "0") or "0")
    liq = _liquidation_price(st)
    stop = D(trade.get("stop_price"))
    cap_add = col * LIQ_MAX_ADD_FRACTION
    if bounds[1] is not None and bounds[1] > col:
        cap_add = min(cap_add, bounds[1] - col)
    elif bounds[1] is not None:
        cap_add = Decimal("0")
    cap_add = (cap_add // _CENT) * _CENT
    reserve = MIN_BALANCE_BUFFER_USDT
    options, seen = [], set()
    if liq is not None and liab > 0 and col > 0:
        for target_gap, label in LIQ_TARGET_GAPS + ((None, "حداکثر افزایش معقول"),):
            if target_gap is None:
                add = cap_add
            else:
                g = Decimal(target_gap)
                target_liq = stop / (1 + g / 100) if side == "LONG" else stop / (1 - g / 100)
                add = ((liq - target_liq) if side == "LONG" else (target_liq - liq)) * liab
                if add <= 0:
                    continue                                   # already at least this safe
                add = min(add.quantize(_CENT, rounding=ROUND_UP), cap_add)
            if add < _CENT or add in seen:
                continue
            seen.add(add)
            nl = _new_liq_estimate(side, liq, liab, add)
            gap = ((stop - nl) / nl * 100) if (side == "LONG" and nl > 0) else (((nl - stop) / nl * 100) if nl > 0 else Decimal("999"))
            options.append({"label": label, "add": add, "new_liq": nl, "new_gap": gap,
                            "affordable": add + reserve <= free})
    return {"options": options, "col": col, "liab": liab, "liq": liq, "cap_add": cap_add, "reserve": reserve}


def cmd_liqsafe(nx: NobitexClient, state: Dict[str, Any], key: str):
    key = key.upper()
    trade = state.get("open_trades", {}).get(key)
    if not trade:
        return "❌ این معامله دیگر باز نیست.", [[_btn_positions(), _btn_menu()]]
    try:
        st = _position_status(nx, trade)
    except TransientAPIError as e:
        return f"⏳ نوبیتکس موقتاً پاسخ نداد ({e}). چند لحظه بعد دوباره امتحان کنید.", [[_btn_positions()]]
    if not _pos_open(st):
        return "ℹ️ این پوزیشن دیگر باز نیست.", [[_btn_positions(), _btn_menu()]]
    if _liquidation_price(st) is None:
        return ("ℹ️ نوبیتکس قیمت لیکویید این پوزیشن را نداده؛ برای همین نمی‌توانم پیشنهاد دقیق بدهم. "
                "می‌توانید از «💵 مبلغ تعهد همه‌ی معاملات» استفاده کنید."), [[_btn_positions(), _btn_menu()]]
    try:
        free = nx.get_margin_usdt_balance()
    except Exception as e:
        return f"⏳ موجودی خوانده نشد ({e}). دوباره امتحان کنید.", [[_btn_positions()]]
    bounds = (None, None)
    try:
        bounds = _extract_bounds(nx.get_edit_collateral_options(int(trade["position_id"])))
    except Exception as e:
        log.info("edit-collateral options unavailable for %s: %s", key, e)
    plan = _liq_plan(trade, st, free, bounds)
    info = _liq_info(trade, st)
    if not plan["options"]:
        if info and not info["close"]:
            return (f"✅ {key}: فاصله‌ی حد ضرر تا لیکویید ٪{info['gap']:.1f} است و از قبل مناسب است؛ نیازی به افزایش وثیقه نیست."), \
                   [[_btn_positions(), _btn_menu()]]
        return (f"ℹ️ {key}: وثیقه‌ی این معامله به سقف مجاز افزایش رسیده یا افزایش بیشتر ممکن نیست."), [[_btn_positions(), _btn_menu()]]
    note = ""
    aff = [o for o in plan["options"] if o["affordable"]]
    if not aff:
        need = plan["options"][0]["add"] + plan["reserve"]
        note = ui.msg_low_balance(need, free, "افزایش وثیقه‌ی این معامله")
    elif len(aff) < len(plan["options"]):
        note = "ℹ️ بعضی گزینه‌ها به‌خاطر موجودی Margin فعلی قابل انجام نیستند و دکمه ندارند."
    text = ui.liq_analysis(key, trade.get("side"), trade.get("entry_actual"), trade.get("stop_price"),
                           plan["liq"], info["gap"] if info else None, plan["col"], trade.get("leverage", "?"),
                           free, plan["options"], note)
    rows = [[{"text": f"➕ {num_btn(o['add'])} USDT — {o['label']}", "callback_data": _cb("liqadd", key, num_btn(o["add"]))}]
            for o in plan["options"] if o["affordable"]]
    rows.append([_btn_positions(), _btn_menu()])
    return text, rows


def num_btn(x: Decimal) -> str:
    return format(Decimal(x).quantize(_CENT, rounding=ROUND_UP), "f")


def _apply_collateral_add(nx: NobitexClient, trade: dict, add: Decimal) -> tuple:
    """Add `add` USDT to this position's collateral. Returns (old_col, new_col, old_liq, new_liq).
    Raises ValueError (Persian, user-safe) on any refusal."""
    st = _position_status(nx, trade)                 # TransientAPIError propagates to the caller
    if not _pos_open(st):
        raise ValueError("این پوزیشن دیگر باز نیست.")
    old_col = D(st.get("collateral", "0") or "0")
    old_liq = _liquidation_price(st)
    if old_col <= 0:
        raise ValueError("وثیقه‌ی فعلی از نوبیتکس خوانده نشد.")
    if add <= 0:
        raise ValueError("مبلغ باید بزرگ‌تر از صفر باشد.")
    if add > old_col * LIQ_MAX_ADD_FRACTION + _CENT:
        raise ValueError(f"برای ایمنی، در هر بار حداکثر {LIQ_MAX_ADD_FRACTION * 100:.0f}٪ وثیقه‌ی فعلی ({num_btn(old_col * LIQ_MAX_ADD_FRACTION)} USDT) اضافه می‌شود.")
    try:
        _, hi = _extract_bounds(nx.get_edit_collateral_options(int(trade["position_id"])))
    except Exception:
        hi = None
    if hi is not None and old_col + add > hi + Decimal("0.0001"):
        raise ValueError(f"نوبیتکس اجازه‌ی وثیقه‌ی بیشتر از {num_btn(hi)} USDT برای این پوزیشن را نمی‌دهد.")
    free = nx.get_margin_usdt_balance()
    if add + MIN_BALANCE_BUFFER_USDT > free:
        raise LowBalance(add + MIN_BALANCE_BUFFER_USDT, free)
    last_err: Optional[NobitexAPIError] = None
    for quant in (Decimal("0.0001"), Decimal("0.01")):
        new_total = (old_col + add).quantize(quant, rounding=ROUND_UP)
        try:
            nx.edit_position_collateral(int(trade["position_id"]), format(new_total, "f"))
            last_err = None
            break
        except NobitexAPIError as e:
            if is_transient_error(e):
                raise TransientAPIError(f"{e.code}: {e.message}") from e
            last_err = e
            txt = f"{e.code} {e.message}".lower()
            if any(w in txt for w in ("precision", "decimal", "invalid", "اعشار")):
                continue
            break
    if last_err is not None:
        txt = f"{last_err.code} {last_err.message}"
        if any(w in txt.lower() for w in ("balance", "insufficient", "موجودی")):
            raise LowBalance(add + MIN_BALANCE_BUFFER_USDT, free)
        raise ValueError(f"نوبیتکس این تغییر را نپذیرفت ({txt}). چیزی تغییر نکرد.")
    st2 = _position_status(nx, trade)
    new_col = D((st2 or {}).get("collateral", "0") or "0") or (old_col + add)
    new_liq = _liquidation_price(st2)
    trade["collateral"] = str(new_col)
    trade["collateral_added"] = str(D(trade.get("collateral_added", "0") or "0") + add)
    return old_col, new_col, old_liq, new_liq


class LowBalance(Exception):
    def __init__(self, need: Decimal, have: Decimal):
        super().__init__(f"need {need}, have {have}")
        self.need, self.have = need, have


def _low_balance_result(nx: NobitexClient, e: "LowBalance", what: str):
    spot = None
    try:
        spot = nx.get_spot_usdt_balance()
    except Exception:
        pass
    return ui.msg_low_balance(e.need, e.have, what, spot), [[_btn_positions(), _btn_menu()]]


def cmd_liqadd(nx: NobitexClient, state: Dict[str, Any], key: str, amount_raw: str):
    key = key.upper()
    trade = state.get("open_trades", {}).get(key)
    if not trade:
        return "❌ این معامله دیگر باز نیست.", [[_btn_positions(), _btn_menu()]]
    try:
        add = D(amount_raw)
    except (InvalidOperation, TypeError):
        return "❌ مبلغ نامعتبر است.", [[_btn_positions()]]
    try:
        old_col, new_col, old_liq, new_liq = _apply_collateral_add(nx, trade, add)
    except LowBalance as e:
        return _low_balance_result(nx, e, "افزایش وثیقه")
    except TransientAPIError as e:
        return f"⏳ نوبیتکس موقتاً پاسخ نداد ({e}). چیزی تغییر نکرد؛ چند لحظه بعد دوباره بزنید.", [[_btn_positions()]]
    except ValueError as e:
        return f"❌ {e}", [[_btn_positions(), _btn_menu()]]
    save_state(state)
    return ui.liq_applied(key, trade.get("side"), add, old_col, new_col, old_liq, new_liq, trade.get("stop_price")), \
        [[_btn_positions(), _btn_menu()]]


def cmd_colall(nx: NobitexClient, state: Dict[str, Any], args: list):
    """Adjust the collateral of ALL open trades at once (increase only - lowering it would bring the
    liquidation price closer). No args = menu. `pct N` / `to X` = preview, add `yes` to apply."""
    trades = state.get("open_trades", {})
    if not trades:
        return "📈 معامله‌ی بازی برای تنظیم وجود ندارد.", [[_btn_positions(), _btn_menu()]]
    try:
        free = nx.get_margin_usdt_balance()
        live = {}
        for k, t in trades.items():
            st = _position_status(nx, t)
            if _pos_open(st):
                live[k] = st
    except TransientAPIError as e:
        return f"⏳ نوبیتکس موقتاً پاسخ نداد ({e}). دوباره امتحان کنید.", [[_btn_positions()]]
    except Exception as e:
        return f"⏳ خواندن اطلاعات ممکن نشد ({e}).", [[_btn_positions()]]
    if not live:
        return "ℹ️ هیچ پوزیشن بازی روی نوبیتکس پیدا نشد.", [[_btn_positions(), _btn_menu()]]
    cols = {k: D(st.get("collateral", "0") or "0") for k, st in live.items()}
    total_col = sum(cols.values(), Decimal("0"))
    reserve = MIN_BALANCE_BUFFER_USDT

    def plan_for(mode: str, val: Decimal) -> dict:
        rows, total_add, skipped = [], Decimal("0"), []
        for k in sorted(live):
            c = cols[k]
            if c <= 0:
                skipped.append((k, "وثیقه‌ی فعلی نامشخص")); continue
            add = (c * val / 100) if mode == "pct" else (val - c)
            if add <= 0:
                skipped.append((k, "وثیقه‌ی فعلی همین‌قدر یا بیشتر است")); continue
            add = add.quantize(_CENT, rounding=ROUND_UP)
            if add < _CENT:
                skipped.append((k, "مبلغ خیلی کوچک")); continue
            if add > c * LIQ_MAX_ADD_FRACTION + _CENT:
                skipped.append((k, f"بیش از {LIQ_MAX_ADD_FRACTION * 100:.0f}٪ وثیقه‌ی فعلی مجاز نیست")); continue
            rows.append((k, add))
            total_add += add
        return {"rows": rows, "total": total_add, "skipped": skipped}

    if not args:
        lines = ["💵 مبلغ تعهد (وثیقه) همه‌ی معاملات", ui.DIVIDER,
                 f"📈 معاملات باز: {len(live)}   |   مجموع وثیقه‌ی فعلی: {ui.num(total_col, 2)} USDT",
                 f"💰 موجودی آزاد Margin: {ui.num(free, 2)} USDT", "",
                 "افزایش وثیقه، قیمت لیکویید را از حد ضرر دورتر می‌کند؛ حد ضرر و تارگت‌ها تغییر نمی‌کنند. "
                 "کاهش وثیقه از این منو ممکن نیست (چون خطر لیکویید را بیشتر می‌کند).", "",
                 "هر گزینه‌ای را بزنید تا اول پیش‌نمایش و مبلغ دقیق را ببینید:"]
        rows_btn = []
        for pct in (10, 25, 50, 100):
            pl = plan_for("pct", Decimal(pct))
            ok = pl["total"] + reserve <= free
            lines.append(f"• +{pct}٪: ≈{ui.num(pl['total'], 2)} USDT " + ("✅" if ok else "❌ موجودی کافی نیست"))
            rows_btn.append({"text": f"+{pct}٪", "callback_data": _cb("colall", "pct", pct)})
        kb = [rows_btn[:2], rows_btn[2:],
              [{"text": "✏️ درصد دلخواه", "callback_data": _ask_cb("colallpct")},
               {"text": "🎯 وثیقه‌ی هر معامله = مبلغ", "callback_data": _ask_cb("colallto")}],
              [_btn_positions(), _btn_menu()]]
        return "\n".join(lines), kb

    mode = args[0].lower()
    if mode not in ("pct", "to") or len(args) < 2:
        return "❌ گزینه‌ی نامعتبر.", [[_btn_positions()]]
    try:
        val = D(args[1])
    except (InvalidOperation, TypeError):
        return "❌ عدد نامعتبر است.", [[_btn_positions()]]
    if val <= 0 or (mode == "pct" and val > LIQ_MAX_ADD_FRACTION * 100):
        return f"❌ درصد باید بین ۱ و {LIQ_MAX_ADD_FRACTION * 100:.0f} باشد.", [[_cb_btn("↩️ برگشت", _cb("colall"))]]
    apply_now = len(args) > 2 and args[2].lower() in ("yes", "تایید")
    pl = plan_for(mode, val)
    what = f"+{val:g}٪ وثیقه" if mode == "pct" else f"وثیقه‌ی هر معامله = {val:g} USDT"
    if not pl["rows"]:
        why = "\n".join(f"• {k}: {r}" for k, r in pl["skipped"])
        return f"ℹ️ با «{what}» هیچ معامله‌ای تغییر نمی‌کند.\n{why}", [[_cb_btn("↩️ برگشت", _cb("colall"))]]
    if pl["total"] + reserve > free:
        pmax = int(((free - reserve) / total_col * 100) // 1) if total_col > 0 else 0
        text = ui.msg_low_balance(pl["total"] + reserve, free, f"«{what}» برای همه‌ی معاملات")
        kb = []
        if mode == "pct" and pmax >= 5:
            kb.append([{"text": f"➕ حداکثر ممکن: +{pmax}٪", "callback_data": _cb("colall", "pct", pmax)}])
        kb.append([_cb_btn("↩️ برگشت", _cb("colall")), _btn_menu()])
        return text, kb
    if not apply_now:
        lines = [f"🧾 پیش‌نمایش: {what}", ui.DIVIDER]
        for k, add in pl["rows"]:
            lines.append(f"• {k}: {ui.num(cols[k], 4)} ⟶ {ui.num(cols[k] + add, 4)} USDT   (+{ui.num(add, 2)})")
        for k, r in pl["skipped"]:
            lines.append(f"• {k}: بدون تغییر ({r})")
        lines += [ui.DIVIDER, f"➕ جمع افزایش: {ui.num(pl['total'], 2)} USDT   |   💰 موجودی بعد از آن: ≈{ui.num(free - pl['total'], 2)} USDT",
                  "حد ضرر و تارگت‌ها هیچ تغییری نمی‌کنند. تأیید می‌کنید؟"]
        return "\n".join(lines), [[{"text": "✅ بله، اعمال کن", "callback_data": _cb("colall", mode, args[1], "yes")}],
                                  [_cb_btn("↩️ برگشت", _cb("colall")), _btn_menu()]]
    done, failed = [], []
    for k, add in pl["rows"]:
        tr = trades.get(k)
        if not tr:
            continue
        try:
            oc, nc, ol, nl = _apply_collateral_add(nx, tr, add)
            save_state(state)
            done.append((k, add, nc, nl))
        except LowBalance as e:
            failed.append((k, f"موجودی کافی نیست (لازم {ui.num(e.need, 2)}، موجود {ui.num(e.have, 2)} USDT)"))
            break
        except TransientAPIError as e:
            failed.append((k, f"نوبیتکس موقتاً پاسخ نداد ({e})"))
        except ValueError as e:
            failed.append((k, str(e)))
    lines = ["✅ نتیجه‌ی تنظیم مبلغ تعهد همه‌ی معاملات", ui.DIVIDER]
    for k, add, nc, nl in done:
        lines.append(f"• {k}: +{ui.num(add, 2)} → وثیقه {ui.num(nc, 4)}" + (f"  |  لیکویید ≈ {ui.price(nl)}" if nl else ""))
    for k, r in failed:
        lines.append(f"• ⚠️ {k}: {r}")
    for k, r in pl["skipped"]:
        lines.append(f"• {k}: بدون تغییر ({r})")
    lines.append(ui.DIVIDER)
    lines.append(f"➕ جمع اضافه‌شده: {ui.num(sum((a for _, a, _, _ in done), Decimal('0')), 2)} USDT")
    if any("موجودی کافی نیست" in r for _, r in failed):
        lines.append("💰 موجودی Margin کم است؛ بعد از شارژ، همین دکمه را دوباره بزنید (معاملات انجام‌شده دوباره افزایش نمی‌یابند مگر بخواهید).")
    return "\n".join(lines), [[_btn_positions(), _btn_menu()]]


def _cb_btn(text: str, cb: str) -> dict:
    return {"text": text, "callback_data": cb}


# ---------------------------------------------------------------------------
# Reports: open trades / history / P&L
# ---------------------------------------------------------------------------
def _live_positions_by_id(nx: NobitexClient) -> Dict[int, dict]:
    out: Dict[int, dict] = {}
    try:
        for p in nx.list_positions(status="active").get("positions", []):
            if p.get("id") is not None:
                out[int(p["id"])] = p
    except Exception as e:
        log.warning("list_positions failed for /positions: %s", e)
    return out


def cmd_positions(nx: NobitexClient, state: Dict[str, Any]):
    trades = state.get("open_trades", {})
    if not trades:
        n_live = 0
        try:
            n_live = len([p for p in nx.list_positions(status="active").get("positions", [])
                          if str(p.get("status", "")).lower() == "open"])
        except Exception:
            pass
        if n_live and _ui_uid is None:
            return (f"📈 هیچ معامله‌ای در state نیست، ولی نوبیتکس {n_live} پوزیشن باز دارد.\n"
                    f"برای برگرداندن آن‌ها زیر نظر Executor و ثبت دوباره‌ی محافظت، «بازیابی» را بزنید."), \
                   [[{"text": "♻️ بازیابی معاملات", "callback_data": "cmd:/recover"}], [_btn_menu()]]
        return "📈 الان معامله‌ی بازی ندارید.\nهر سیگنال جدید کانال خودکار اجرا می‌شود.", \
               [[_btn_history(), _btn_pnl()], [_btn_menu()]]
    live_by_id = _live_positions_by_id(nx)
    cards, rows = [], []
    realized_total = Decimal("0")
    unreal_total = Decimal("0")
    for key, t in sorted(trades.items()):
        live = live_by_id.get(int(t.get("position_id", 0) or 0), {})
        realized = _realized_so_far(t)
        realized_total += realized
        try:
            unreal_total += D(live.get("unrealizedPNL", "0") or "0")
        except (InvalidOperation, TypeError):
            pass
        info = _liq_info(t, live)
        col = live.get("collateral")
        cards.append(ui.card_position(key, t, live, _stop_vs_liquidation_note(t, live), realized=realized,
                                      collateral=col, liq_price=info["liq"] if info else None))
        row = [_btn_close(key)]
        row += _liq_buttons(key, info)
        rows.append(row)
    footer = ui.positions_footer(realized_total, unreal_total, _cumulative_total(state))
    pages = ui.split_cards(cards, footer)
    kb = list(rows)
    kb.append([{"text": "❌ بستن همه‌ی معاملات", "callback_data": _cb("closeall")},
               {"text": "💵 مبلغ تعهد همه‌ی معاملات", "callback_data": _cb("colall")}])
    kb.append([{"text": "🔄 بروزرسانی", "callback_data": ("cmd:/positions" if _ui_uid is None else (f"cmd:/usertrades {_ui_uid}" if _ui_admin_view else "cmd:/mytrades"))},
               _btn_history(), _btn_menu()])
    out = [(pg, None) for pg in pages[:-1]] + [(pages[-1], kb)]
    return out if len(out) > 1 else out[0]


def _history_with_running_totals(state: Dict[str, Any]) -> list:
    """[(entry, cumulative_total_after_it)] oldest -> newest (the running total
    starts from the trimmed-away base so it stays the true all-time figure)."""
    cum = D(state.get("history_base_total", "0") or "0")
    out = []
    for h in state.get("trade_history") or []:
        v = _hist_value(h)
        if v is not None:
            cum += v
        out.append((h, cum))
    return out


def cmd_history(state: Dict[str, Any], page: int = 1):
    rows = _history_with_running_totals(state)
    if not rows:
        return "📜 هنوز هیچ معامله‌ی بسته‌شده‌ای در تاریخچه نیست.", [[_btn_positions(), _btn_menu()]]
    newest_first = list(reversed(rows))
    total_n = len(newest_first)
    blocks = [ui.history_block(total_n - i, h, cum) for i, (h, cum) in enumerate(newest_first)]
    pages = ui.history_pages(blocks)
    page = max(1, min(int(page), len(pages)))
    text = ui.history_text(pages[page - 1], page, len(pages), total_n, rows[-1][1])
    base = "cmd:/history" if _ui_uid is None else (f"cmd:/userhistory {_ui_uid}" if _ui_admin_view else "cmd:/myhistory")
    nav = []
    if page > 1:
        nav.append({"text": "◀️ جدیدتر", "callback_data": f"{base} {page - 1}"})
    if page < len(pages):
        nav.append({"text": "قدیمی‌تر ▶️", "callback_data": f"{base} {page + 1}"})
    kb = ([nav] if nav else []) + [[_btn_pnl(), _btn_positions()], [_btn_menu()]]
    return text, kb


def cmd_pnl(state: Dict[str, Any], n: int = 100):
    rows = _history_with_running_totals(state)
    if not rows:
        return "📊 هنوز هیچ معامله‌ی بسته‌شده‌ای برای محاسبه نیست.", [[_btn_positions(), _btn_menu()]]
    sample = rows[-n:]
    total = Decimal("0"); wins = losses = flat = counted = n_exact = 0
    best = worst = None
    days: Dict[str, list] = {}
    for h, cum in sample:
        v = _hist_value(h)
        if v is None:
            continue
        if h.get("realized_usdt") is not None:
            n_exact += 1
        counted += 1
        total += v
        wins += v > 0
        losses += v < 0
        flat += v == 0
        best = v if best is None or v > best else best
        worst = v if worst is None or v < worst else worst
        day = time.strftime("%Y-%m-%d", time.localtime(float(h.get("closed_at") or 0)))
        d = days.setdefault(day, [0, Decimal("0"), Decimal("0")])
        d[0] += 1
        d[1] += v
        d[2] = cum
    if counted == 0:
        return f"📊 از {len(sample)} معامله، برای هیچ‌کدام سود/زیان قابل محاسبه نبود.", [[_btn_history(), _btn_menu()]]
    day_lines = [(k, v[0], v[1], v[2]) for k, v in sorted(days.items())][-14:]
    today_key = time.strftime("%Y-%m-%d")
    today = days.get(today_key, [0, Decimal("0")])[1] if today_key in days else Decimal("0")
    text = ui.pnl_report(len(sample), total, wins, losses, flat, n_exact, counted, best, worst, day_lines,
                         rows[-1][1], skipped=len(sample) - counted, today=today)
    return text, [[_btn_history(), _btn_positions()], [_btn_menu()]]


def _today_realized(state: Dict[str, Any]) -> Decimal:
    tk = time.strftime("%Y-%m-%d")
    total = Decimal("0")
    for h in state.get("trade_history") or []:
        try:
            if time.strftime("%Y-%m-%d", time.localtime(float(h.get("closed_at") or 0))) != tk:
                continue
        except (TypeError, ValueError):
            continue
        v = _hist_value(h)
        if v is not None:
            total += v
    return total


# ---------------------------------------------------------------------------
# Subscriber experience (everything is button driven)
# ---------------------------------------------------------------------------
_USER_CMD_MAP = {"/mytrades": "/positions", "/myhistory": "/history", "/mypnl": "/pnl"}


def _u_menu_btn() -> dict:
    return {"text": "📋 منوی من", "callback_data": "cmd:/menu"}


def _u_state_ro(uid: str) -> Dict[str, Any]:
    st = _user_states.get(str(uid))
    if st is not None:
        return st
    p = user_state_path(uid)
    return load_state(p) if os.path.exists(p) else {}


def _user_stage(rec) -> str:
    if rec.status == us.STATUS_BLOCKED:
        return "blocked"
    if not rec.terms_accepted_at:
        return "terms"
    if rec.public_key_enc:
        return "ready"
    if rec.status == us.STATUS_PENDING and rec.pending_days > 0:
        return "connect"
    return "plans"


def _active_discount_for(rec):
    """(DiscountCode, '') if this subscriber has a currently usable code entered, else (None, reason).
    A code that went stale (expired/disabled/used up) is dropped from the user so it can never
    silently change a price they were shown."""
    if rec is None or _discounts is None:
        return None, ""
    code = _discounts.user_code(rec.user_id)
    if not code:
        return None, ""
    dc, why = _discounts.check(code, rec.user_id, _ledger)
    if dc is None:
        _discounts.clear_user_code(rec.user_id)
        return None, why
    return dc, ""


def _plans_screen(rec=None, banner: str = ""):
    tiers = _pricing.all() if _pricing else {}
    card_ok = bool(_payment_info and _payment_info.is_card_set())
    usdt_ok = bool(_payment_info and _payment_info.is_usdt_set())
    dc, stale_why = _active_discount_for(rec)
    disc = None
    if dc is not None:
        prices = {}
        for days, p in tiers.items():
            if not dc.applies_to(days):
                continue
            prices[days] = {c: us.apply_discount(float(p[c]), dc.percent, c) for c in ("toman", "usdt") if p.get(c) is not None}
        disc = {"code": dc.code, "percent": dc.percent, "prices": prices, "all_plans": not dc.plans}
    text = ui.msg_plans(tiers, card_ok, usdt_ok, _payment_info.usdt_network if _payment_info else "", disc)
    if stale_why:
        text = f"ℹ️ کد تخفیف قبلی‌تان برداشته شد: {stale_why}\n\n" + text
    if banner:
        text = banner + "\n\n" + text
    rows = []
    if rec is not None and _discounts is not None:
        if dc is not None:
            rows.append([{"text": f"🎟 {dc.code} ({dc.percent:g}٪) — حذف کد", "callback_data": "cmd:/cleardiscount"}])
        else:
            rows.append([{"text": "🎟 کد تخفیف دارم", "callback_data": "ask:discount"}])
    for days, p in tiers.items():
        row = []
        dp = (disc or {}).get("prices", {}).get(days, {})
        if p.get("toman") is not None and card_ok:
            lab = f"📅 {days} روز — {int(dp['toman']):,} تومان 🎟" if "toman" in dp else f"📅 {days} روز — {int(p['toman']):,} تومان"
            row.append({"text": lab, "callback_data": f"cmd:/subscribe {days} toman"})
        if p.get("usdt") is not None and usdt_ok:
            lab = f"📅 {days} روز — {float(dp['usdt']):g} USDT 🎟" if "usdt" in dp else f"📅 {days} روز — {float(p['usdt']):g} USDT"
            row.append({"text": lab, "callback_data": f"cmd:/subscribe {days} usdt"})
        for i in range(0, len(row), 1):
            rows.append([row[i]])
    if rec is None or not rec.public_key_enc:
        rows.append([_signup_btn()])          # not connected yet = may not have a Nobitex account at all
    rows.append([{"text": "🆘 پرسش از پشتیبانی", "callback_data": "ask:support"}])
    if rec is not None and rec.public_key_enc:
        rows.append([_u_menu_btn()])
    return text, rows


def _connect_screen():
    return ui.CONNECT_GUIDE, [[{"text": "🔑 شروع اتصال (وارد کردن کلید API)", "callback_data": "ask:apikey"}],
                              [_signup_btn()],
                              [{"text": "🆘 پشتیبانی", "callback_data": "ask:support"}]]


def _terms_screen(page: int, accepted: bool):
    pages = ui.TERMS_PAGES
    page = max(1, min(page, len(pages)))
    text = pages[page - 1] + (f"\n\n{ui.DIVIDER}\n(صفحه {page} از {len(pages)})")
    if page < len(pages):
        rows = [[{"text": "➡️ ادامه‌ی شرایط", "callback_data": f"cmd:/terms {page + 1}"}]]
    else:
        text += "\n" + ui.TERMS_FOOTER
        rows = [[{"text": "✅ می‌پذیرم", "callback_data": "cmd:/accept"}, {"text": "❌ نمی‌پذیرم", "callback_data": "cmd:/decline"}]]
        if page > 1:
            rows.append([{"text": "⬅️ صفحه‌ی قبل", "callback_data": f"cmd:/terms {page - 1}"}])
    if accepted:
        rows = [[{"text": "📋 منوی من", "callback_data": "cmd:/menu"}]]
    return text, rows


def _user_menu(rec, banner: str = ""):
    uid = rec.user_id
    st = _u_state_ro(uid)
    bal = None
    if rec.public_key_enc:
        try:
            bal = _get_user_client(rec).get_margin_usdt_balance()
        except Exception:
            bal = None
    days_left = rec.days_left() if rec.status == us.STATUS_ACTIVE else None
    text = ui.msg_main_menu(ui.status_fa(rec.status), days_left, len(st.get("open_trades", {})), rec.paused, bal,
                            _today_realized(st) if st.get("trade_history") else None, rec.display_name)
    if rec.status == us.STATUS_EXPIRED:
        text = ui.msg_expired_hint() + "\n\n" + text
    elif rec.status == us.STATUS_SUSPENDED:
        text = "⏸ حساب شما توسط ادمین معلق شده؛ ورود معامله‌ی جدید متوقف است. برای اطلاع با پشتیبانی تماس بگیرید.\n\n" + text
    if bal is not None and bal < Decimal("3") and rec.status == us.STATUS_ACTIVE:
        text += "\n\n⚠️ موجودی Margin شما کم است؛ برای اینکه سیگنال‌ها اجرا شوند، USDT به کیف پول تعهدی اضافه کنید."
    if banner:
        text = banner + "\n\n" + text
    rows = [
        [{"text": "📈 معاملات باز", "callback_data": "cmd:/mytrades"}, {"text": "📜 تاریخچه", "callback_data": "cmd:/myhistory"}],
        [{"text": "📊 سود و زیان", "callback_data": "cmd:/mypnl"}, {"text": "💰 موجودی من", "callback_data": "cmd:/mybalance"}],
        [{"text": "💵 مبلغ تعهد همه‌ی معاملات", "callback_data": "cmd:/mycolall"}, {"text": "❌ بستن همه‌ی معاملات", "callback_data": "cmd:/mycloseall"}],
        [{"text": "⚙️ تنظیمات", "callback_data": "cmd:/mysettings"}, {"text": "🔔 اشتراک من", "callback_data": "cmd:/mysubscription"}],
        [{"text": "▶️ ازسرگیری ورود" if rec.paused else "⏸ توقف ورود جدید", "callback_data": "cmd:/myresume" if rec.paused else "cmd:/mypause"},
         {"text": "💳 تمدید اشتراک", "callback_data": "cmd:/plans"}],
        [{"text": "🆘 پشتیبانی", "callback_data": "ask:support"}, {"text": "📖 راهنما", "callback_data": "cmd:/help"}],
        [{"text": "📜 شرایط استفاده", "callback_data": "cmd:/terms"}, {"text": "🔌 قطع اتصال", "callback_data": "cmd:/disconnect"}],
    ]
    return text, rows


def _stage_screen(rec):
    stage = _user_stage(rec)
    if stage == "blocked":
        return ui.msg_blocked(), [[{"text": "🆘 پشتیبانی", "callback_data": "ask:support"}]]
    if stage == "terms":
        return _terms_screen(1, False)
    if stage == "plans":
        return _plans_screen(rec)
    if stage == "connect":
        t, b = _connect_screen()
        return f"🎁 اشتراک {rec.pending_days:.0f} روزه‌ی شما آماده است و با اتصال حساب شروع می‌شود.\n\n" + t, b
    return _user_menu(rec)


def _settings_screen(rec):
    text = ui.msg_settings({
        "risk": rec.risk_usdt, "cap_risk": rec.cap_risk_usdt, "collateral": rec.max_collateral_usdt,
        "cap_collateral": rec.cap_collateral_usdt, "leverage": rec.leverage, "cap_leverage": rec.cap_leverage,
        "max_open": rec.max_open_trades, "auto_exit": rec.auto_exit, "daily_loss": rec.daily_loss_usdt,
        "daily_report": rec.daily_report})
    if rec.cap_request:
        text += "\n\n⏳ یک درخواست افزایش سقف شما در انتظار تأیید ادمین است."
    rows = [
        [{"text": "🎯 ریسک هر معامله", "callback_data": "cmd:/myvalue risk"}, {"text": "💵 سقف وثیقه", "callback_data": "cmd:/myvalue col"}],
        [{"text": "📈 اهرم", "callback_data": "cmd:/myvalue lev"}, {"text": "🔢 حداکثر معاملات", "callback_data": "cmd:/myvalue max"}],
        [{"text": ("🤖 خروج خودکار: خاموش کن" if rec.auto_exit else "🤖 خروج خودکار: روشن کن"),
          "callback_data": "cmd:/myautoexit " + ("off" if rec.auto_exit else "on")}],
        [{"text": "🛑 سقف ضرر روزانه", "callback_data": "cmd:/myvalue daily"},
         {"text": ("📰 گزارش روزانه: خاموش" if rec.daily_report else "📰 گزارش روزانه: روشن"),
          "callback_data": "cmd:/mydailyreport " + ("off" if rec.daily_report else "on")}],
        [_u_menu_btn()],
    ]
    return text, rows


_VALUE_FIELDS = {
    "risk": ("🎯 ریسک هر معامله (حداکثر ضرر تا حد ضرر)", "risk_usdt", "cap_risk_usdt", (0.25, 0.5, 1, 2, 5, 10, 25), "USDT",
             "هر چه کمتر، ضرر هر معامله کمتر."),
    "col": ("💵 سقف وثیقه‌ی هر معامله", "max_collateral_usdt", "cap_collateral_usdt", (1, 1.25, 1.5, 2, 5, 10, 25, 50), "USDT",
            "حداکثر پولی که برای هر معامله به‌عنوان وثیقه قفل می‌شود."),
    "lev": ("📈 اهرم", "leverage", "cap_leverage", (2, 3, 5, 10, 20), "x", "اهرم بالاتر یعنی فاصله‌ی لیکویید کمتر."),
    "max": ("🔢 حداکثر معاملات همزمان", "max_open_trades", "cap_max_open_trades", (1, 2, 3, 5, 10, 15, 20), "", "بیشتر از این، سیگنال‌ها نادیده گرفته می‌شوند."),
    "daily": ("🛑 سقف ضرر روزانه", "daily_loss_usdt", None, (0, 1, 2, 5, 10, 25), "USDT",
              "وقتی ضرر امروز به این عدد برسد تا فردا ورود جدید متوقف می‌شود؛ ۰ = خاموش."),
}
_VALUE_ASK = {"risk": "setrisk", "col": "setcol", "lev": "setlev", "max": "setmax", "daily": "setdaily"}


def _value_screen(rec, field: str):
    if field not in _VALUE_FIELDS:
        return _settings_screen(rec)
    title_fa, attr, cap_attr, presets, unit, hint = _VALUE_FIELDS[field]
    cur = getattr(rec, attr)
    cap = getattr(rec, cap_attr) if cap_attr else None
    hint2 = hint + (f"\nسقف مجاز شما: {cap} {unit}" if cap is not None else "")
    text = ui.msg_value_menu(title_fa, f"{cur} {unit}".strip(), hint2)
    vals = [v for v in presets if cap is None or float(v) <= float(cap)]
    if cap is not None and float(cap) not in [float(v) for v in vals]:
        vals.append(float(cap))
    btns = [{"text": (f"{v:g}" + (f" {unit}" if unit else "")) if v != 0 else "خاموش", "callback_data": f"cmd:/myset {field} {v:g}"} for v in vals]
    rows = [btns[i:i + 3] for i in range(0, len(btns), 3)]
    rows.append([{"text": "✏️ عدد دلخواه", "callback_data": f"ask:{_VALUE_ASK[field]}"}])
    if cap_attr and not rec.cap_request:
        rows.append([{"text": "📨 درخواست افزایش سقف از ادمین", "callback_data": f"ask:reqcap:{field}"}])
    rows.append([{"text": "↩️ تنظیمات", "callback_data": "cmd:/mysettings"}, _u_menu_btn()])
    return text, rows


def _apply_user_value(rec, uid: str, field: str, raw: str):
    if field not in _VALUE_FIELDS:
        return "❌ گزینه‌ی نامعتبر.", [[_u_menu_btn()]]
    try:
        v = D(raw)
    except (InvalidOperation, TypeError, ValueError):
        return "❌ لطفاً فقط عدد بفرستید.", _value_screen(rec, field)[1]
    if v < 0 or (v == 0 and field != "daily"):
        return "❌ عدد باید بزرگ‌تر از صفر باشد.", _value_screen(rec, field)[1]
    if field == "daily":
        _vault.set_field(uid, daily_loss_usdt=str(v.normalize() if v != 0 else 0))
        return ("✅ سقف ضرر روزانه خاموش شد." if v == 0 else f"✅ سقف ضرر روزانه: {v:g} USDT ثبت شد."), _settings_screen(_vault.get(uid))[1]
    kwargs = {"risk": "risk_usdt", "col": "max_collateral_usdt", "lev": "leverage", "max": "max_open_trades"}
    try:
        _vault.user_set_values(uid, **{kwargs[field]: format(v.normalize(), "f")})
    except us.CapExceeded as e:
        return ui.msg_cap_exceeded(e.name_fa, e.value, e.cap), [
            [{"text": "📨 ارسال درخواست به ادمین", "callback_data": f"cmd:/reqcap {field} {e.value}"}],
            [{"text": f"✅ همین سقف ({e.cap}) را بگذار", "callback_data": f"cmd:/myset {field} {e.cap}"}],
            [{"text": "↩️ تنظیمات", "callback_data": "cmd:/mysettings"}]]
    except ValueError as e:
        return f"❌ {e}", _value_screen(rec, field)[1]
    return "✅ ثبت شد؛ از معامله‌ی بعدی اعمال می‌شود.", _settings_screen(_vault.get(uid))[1]


def _subscription_screen(rec):
    hist = _ledger.for_user(rec.user_id) if _ledger else []
    pl = []
    for p in hist[-5:]:
        d = time.strftime("%Y-%m-%d", time.localtime(p.requested_at))
        st_fa = {"pending": "⏳ در انتظار", "confirmed": "✅ تأیید", "rejected": "❌ رد/لغو"}.get(p.status, p.status)
        pl.append(f"  {p.payment_id} [{d}] {p.days} روز — {_fmt_amt(p.amount, p.currency)} — {st_fa}")
    text = ui.msg_subscription({
        "status": rec.status, "paused": rec.paused, "days_left": rec.days_left(),
        "expires_date": time.strftime("%Y-%m-%d", time.localtime(rec.expires_at)) if rec.expires_at else "؟",
        "pending_days": rec.pending_days, "connected": bool(rec.public_key_enc)}, _user_open_count(rec.user_id), pl)
    return text, [[{"text": "💳 تمدید اشتراک", "callback_data": "cmd:/plans"}], [_u_menu_btn()]]


def handle_self_service_command(cmd: str, parts: list, state: Dict[str, Any], nx: NobitexClient,
                                 caller_chat_id: str):
    """The fixed command surface a subscriber's own chat can reach (see SELF_SERVICE_COMMANDS /
    the gate in handle_bot_command). `nx`/`state` are the ADMIN's and are never used here: every
    action touching a subscriber's Nobitex account goes through _user_context() with THEIR OWN
    client and state. Returns (text, buttons) or a list of such messages."""
    if _vault is None:
        return "⏳ سرویس هنوز آماده نیست؛ کمی بعد دوباره امتحان کنید.", [[_u_menu_btn()]]
    gh = _gh_main
    uid = caller_chat_id
    rec = _vault.get(uid)
    meta = _caller_meta or {}

    # ---- first contact: create the record, then follow the person through the steps ----
    if rec is None:
        if cmd not in ("/start", "/help", "/menu", "/terms", "/accept", "/decline", "/plans", "/prices"):
            cmd = "/start"
        rec = _vault.register_lead(uid, meta.get("first_name") or "", meta.get("username") or "")
    else:
        if (meta.get("username") and rec.username != meta.get("username")) or (meta.get("first_name") and not rec.display_name):
            _vault.set_field(uid, username=meta.get("username") or rec.username,
                             display_name=rec.display_name or meta.get("first_name") or "")
            rec = _vault.get(uid)

    if rec.status == us.STATUS_BLOCKED:
        if cmd == "/support":
            pass
        else:
            return ui.msg_blocked(), [[{"text": "🆘 پشتیبانی", "callback_data": "ask:support"}]]

    if cmd == "/start":
        if rec.terms_accepted_at and rec.public_key_enc:
            notify_admin("👋 خوش برگشتید! منوی کامل شما پایین است.", chat_id=uid, kb="user")
            return _user_menu(rec)
        first = ui.msg_welcome_new(meta.get("first_name") or rec.display_name)
        text, rows = _stage_screen(rec)
        return [(first, [[_signup_btn()]]), (text, rows)]

    if cmd == "/help":
        return ui.msg_help_user(), [[_u_menu_btn()], [_signup_btn()], [{"text": "🆘 پشتیبانی", "callback_data": "ask:support"}]]

    if cmd == "/menu":
        return _stage_screen(rec)

    if cmd == "/terms":
        page = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 1
        return _terms_screen(page, bool(rec.terms_accepted_at) and page == 1 and len(parts) == 1)

    if cmd == "/decline":
        return ui.msg_declined(), [[{"text": "📜 دوباره شرایط را بخوانم", "callback_data": "cmd:/terms"}]]

    if cmd == "/accept":
        first_accept = not rec.terms_accepted_at
        if first_accept:
            _vault.accept_terms(uid)
            rec = _vault.get(uid)
        if first_accept and not rec.lead_notified and rec.status == us.STATUS_NEW:
            _vault.set_field(uid, lead_notified=True)
            notify_admin(ui.card_new_user_admin(uid, rec.display_name, rec.username), buttons=[
                [{"text": "🎁 ۷ روز اشتراک", "callback_data": f"cmd:/grant {uid} 7"},
                 {"text": "🎁 ۳۰ روز", "callback_data": f"cmd:/grant {uid} 30"}],
                [{"text": "⛔ مسدود", "callback_data": f"cmd:/block {uid}"}, {"text": "👤 پنل کاربر", "callback_data": f"cmd:/user {uid}"}],
                [{"text": "📋 منو", "callback_data": "menu"}]])
        text, rows = _stage_screen(rec)
        return [("✅ شرایط ثبت شد. ممنون از اعتمادتان 🙏", None), (text, rows)]

    if cmd in ("/plans", "/prices"):
        if not rec.terms_accepted_at:
            return _terms_screen(1, False)
        return _plans_screen(rec)

    if cmd == "/connectguide":
        stage = _user_stage(rec)
        return _connect_screen() if stage == "connect" else _stage_screen(rec)

    if cmd == "/support":
        text = " ".join(parts[1:]).strip()
        if not text:
            return "✍️ پیام‌تان را بنویسید.", [[{"text": "🆘 نوشتن پیام", "callback_data": "ask:support"}]]
        notify_admin(ui.card_support_admin(uid, rec.display_name, text), buttons=[
            [{"text": "✍️ پاسخ به کاربر", "callback_data": f"ask:reply:{uid}"}, {"text": "👤 پنل کاربر", "callback_data": f"cmd:/user {uid}"}],
            [{"text": "📋 منو", "callback_data": "menu"}]])
        return "✅ پیام شما برای پشتیبانی ارسال شد. پاسخ همین‌جا برایتان می‌آید.", [[_u_menu_btn()]]

    if cmd == "/subscribe" and len(parts) > 2:
        if not rec.terms_accepted_at:
            return _terms_screen(1, False)
        try:
            days = int(parts[1])
        except ValueError:
            return _plans_screen(rec)
        currency = parts[2].lower()
        tier = _pricing.price_for(days) if _pricing else None
        amount = tier.get(currency) if (tier and currency in ("toman", "usdt")) else None
        if amount is None:
            return "❌ این تعرفه دیگر موجود نیست.", _plans_screen(rec)[1]
        if currency == "toman" and not (_payment_info and _payment_info.is_card_set()):
            return "❌ اطلاعات کارت هنوز ثبت نشده؛ لطفاً با پشتیبانی هماهنگ کنید.", [[{"text": "🆘 پشتیبانی", "callback_data": "ask:support"}]]
        if currency == "usdt" and not (_payment_info and _payment_info.is_usdt_set()):
            return "❌ آدرس USDT هنوز ثبت نشده؛ لطفاً با پشتیبانی هماهنگ کنید.", [[{"text": "🆘 پشتیبانی", "callback_data": "ask:support"}]]
        open_reqs = [p for p in _ledger.for_user(uid) if p.status == us.STATUS_PENDING_PAY and not p.note]
        if len(open_reqs) >= 3:
            rows = [[{"text": f"✅ ارسال رسید {p.payment_id} ({p.days} روز)", "callback_data": f"ask:receipt:{p.payment_id}"},
                     {"text": "🗑 لغو", "callback_data": f"cmd:/cancelpay {p.payment_id}"}] for p in open_reqs]
            return ("❌ چند درخواست پرداخت بدون رسید دارید. اول برای آن‌ها رسید بفرستید یا لغوشان کنید:"), rows
        list_amount = float(amount)
        d_code, d_pct = "", 0.0
        if _discounts is not None and _discounts.user_code(uid):
            ucode = _discounts.user_code(uid)
            dc, why = _discounts.check(ucode, uid, _ledger, days)
            if dc is None:
                # Never charge a different price than the one on screen: tell them and let them decide.
                if "اعمال نمی" not in why:
                    held = [p for p in _ledger.for_user(uid) if p.code == ucode and p.status != us.STATUS_REJECTED]
                    if held and "قبلاً" in why:
                        h = held[-1]
                        _discounts.clear_user_code(uid)
                        return _plans_screen(rec, f"ℹ️ هر کد تخفیف فقط یک‌بار قابل استفاده است و کد {ucode} همین الان روی درخواست پرداخت {h.payment_id} "
                                                  f"({h.days} روزه) نشسته. همان را پرداخت کنید، یا اول لغوش کنید (🗑) تا بتوانید کد را روی اشتراک دیگری بزنید. "
                                                  f"چیزی ثبت نشد.")
                    _discounts.clear_user_code(uid)
                    return _plans_screen(rec, f"ℹ️ کد تخفیف شما دیگر قابل استفاده نیست: {why}\nقیمت‌ها بدون تخفیف نمایش داده شد. چیزی ثبت نشد.")
                # code just doesn't cover this plan length: this plan is simply full price
            else:
                amount = us.apply_discount(list_amount, dc.percent, currency)
                d_code, d_pct = dc.code, float(dc.percent)
        pay = _ledger.request(uid, days, currency, float(amount), code=d_code,
                              list_amount=(list_amount if d_code else 0.0), discount_percent=d_pct)
        text = ui.msg_pay_instructions(pay.payment_id, days, currency, float(amount),
                                       _payment_info.card_number, _payment_info.card_holder,
                                       _payment_info.usdt_address, _payment_info.usdt_network,
                                       code=d_code, list_amount=(list_amount if d_code else None), percent=d_pct)
        return text, [[{"text": "✅ پرداخت کردم — ارسال رسید", "callback_data": f"ask:receipt:{pay.payment_id}"}],
                      [{"text": "🗑 لغو این درخواست", "callback_data": f"cmd:/cancelpay {pay.payment_id}"}],
                      [{"text": "↩️ تعرفه‌ها", "callback_data": "cmd:/plans"}]]

    if cmd == "/usediscount":
        if not rec.terms_accepted_at:
            return _terms_screen(1, False)
        if _discounts is None:
            return "❌ کد تخفیف فعلاً در دسترس نیست.", _plans_screen(rec)[1]
        code = us.normalize_code(parts[1]) if len(parts) > 1 else ""
        if not code:
            return _plans_screen(rec, "❌ کد تخفیف معتبر نیست. فقط حروف انگلیسی و عدد، مثلاً EID20.")
        dc, why = _discounts.check(code, uid, _ledger)
        if dc is None:
            return _plans_screen(rec, f"❌ {why}")
        _discounts.set_user_code(uid, dc.code)
        scope = "روی همه‌ی اشتراک‌ها" if not dc.plans else "روی اشتراک‌های " + "، ".join(f"{d} روزه" for d in dc.plans)
        return _plans_screen(rec, f"✅ کد {dc.code} اعمال شد: {dc.percent:g}٪ تخفیف {scope}.\nمبلغ‌های تخفیف‌خورده را پایین ببینید و اشتراک را انتخاب کنید 👇")

    if cmd == "/cleardiscount":
        if _discounts is not None:
            _discounts.clear_user_code(uid)
        return _plans_screen(rec, "🗑 کد تخفیف حذف شد.")

    if cmd == "/cancelpay" and len(parts) > 1:
        try:
            _ledger.cancel_by_user(parts[1], uid)
        except (KeyError, ValueError) as e:
            return f"ℹ️ {e if isinstance(e, ValueError) else 'این درخواست پیدا نشد.'}", _plans_screen(rec)[1]
        return "🗑 درخواست لغو شد.", _plans_screen(rec)[1]

    if cmd == "/paid" and len(parts) > 2:
        pay = _ledger.get(parts[1]) if _ledger else None
        if not pay or pay.user_id != str(uid):
            return "❌ چنین درخواست پرداختی برای شما پیدا نشد.", _plans_screen(rec)[1]
        note = " ".join(parts[2:])[:300]
        try:
            _ledger.set_note(parts[1], note)
        except ValueError as e:
            return f"❌ {e}", [[_u_menu_btn()]]
        notify_admin(ui.card_payment_admin(pay.payment_id, uid, rec.display_name, pay.days, pay.amount, pay.currency, note,
                                          code=pay.code, list_amount=pay.list_amount, percent=pay.discount_percent),
                     buttons=[[{"text": "✅ تأیید پرداخت", "callback_data": f"cmd:/confirmpayment {pay.payment_id}"},
                               {"text": "❌ رد", "callback_data": f"cmd:/rejectpayment {pay.payment_id}"}],
                              [{"text": "✍️ رد با ذکر دلیل", "callback_data": f"ask:rejectreason:{pay.payment_id}"}],
                              [{"text": "👤 پنل کاربر", "callback_data": f"cmd:/user {uid}"}, {"text": "📋 منو", "callback_data": "menu"}]])
        return ui.msg_receipt_saved(pay.payment_id), [[_u_menu_btn()], [{"text": "🆘 پشتیبانی", "callback_data": "ask:support"}]]

    if cmd == "/connect_enc" and len(parts) > 2:
        stage = _user_stage(rec)
        if stage in ("terms", "plans", "blocked"):
            return _stage_screen(rec)
        try:
            pub = us.decrypt_field(parts[1])
            priv = us.decrypt_field(parts[2])
        except RuntimeError as e:
            return f"❌ {e}", [[{"text": "🔁 دوباره تلاش", "callback_data": "ask:apikey"}]]
        retry = [[{"text": "🔁 دوباره وارد می‌کنم", "callback_data": "ask:apikey"}],
                 [{"text": "📖 راهنمای ساخت کلید", "callback_data": "cmd:/connectguide"}, {"text": "🆘 پشتیبانی", "callback_data": "ask:support"}]]
        candidate = NobitexClient(NobitexConfig(public_key=pub, private_key_b64=priv,
                                                 base_url=NOBITEX_BASE_URL, public_base_url=NOBITEX_PUBLIC_BASE_URL))
        ok, detail = us.validate_key_permissions(candidate)
        if not ok:
            return f"❌ کلید پذیرفته نشد.\n{detail}\n\nاگر برای کلید محدودیت IP گذاشته‌اید، آدرس سرور ربات را از پشتیبانی بگیرید.", retry
        try:
            candidate.get_margin_usdt_balance()
        except Exception as e:
            return (f"❌ مجوز کلید درست بود ولی اتصال آزمایشی به کیف پول تعهدی شکست خورد.\n"
                    f"مطمئن شوید معاملات تعهدی (Margin) در حساب نوبیتکس‌تان فعال است.\n({e})"), retry
        first_time = rec.status == us.STATUS_PENDING
        _vault.connect_keys(uid, pub, priv)
        rec = _vault.get(uid)
        _vault.set_field(uid, notify_chat_id=uid)
        rec = _vault.get(uid)
        _user_clients.pop(uid, None)
        _user_states.pop(uid, None)
        _user_key_health.pop(uid, None)
        try:
            os.makedirs(USER_STATES_DIR, exist_ok=True)
            if first_time or not os.path.exists(user_state_path(uid)):
                _get_user_state(rec, gh)   # creates + seeds "start from now" (never replays channel history)
        except Exception:
            log.exception("could not initialise state for subscriber %s", uid)
        notify_admin(f"✅ کاربر {uid}" + (f" ({rec.display_name})" if rec.display_name else "") +
                     f" حسابش را متصل کرد ({detail}).", buttons=[[{"text": "👤 پنل کاربر", "callback_data": f"cmd:/user {uid}"}, {"text": "📋 منو", "callback_data": "menu"}]])
        bal = None
        try:
            bal = candidate.get_margin_usdt_balance()
        except Exception:
            pass
        notify_admin(ui.msg_connected(rec.days_left(), bal) if rec.status == us.STATUS_ACTIVE
                     else "✅ کلید متصل شد، ولی اشتراک فعال نیست؛ برای شروع اشتراک تهیه کنید.", chat_id=uid, kb="user")
        return _user_menu(rec)

    if cmd in ("/mysubscription", "/mystatus"):
        return _subscription_screen(rec)

    # ---- everything below needs a connected key ----
    need_key = ("/myconfirmclose", "/mydismissclose", "/mytrades", "/myhistory", "/mypnl", "/myclose", "/mycloseall", "/mypause", "/myresume", "/mybalance",
                "/myliqsafe", "/myliqadd", "/mycolall", "/disconnect", "/myautoexit", "/mydailyreport",
                "/mysettings", "/myvalue", "/myset", "/myrisk", "/reqcap")
    if cmd in need_key and not rec.public_key_enc and cmd != "/disconnect":
        text, rows = _stage_screen(rec)
        return [(ui.msg_no_key_yet(), None), (text, rows)]

    if cmd == "/mysettings":
        return _settings_screen(rec)
    if cmd == "/myvalue" and len(parts) > 1:
        return _value_screen(rec, parts[1])
    if cmd == "/myset" and len(parts) > 2:
        return _apply_user_value(rec, uid, parts[1], parts[2])
    if cmd == "/myrisk":
        if len(parts) == 4:
            try:
                _vault.user_set_risk(uid, parts[1], parts[2], parts[3])
            except ValueError as e:
                return f"❌ {e}", _settings_screen(rec)[1]
            return "✅ ثبت شد؛ برای معاملات جدید اعمال می‌شود.", _settings_screen(_vault.get(uid))[1]
        return _settings_screen(rec)

    if cmd == "/reqcap" and len(parts) > 2:
        field, val = parts[1], parts[2]
        cap_fields = {"risk": "risk_usdt", "col": "max_collateral_usdt", "lev": "leverage", "max": "max_open_trades"}
        if field not in cap_fields:
            return "❌ گزینه‌ی نامعتبر.", [[_u_menu_btn()]]
        try:
            v = D(val)
            if v <= 0:
                raise InvalidOperation
        except (InvalidOperation, TypeError, ValueError):
            return "❌ عدد نامعتبر است.", _value_screen(rec, field)[1]
        if field == "lev" and v > MAX_LEVERAGE_CAP:
            return f"❌ حداکثر اهرم قابل‌تنظیم در سیستم {MAX_LEVERAGE_CAP}x است.", _value_screen(rec, field)[1]
        if rec.cap_request:
            return "⏳ یک درخواست قبلی شما هنوز در انتظار ادمین است؛ منتظر نتیجه‌اش بمانید.", [[_u_menu_btn()]]
        req = {cap_fields[field]: format(v.normalize(), "f")}
        _vault.set_field(uid, cap_request=req)
        cur = {"risk_usdt": rec.cap_risk_usdt, "max_collateral_usdt": rec.cap_collateral_usdt,
               "leverage": rec.cap_leverage, "max_open_trades": str(rec.cap_max_open_trades)}
        notify_admin(ui.card_cap_request_admin(uid, rec.display_name, req, cur), buttons=[
            [{"text": "✅ تأیید", "callback_data": f"cmd:/capapprove {uid}"}, {"text": "❌ رد", "callback_data": f"cmd:/caprefuse {uid}"}],
            [{"text": "👤 پنل کاربر", "callback_data": f"cmd:/user {uid}"}, {"text": "📋 منو", "callback_data": "menu"}]])
        return "📨 درخواست شما برای ادمین ارسال شد؛ نتیجه همین‌جا اعلام می‌شود.", [[_u_menu_btn()]]

    if cmd == "/myautoexit":
        if len(parts) < 2 or parts[1].lower() not in ("on", "off"):
            return _settings_screen(rec)
        _vault.set_auto_exit(uid, parts[1].lower() == "on")
        return ("🤖 خروج خودکار طبق کانال " + ("روشن شد ✅" if parts[1].lower() == "on" else "خاموش شد ⏸ (بستن با خودتان است)")), \
            _settings_screen(_vault.get(uid))[1]

    if cmd == "/mydailyreport":
        on = len(parts) > 1 and parts[1].lower() == "on"
        _vault.set_field(uid, daily_report=on)
        return ("📰 گزارش روزانه " + ("روشن شد ✅" if on else "خاموش شد ⏸")), _settings_screen(_vault.get(uid))[1]

    if cmd == "/mypause":
        _vault.set_paused(uid, True)
        return ("⏸ ورود معاملات جدید متوقف شد.\nمعاملات بازِ فعلی‌تان همچنان مدیریت می‌شوند."), \
               [[{"text": "▶️ ازسرگیری ورود", "callback_data": "cmd:/myresume"}], [_u_menu_btn()]]
    if cmd == "/myresume":
        _vault.set_paused(uid, False)
        return (("▶️ ورود معاملات جدید دوباره فعال شد." if rec.is_active() else
                 "▶️ ثبت شد، ولی اشتراک شما فعال نیست؛ برای ورود جدید باید تمدید کنید.")), [[_u_menu_btn()]]

    if cmd == "/disconnect":
        if not rec.public_key_enc:
            return "ℹ️ الان کلیدی متصل نیست.", _stage_screen(rec)[1]
        if _user_open_count(uid) > 0:
            return ("❌ معامله‌ی باز دارید؛ اگر کلید را حذف کنم محافظت آن‌ها قطع می‌شود.\n"
                    "اول معاملات را ببندید یا صبر کنید تا بسته شوند."), \
                   [[{"text": "📈 معاملات باز", "callback_data": "cmd:/mytrades"}], [_u_menu_btn()]]
        if not (len(parts) > 1 and parts[1] in ("yes", "تایید")):
            return ui.msg_confirm("قطع اتصال حساب نوبیتکس",
                                  "کلید API شما از سیستم پاک می‌شود و ورود معامله‌ی جدید متوقف می‌شود.\n"
                                  "روزهای باقی‌مانده‌ی اشتراک حفظ می‌شود و هر وقت خواستید دوباره وصل می‌شوید."), \
                   [[{"text": "🔌 بله، قطع کن", "callback_data": "cmd:/disconnect yes"}], [_u_menu_btn()]]
        _vault.disconnect(uid)
        _user_clients.pop(uid, None)
        _user_states.pop(uid, None)
        notify_admin(f"🔌 کاربر {uid} کلیدش را قطع کرد.", buttons=[[{"text": "👤 پنل کاربر", "callback_data": f"cmd:/user {uid}"}]])
        notify_admin("🔌 اتصال قطع شد.", chat_id=uid, kb="remove")
        rec = _vault.get(uid)
        return _stage_screen(rec)

    if cmd == "/mycloseall":
        n_open = _user_open_count(uid)
        if not n_open:
            return "📈 معامله‌ی بازی برای بستن ندارید.", [[_u_menu_btn()]]
        if not (len(parts) > 1 and parts[1] in ("yes", "تایید")):
            return ui.msg_confirm("بستن همه‌ی معاملات",
                                  f"همه‌ی {n_open} معامله‌ی بازتان با قیمت بازار و به‌صورت کامل بسته می‌شود. "
                                  "این کار برگشت‌پذیر نیست."), \
                   [[{"text": "❌ بله، همه را ببند", "callback_data": "cmd:/mycloseall yes"}],
                    [{"text": "↩️ لغو", "callback_data": "cmd:/mytrades"}]]
        return _run_as_user(rec, gh, "/closeall yes")

    # ---- account-level commands executed as the subscriber ----
    if cmd == "/mytrades":
        return _run_as_user(rec, gh, "/positions")
    if cmd == "/myhistory":
        return _run_as_user(rec, gh, "/history" + (" " + parts[1] if len(parts) > 1 and parts[1].isdigit() else ""))
    if cmd == "/mypnl":
        return _run_as_user(rec, gh, "/pnl")
    if cmd == "/mybalance":
        return _run_as_user(rec, gh, "/balance")
    if cmd == "/myclose" and len(parts) > 1:
        return _run_as_user(rec, gh, f"/close {parts[1].upper()}")
    if cmd == "/myliqsafe" and len(parts) > 1:
        return _run_as_user(rec, gh, f"/liqsafe {parts[1].upper()}")
    if cmd == "/myliqadd" and len(parts) > 2:
        return _run_as_user(rec, gh, f"/liqadd {parts[1].upper()} {parts[2]}")
    if cmd == "/mycolall":
        return _run_as_user(rec, gh, "/colall" + ("" if len(parts) == 1 else " " + " ".join(parts[1:4])))
    if cmd in ("/myconfirmclose", "/mydismissclose") and len(parts) > 1:
        return _run_as_user(rec, gh, ("/confirmclose " if cmd == "/myconfirmclose" else "/dismissclose ") + parts[1].upper())

    text, rows = _stage_screen(rec)
    return [("❓ این گزینه پیدا نشد؛ منوی درست را می‌فرستم:", None), (text, rows)]


def handle_bot_command(command: str, state: Dict[str, Any], nx: NobitexClient, gh: GithubClient,
                        caller_chat_id: Optional[str] = None) -> str:
    """Admin bot command handler. Returns a user-visible Persian report.

    Takes the SAME state object the main loop holds in memory (not its own
    load_state() snapshot) - using a separate copy here was a real bug: a
    command's change (e.g. /ack popping a key from needs_protection) would
    be written to disk, then silently overwritten moments later by the main
    loop's next save of its own, unaware, long-lived in-memory state -
    making acknowledged/closed items reappear as if nothing had happened.

    caller_chat_id: None means this arrived via the admin-only relay path
    (every pre-existing call site, and bridge.py's ADMIN_CHAT_ID path) - full
    command set. A non-None value means a multi-tenant subscriber's own chat
    sent this (bridge.py tags it); only SELF_SERVICE_COMMANDS are reachable,
    checked here independently of whatever bridge.py already filtered -
    never trust a single layer for something that gates other users' money
    and exchange keys.
    """
    if caller_chat_id is not None:
        cmd0 = command.strip().split()[0].lower() if command.strip() else ""
        if cmd0 not in SELF_SERVICE_COMMANDS:
            return "❌ این دستور فقط برای ادمین است."
    try:
        parts=command.strip().split()
        cmd=parts[0].lower() if parts else ""
        if caller_chat_id is not None:
            return handle_self_service_command(cmd, parts, state, nx, str(caller_chat_id))
        if cmd in ("/status", "/config"):
            c = load_control(gh)
            bal = nx.get_margin_usdt_balance()
            txt = (f"📊 وضعیت سیستم\n{ui.DIVIDER}\nExecutor: 🟢 آنلاین (نسخه {EXECUTOR_BUILD})\n"
                   f"ورود معامله‌ی جدید: {'🟢 فعال' if c['enabled'] else '🔴 متوقف'}\n"
                   f"🎯 ریسک هر معامله: {c['risk_usdt']} USDT\n💵 سقف وثیقه‌ی هر معامله: {c['max_collateral_usdt']} USDT\n"
                   f"📈 اهرم: {c.get('leverage', DEFAULT_LEVERAGE)}x\n🔢 حداکثر معاملات همزمان: {c['max_open_trades']}\n"
                   f"🤖 خروج خودکار طبق کانال: {'روشن' if _auto_exit_enabled() else 'خاموش'}\n{ui.DIVIDER}\n"
                   f"📈 معاملات باز: {len(state.get('open_trades', {}))}\n"
                   f"⏳ در انتظار محافظت: {len(state.get('pending_protection', {}))}   |   🚨 محافظت ناقص: {len(state.get('needs_protection', {}))}   |   "
                   f"🛑 در حال بستن کامل: {len(state.get('needs_flatten', {}))}\n"
                   f"📡 در انتظار تأیید بستن: {len(state.get('pending_close_confirm', {}))}\n"
                   f"💰 موجودی آزاد Margin: {bal} USDT\n"
                   f"👥 کاربران: {len(_vault.all()) if _vault else 0}   |   💳 پرداخت در انتظار: {len(_ledger.pending()) if _ledger else 0}")
            return txt, [
                [{"text": "📈 معاملات باز", "callback_data": "cmd:/positions"}, {"text": "💰 موجودی", "callback_data": "cmd:/balance"}],
                [{"text": "⏸ توقف ورود" if c["enabled"] else "▶️ فعال‌سازی ورود", "callback_data": "cmd:/pause" if c["enabled"] else "cmd:/resume"},
                 {"text": "🤖 خروج خودکار", "callback_data": "cmd:/autoexit"}],
                [{"text": "🔄 همگام‌سازی", "callback_data": "cmd:/reconcile"}, {"text": "🛡 بررسی محافظت", "callback_data": "cmd:/protection"}],
                [{"text": "📋 منو", "callback_data": "menu"}]]
        if cmd == "/balance":
            m = nx.get_margin_usdt_balance()
            sp = nx.get_spot_usdt_balance()
            txt = (f"💰 موجودی USDT\n{ui.DIVIDER}\n📊 کیف پول تعهدی (Margin): {ui.num(m, 4)} USDT\n💼 کیف پول اسپات: {ui.num(sp, 4)} USDT")
            if _ui_uid is not None and m < Decimal("3"):
                txt += "\n\n⚠️ موجودی Margin کم است؛ برای اجرای سیگنال‌ها به کیف پول تعهدی USDT اضافه کنید."
            return txt, [[_btn_positions(), _btn_pnl()], [_btn_menu()]]
        if cmd == "/positions":
            return cmd_positions(nx, state)
        if cmd == "/history":
            return cmd_history(state, int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 1)
        if cmd == "/pnl":
            return cmd_pnl(state, int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 100)
        if cmd == "/liqsafe" and len(parts) > 1:
            return cmd_liqsafe(nx, state, parts[1])
        if cmd == "/liqadd" and len(parts) > 2:
            return cmd_liqadd(nx, state, parts[1], parts[2])
        if cmd == "/colall":
            return cmd_colall(nx, state, parts[1:4])
        if cmd=="/pause":
            c=load_control(gh); c["enabled"]=False; save_control(gh,c); return "⏸ ورود معاملات جدید متوقف شد. پوزیشن‌های باز دست‌نخورده می‌مانند."
        if cmd=="/resume":
            c=load_control(gh); c["enabled"]=True; save_control(gh,c); return "▶️ ورود معاملات جدید فعال شد."
        if cmd=="/risk" and len(parts)>1:
            v=D(parts[1]);
            if v<=0: return "❌ Risk باید بزرگ‌تر از 0 باشد."
            c=load_control(gh); c["risk_usdt"]=str(v); save_control(gh,c); return f"✅ Risk cap به {v} USDT تغییر کرد."
        if cmd=="/collateral" and len(parts)>1:
            v=D(parts[1]);
            if v<=0: return "❌ Max collateral باید بزرگ‌تر از 0 باشد."
            c=load_control(gh); c["max_collateral_usdt"]=str(v); save_control(gh,c); return f"✅ سقف مارجین هر معامله به {v} USDT تغییر کرد."
        if cmd=="/maxtrades" and len(parts)>1:
            v=int(parts[1]);
            if v<1: return "❌ Max trades باید حداقل 1 باشد."
            c=load_control(gh); c["max_open_trades"]=v; save_control(gh,c); return f"✅ حداکثر معاملات همزمان: {v}"
        if cmd=="/leverage" and len(parts)>1:
            v=D(parts[1]);
            if v<1 or v>MAX_LEVERAGE_CAP: return f"❌ Leverage باید بین 1 و {MAX_LEVERAGE_CAP} باشد."
            c=load_control(gh); c["leverage"]=str(v); save_control(gh,c)
            return (f"✅ اهرم ترجیحی به {v}x تغییر کرد و همین عدد مستقیماً به نوبیتکس درخواست می‌شود.\n"
                    f"اگر نوبیتکس برای بازاری خاص واقعاً اهرم پایین‌تری اعمال کند، Executor آن را از پاسخ "
                    f"واقعی بعد از باز شدن معامله می‌خواند و در همان پیام تأیید نشان می‌دهد.")
        if cmd=="/ack" and len(parts)>1:
            k=parts[1].strip().upper()
            removed=[]
            if k in state.get("pending_protection", {}):
                state["pending_protection"].pop(k, None); removed.append("pending_protection")
            if k in state.get("needs_protection", {}):
                state["needs_protection"].pop(k, None); removed.append("needs_protection")
            if removed:
                save_state(state)
                return (f"✅ {k} به‌عنوان بررسی‌شده ثبت شد و از صف پیگیری خودکار ({', '.join(removed)}) حذف شد.\n"
                        f"⚠️ توجه: این فقط پیگیری/هشدار خودکار Executor را متوقف می‌کند؛ اگر مطمئن نیستید، لطفاً "
                        f"وضعیت واقعی این پوزیشن را خودتان یک‌بار در نوبیتکس بررسی کنید.")
            return f"ℹ️ موردی با کلید {k} در صف پیگیری خودکار (pending_protection/needs_protection) پیدا نشد."
        if cmd=="/fixprotection" and len(parts)>1:
            k=parts[1].strip().upper()
            return attempt_protection_repair(nx, state, k)
        if cmd=="/confirmclose" and len(parts)>1:
            k=parts[1].strip().upper()
            pc=state.get("pending_close_confirm", {}).pop(k, None)
            save_state(state)
            if not pc:
                return f"ℹ️ درخواست بستنی برای {k} در انتظار تصمیم نبود."
            trade=state.get("open_trades", {}).get(k)
            if not trade:
                return f"ℹ️ {k} دیگر در معاملات باز نیست."
            kind=pc.get("kind")
            price_raw=pc.get("price")
            if kind in ("breakeven","sl_after_t2","sl_after_t3") and price_raw:
                _close_remaining_at_price(nx, state, k, kind, D(price_raw))
            else:
                _close_remaining_market(nx, state, k, kind)
            return f"✅ بستن {k} طبق تأیید شما ({kind}) اجرا شد."
        if cmd=="/dismissclose" and len(parts)>1:
            k=parts[1].strip().upper()
            if k in state.get("pending_close_confirm", {}):
                state["pending_close_confirm"].pop(k, None)
                save_state(state)
                return (f"↩️ پیشنهاد بستن {k} از طرف کانال رد شد (طبق تصمیم شما).\n"
                        f"⚠️ این معامله هنوز باز است؛ حد ضرر/تارگت‌های قبلی‌اش دست‌نخورده روی نوبیتکس فعال ماندند. "
                        f"اگر می‌خواهید خودتان همین الان کامل ببندیدش، از دکمه/فرمان بستن دستی (/close {k}) استفاده کنید.")
            return f"ℹ️ درخواست بستنی برای {k} در انتظار تصمیم نبود."
        if cmd=="/test_api":
            markets=nx.get_margin_markets().get("markets",{}); bal=nx.get_margin_usdt_balance(); return f"🧪 API OK\nMargin markets: {len(markets)}\nMargin USDT: {bal}"
        if cmd in ("/reconcile", "/sync"):
            reconcile_on_startup(nx,state)
            _rec, _rec_btn = recover_orphan_positions(nx, gh, state, only_known=True)
            if _rec:
                notify_admin(_rec, buttons=_rec_btn)
            return "🔄 Reconciliation اجرا شد؛ گزارش کامل آن در تلگرام ارسال شد."
        if cmd == "/recover":
            pids = None
            if len(parts) > 1:
                try:
                    pids = {int(x) for x in parts[1:]}
                except ValueError:
                    return "❌ شناسه‌ی پوزیشن باید عدد باشد، مثل: /recover 119056412"
            _rec, _rec_btn = recover_orphan_positions(nx, gh, state, only_known=False, position_ids=pids)
            if _rec:
                notify_admin(_rec, buttons=_rec_btn)
                return "♻️ نتیجه‌ی بررسی بازیابی در پیام جداگانه‌ی بالا ارسال شد."
            return "ℹ️ چیزی برای گزارش نبود."
        if cmd == "/ack_orphan" and len(parts) > 1:
            try:
                pid = int(parts[1])
            except ValueError:
                return "❌ شناسه‌ی پوزیشن باید عدد باشد."
            rec = state.get("unmatched_orphans", {}).get(str(pid))
            if not rec:
                return f"ℹ️ پوزیشن #{pid} در صف پیگیری پوزیشن‌های بی‌صاحب نبود."
            rec["acked"] = True
            save_state(state)
            return (f"✅ #{pid} به‌عنوان بررسی‌شده ثبت شد؛ دیگر خودکار دوباره هشدار داده نمی‌شود.\n"
                    f"⚠️ توجه: این فقط هشدار خودکار را متوقف می‌کند و پوزیشن روی نوبیتکس باز و بدون محافظت این Executor باقی می‌ماند؛ "
                    f"اگر لازم شد با /recover {pid} یا ❌ بستن دوباره تصمیم بگیرید.")
        if cmd == "/closeorphan" and len(parts) > 1:
            try:
                pid = int(parts[1])
            except ValueError:
                return "❌ شناسه‌ی پوزیشن باید عدد باشد."
            return _close_raw_position(nx, state, pid)
        if cmd == "/openlate" and len(parts) > 1:
            key_late = parts[1].upper()
            rec = state.get("pending_late_signal", {}).get(key_late)
            if not rec:
                return f"ℹ️ سیگنال دیرکردی برای {key_late} در صف نیست (شاید قبلاً تصمیم گرفته شده)."
            sig = ParsedSignal(kind="entry", symbol=rec["symbol"], timeframe=rec["timeframe"], side=rec["side"],
                                entry=float(rec["entry"]), stop=float(rec["stop"]),
                                targets={int(k): float(v) for k, v in rec["targets"].items()})
            setattr(sig, "signal_id", rec.get("signal_id"))
            setattr(sig, "source_update_id", rec.get("source_update_id", ""))
            try:
                handle_signal(nx, state, sig, gh)
                state.get("pending_late_signal", {}).pop(key_late, None)
                save_state(state)
                return f"✅ {key_late}: با تأیید شما و قیمت فعلی بازار باز شد (جزئیات در پیام‌های بالا)."
            except SignalFinalized as e:
                state.get("pending_late_signal", {}).pop(key_late, None)
                save_state(state)
                return f"ℹ️ {key_late}: {e}"
            except TransientAPIError as e:
                return f"⏳ {key_late}: نوبیتکس موقتاً پاسخ نداد ({e}); دوباره /openlate {key_late} را بزنید."
            except (NobitexAPIError, ValueError, InvalidOperation) as e:
                return f"❌ {key_late}: باز کردن شکست خورد: {e}"
        if cmd == "/dismisslate" and len(parts) > 1:
            key_late = parts[1].upper()
            if state.get("pending_late_signal", {}).pop(key_late, None) is not None:
                save_state(state)
                return f"❌ {key_late}: نادیده گرفته شد؛ هیچ معامله‌ای باز نشد."
            return f"ℹ️ سیگنال دیرکردی برای {key_late} در صف نیست."
        if cmd == "/protect" and len(parts) >= 6:
            # Manually attach full Executor protection (stop + 4 weighted
            # targets + trailing runner, exactly like any bot-opened trade) to
            # ANY open Nobitex position by its id - including ones opened by
            # hand outside the bot, or an orphan with no matching channel
            # signal. Usage: /protect <position_id> <stop> <t1> <t2> <t3> <t4>
            try:
                pid = int(parts[1])
                stop_p = D(parts[2]); t1, t2, t3, t4 = (D(x) for x in parts[3:7])
            except (ValueError, InvalidOperation, IndexError):
                return "❌ فرمت درست: /protect <position_id> <stop> <t1> <t2> <t3> <t4>"
            if pid in _tracked_position_ids(state):
                return f"⚠️ پوزیشن #{pid} از قبل در state ردیابی می‌شود؛ برای مدیریتش از /positions استفاده کنید."
            try:
                status = nx.get_position_status(pid).get("position")
            except NobitexAPIError as e:
                if e.code in ("HTTP404", "NotFound") or e.http_status == 404:
                    return f"❌ پوزیشن #{pid} روی نوبیتکس پیدا نشد."
                return f"⏳ #{pid}: نوبیتکس موقتاً پاسخ نداد ({e}); دوباره امتحان کنید."
            if not status or str(status.get("status", "")).lower() != "open":
                return f"❌ پوزیشن #{pid} باز نیست."
            side = "LONG" if str(status.get("side", "")).lower() == "buy" else "SHORT"
            symbol = str(status.get("srcCurrency", "")).upper()
            entry = D(status.get("entryPrice", "0"))
            if entry <= 0:
                return f"❌ #{pid}: قیمت ورود نامعتبر است."
            key_m = f"{symbol}_MANUAL"
            suffix = 2
            while key_m in state["open_trades"]:
                key_m = f"{symbol}_MANUAL{suffix}"; suffix += 1
            trade = {
                "symbol": symbol, "timeframe": "MANUAL", "side": side, "position_id": pid,
                "entry_signal": str(entry), "entry_actual": str(entry),
                "original_stop": str(stop_p), "stop_price": str(stop_p),
                "risk_unit": str(abs(entry - stop_p)),
                "initial_amount": str(status.get("liability", "0")), "initial_amount_requested": str(status.get("liability", "0")),
                "leverage": str(status.get("leverage", "5")),
                "targets": {}, "runner": {}, "client_prefix": f"tc-{key_m[:8]}-manual",
                "signal_id": "MANUAL", "closed_pct": "0", "last_trailing_check": 0,
                "peak": str(entry), "trailing_active": False, "opened_at": time.time(), "recovered": True,
            }
            for n, (pct, tp) in enumerate([(W1, t1), (W2, t2), (W3, t3), (W4, t4)], 1):
                trade["targets"][str(n)] = {"target": n, "pct": str(pct), "tp_price": str(tp), "hit": False,
                                            "tp_order_id": None, "sl_order_id": None}
            state["open_trades"][key_m] = trade
            save_state(state)
            try:
                notes = _rebuild_protection(nx, trade, stop_p, state)
                _verify_trade_protection(nx, trade)
                save_state(state)
                return f"✅ {key_m} (#{pid}) با محافظت کامل (حد ضرر + ۴ تارگت + رانر) ثبت شد." + ("\n" + "\n".join(notes) if notes else "")
            except TransientAPIError as e:
                return f"⏳ {key_m} (#{pid}) به state اضافه شد؛ ثبت محافظت به‌خاطر محدودیت نوبیتکس به تلاش خودکار بعدی موکول شد ({e})."
            except Exception as e:
                _flag_protection_issue(nx, state, key_m, trade, f"manual /protect failed: {e}", quiet=True)
                return f"⚠️ {key_m} (#{pid}) به state اضافه شد ولی محافظت کامل نشد ({e}); تلاش خودکار ادامه دارد."
        if cmd == "/protecthelp" and len(parts) > 1:
            try:
                pid = int(parts[1])
                status = nx.get_position_status(pid).get("position") or {}
            except Exception:
                status = {}
            entry = status.get("entryPrice", "؟")
            return (f"🛡 برای ثبت محافظت دستی روی پوزیشن #{pid} (ورود≈{entry}) این را با قیمت‌های خودتان تکمیل و ارسال کنید:\n"
                    f"/protect {pid} <stop> <t1> <t2> <t3> <t4>\n\n"
                    f"مثال: /protect {pid} {entry} {entry} {entry} {entry} {entry}\n"
                    f"وزن‌ها همیشه ثابت است: T1=20٪، T2=30٪، T3=15٪، T4=10٪، رانر=25٪ (مثل بقیه‌ی معاملات).")
        if cmd in ("/protection", "/audit"):
            return audit_open_trade_protection(nx, state)
        if cmd=="/logs":
            try:
                tail = Path("executor.log").read_text(encoding="utf-8", errors="replace").splitlines()[-25:]
                return "🧾 آخرین لاگ‌ها\n" + ("\n".join(tail) if tail else "(خالی)")
            except Exception as e:
                return f"❌ خواندن لاگ ناموفق بود: {e}"
        if cmd == "/close" and len(parts) > 1:
            key_to_close = parts[1].upper()
            if key_to_close not in state.get("open_trades", {}):
                return f"ℹ️ معامله‌ی {key_to_close} دیگر باز نیست.", [[_btn_positions(), _btn_menu()]]
            done = _close_remaining_market(nx, state, key_to_close, "user_close" if _ui_uid and not _ui_admin_view else "admin_close")
            if done:
                return f"✅ {key_to_close} کامل بسته شد؛ گزارش سطر به سطر در پیام جداگانه آمده است.", [[_btn_positions(), _btn_history()], [_btn_menu()]]
            return (f"⏳ بستن {key_to_close} هنوز کامل نشده (نوبیتکس موقتاً اجازه نداد). ربات هر چند ثانیه دوباره تلاش می‌کند "
                    f"و تا بسته شدن کامل ول نمی‌کند؛ نتیجه اعلام می‌شود."), [[_btn_positions(), _btn_menu()]]
        if cmd == "/closeall":
            keys = sorted(state.get("open_trades", {}).keys())
            # Resting limit entries of forwarded signals (admin account only) must not fill AFTER the
            # admin asked to close everything, so they are cancelled together with the trades.
            pend_keys = (sorted(state.get("pending_entries", {}).keys()) + sorted(state.get("fwd_retry", {}).keys())) if _ui_uid is None else []
            if not keys and not pend_keys:
                return "📈 معامله‌ی بازی برای بستن نیست.", [[_btn_history(), _btn_menu()]]
            if not (len(parts) > 1 and parts[1] in ("yes", "تایید")):
                extra_p = f"\nسفارش‌های ورود در انتظار ({', '.join(pend_keys)}) هم لغو می‌شوند." if pend_keys else ""
                return ui.msg_confirm("بستن همه‌ی معاملات", f"همه‌ی {len(keys)} معامله‌ی باز ({', '.join(keys)}) با قیمت بازار و کامل بسته می‌شود." + extra_p), \
                       [[{"text": "❌ بله، همه را ببند", "callback_data": _cb("closeall", "yes")}], [{"text": "↩️ لغو", "callback_data": ("menu" if _ui_uid is None else _btn_positions()["callback_data"])}]]
            ok_n = 0
            for pk in pend_keys:
                if pk in state.get("fwd_retry", {}):
                    state["fwd_retry"].pop(pk, None)
                    save_state(state)
                    continue
                prec = state.get("pending_entries", {}).get(pk)
                if prec:
                    _cancel_entry_and_settle(nx, state, pk, prec, "بستن همه‌ی معاملات")
            keys = sorted(set(keys) | set(state.get("open_trades", {}).keys()))     # a partly-filled entry may have just become a trade
            for k in keys:
                if k in state.get("open_trades", {}):
                    if _close_remaining_market(nx, state, k, "user_close_all" if _ui_uid and not _ui_admin_view else "admin_close_all"):
                        ok_n += 1
            left = len(state.get("open_trades", {}))
            return (f"✅ {ok_n} از {len(keys)} معامله کامل بسته شد." + (f"\n⏳ {left} معامله هنوز در حال بسته‌شدن است؛ ربات تا تمام شدن ادامه می‌دهد." if left else "")
                    + "\nگزارش هر معامله در پیام جداگانه آمده است."), [[_btn_history(), _btn_pnl()], [_btn_menu()]]

        # ---------------- multi-tenant user management (admin-only) ----------------
        if cmd == "/adduser" and len(parts) >= 3:
            try:
                uid = _safe_uid(parts[1])
                days = float(parts[2])
                if days <= 0:
                    raise ValueError
            except ValueError:
                return "❌ شناسه باید عددی و تعداد روز مثبت باشد.", [[_cb_btn("👥 کاربران", "cmd:/listusers"), _cb_btn("📋 منو", "menu")]]
            name = " ".join(parts[3:])
            existing = _vault.get(uid)
            if existing and existing.public_key_enc:
                return (f"⚠️ کاربر {uid} از قبل ثبت و متصل است. برای افزودن زمان از «تمدید» در پنل او استفاده کنید."), \
                       [[_cb_btn("👤 پنل کاربر", f"cmd:/user {uid}")]]
            if existing and existing.status not in (us.STATUS_PENDING, us.STATUS_NEW):
                return (f"⚠️ کاربر {uid} از قبل ثبت شده ({existing.status}). از پنل او تمدید کنید."), [[_cb_btn("👤 پنل کاربر", f"cmd:/user {uid}")]]
            _vault.add_pending(uid, days, name)
            notify_admin(f"🎁 برای شما {days:.0f} روز اشتراک ثبت شد. برای شروع، ربات را باز کنید و /start بزنید؛ شمارش روزها بعد از اتصال حساب شروع می‌شود.",
                         chat_id=uid, buttons=[[{"text": "🚀 شروع", "callback_data": "cmd:/menu"}]])
            return (f"✅ کاربر {uid}" + (f" ({name})" if name else "") + f" اضافه شد؛ اشتراک {days:.0f} روزه در انتظار اتصال کلید.\n"
                    f"⏳ شمارش روزها از لحظه‌ی اتصال کلید شروع می‌شود، نه از الان.\nبه او پیام دادم که /start بزند."), \
                   [[_cb_btn("👤 پنل کاربر", f"cmd:/user {uid}"), _cb_btn("👥 کاربران", "cmd:/listusers")], [_cb_btn("📋 منو", "menu")]]
        if cmd == "/grant" and len(parts) > 2:
            try:
                uid = _safe_uid(parts[1]); days = float(parts[2])
                if days <= 0:
                    raise ValueError
            except ValueError:
                return "❌ ورودی نامعتبر.", [[_cb_btn("📋 منو", "menu")]]
            rec = _vault.get(uid)
            if not rec:
                return f"❌ کاربر {uid} پیدا نشد.", [[_cb_btn("👥 کاربران", "cmd:/listusers")]]
            _vault.extend(uid, days)
            rec = _vault.get(uid)
            if rec.status == us.STATUS_BLOCKED:
                _vault.unblock(uid)
                rec = _vault.get(uid)
            if rec.public_key_enc:
                msg = f"🎁 {days:.0f} روز به اشتراک شما اضافه شد ({rec.days_left():.1f} روز باقی مانده)."
                rows = [[{"text": "📋 منوی من", "callback_data": "cmd:/menu"}]]
            else:
                msg = f"🎁 {days:.0f} روز اشتراک برای شما فعال شد (مجموع {rec.pending_days:.0f} روز؛ از لحظه‌ی اتصال حساب شروع می‌شود). حالا مرحله‌ی بعد 👇"
                rows = [[{"text": "🚀 ادامه‌ی راه‌اندازی", "callback_data": "cmd:/menu"}]]
            notify_admin(msg, chat_id=uid, buttons=rows)
            return f"✅ {days:.0f} روز برای {uid} ثبت و به او اطلاع داده شد.", [[_cb_btn("👤 پنل کاربر", f"cmd:/user {uid}"), _cb_btn("📋 منو", "menu")]]
        if cmd in ("/block", "/unblock") and len(parts) > 1:
            rec = _vault.get(parts[1])
            if not rec:
                return f"❌ کاربر {parts[1]} پیدا نشد.", [[_cb_btn("👥 کاربران", "cmd:/listusers")]]
            if cmd == "/block":
                n_open = _user_open_count(parts[1])
                _vault.block(parts[1])
                notify_admin(ui.msg_blocked(), chat_id=parts[1], kb="remove")
                return (f"⛔ {parts[1]} مسدود شد؛ دیگر ورود جدید ندارد" + (f" (ولی {n_open} معامله‌ی باز او همچنان تا بسته شدن مدیریت می‌شود)" if n_open else "") + "."), \
                       [[_cb_btn("✅ رفع مسدودی", f"cmd:/unblock {parts[1]}"), _cb_btn("👤 پنل", f"cmd:/user {parts[1]}")]]
            _vault.unblock(parts[1])
            notify_admin("✅ دسترسی شما دوباره باز شد.", chat_id=parts[1], buttons=[[{"text": "🚀 ادامه", "callback_data": "cmd:/menu"}]])
            return f"✅ مسدودی {parts[1]} برداشته شد.", [[_cb_btn("👤 پنل", f"cmd:/user {parts[1]}"), _cb_btn("📋 منو", "menu")]]
        if cmd in ("/capapprove", "/caprefuse") and len(parts) > 1:
            rec = _vault.get(parts[1])
            if not rec or not rec.cap_request:
                return "ℹ️ درخواستی در انتظار نیست (شاید قبلاً تصمیم گرفته شده).", [[_cb_btn("👥 کاربران", "cmd:/listusers")]]
            req = dict(rec.cap_request)
            _vault.set_field(parts[1], cap_request=None)
            if cmd == "/caprefuse":
                notify_admin("❌ درخواست افزایش سقف شما تأیید نشد. اگر نیاز دارید با پشتیبانی صحبت کنید.", chat_id=parts[1],
                             buttons=[[{"text": "🆘 پشتیبانی", "callback_data": "ask:support"}, {"text": "📋 منو", "callback_data": "cmd:/menu"}]])
                return "❌ درخواست رد شد و به کاربر اطلاع داده شد.", [[_cb_btn("📋 منو", "menu")]]
            fld, val = next(iter(req.items()))
            try:
                if fld == "max_open_trades":
                    _vault.set_risk(parts[1], max_open_trades=int(float(val)))
                elif fld == "leverage":
                    if D(val) > MAX_LEVERAGE_CAP:
                        return f"❌ حداکثر اهرم سیستم {MAX_LEVERAGE_CAP}x است.", [[_cb_btn("📋 منو", "menu")]]
                    _vault.set_risk(parts[1], leverage=val)
                elif fld == "risk_usdt":
                    _vault.set_risk(parts[1], risk_usdt=val)
                elif fld == "max_collateral_usdt":
                    _vault.set_risk(parts[1], max_collateral_usdt=val)
            except (ValueError, InvalidOperation) as e:
                return f"❌ {e}", [[_cb_btn("📋 منو", "menu")]]
            notify_admin("✅ درخواست افزایش سقف شما تأیید شد و اعمال شد. از تنظیمات می‌توانید مقدار را انتخاب کنید.", chat_id=parts[1],
                         buttons=[[{"text": "⚙️ تنظیمات", "callback_data": "cmd:/mysettings"}, {"text": "📋 منو", "callback_data": "cmd:/menu"}]])
            return "✅ سقف افزایش یافت و به کاربر اطلاع داده شد.", [[_cb_btn("👤 پنل کاربر", f"cmd:/user {parts[1]}"), _cb_btn("📋 منو", "menu")]]
        if cmd == "/listusers":
            users = sorted(_vault.all(), key=lambda r: r.added_at, reverse=True)
            if not users:
                return "👥 هنوز کاربری ثبت نشده.", [[_cb_btn("➕ افزودن کاربر", "ask:adduser"), _cb_btn("📋 منو", "menu")]]
            page = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 1
            per = 10
            pages = max(1, (len(users) + per - 1) // per)
            page = max(1, min(page, pages))
            chunk = users[(page - 1) * per: page * per]
            icon = {us.STATUS_ACTIVE: "🟢", us.STATUS_PENDING: "🟡", us.STATUS_SUSPENDED: "🟠", us.STATUS_EXPIRED: "🔴", us.STATUS_NEW: "⚪", us.STATUS_BLOCKED: "⛔"}
            lines = [f"👥 کاربران ({len(users)}) — صفحه {page} از {pages}", ui.DIVIDER]
            rows = []
            for r in chunk:
                extra = f"{r.days_left():.1f} روز" if r.status == us.STATUS_ACTIVE else (f"{r.pending_days:.0f} روز در انتظار" if r.status == us.STATUS_PENDING else "")
                nm = r.display_name or (f"@{r.username}" if r.username else "")
                lines.append(f"{icon.get(r.status, '⚪')} {r.user_id}" + (f" ({nm})" if nm else "") + (f" — {extra}" if extra else "") + f" — 📈{_user_open_count(r.user_id)}")
                rows.append([_cb_btn(f"{icon.get(r.status, '⚪')} {nm or r.user_id}", f"cmd:/user {r.user_id}")])
            nav = []
            if page > 1:
                nav.append(_cb_btn("◀️ قبلی", f"cmd:/listusers {page - 1}"))
            if page < pages:
                nav.append(_cb_btn("بعدی ▶️", f"cmd:/listusers {page + 1}"))
            if nav:
                rows.append(nav)
            rows.append([_cb_btn("➕ افزودن کاربر", "ask:adduser"), _cb_btn("💳 پرداخت‌های در انتظار", "cmd:/pendingpayments")])
            rows.append([_cb_btn("📢 پیام همگانی", "ask:broadcast"), _cb_btn("📋 منو", "menu")])
            return "\n".join(lines), rows
        if cmd in ("/user", "/userstatus") and len(parts) > 1:
            rec = _vault.get(parts[1])
            if not rec:
                return f"❌ کاربر {parts[1]} پیدا نشد.", [[_cb_btn("👥 کاربران", "cmd:/listusers")]]
            uid = rec.user_id
            pay_lines = []
            for p in (_ledger.for_user(uid) if _ledger else [])[-5:]:
                d = time.strftime("%Y-%m-%d", time.localtime(p.requested_at))
                pay_lines.append(f"  {p.payment_id} [{d}] {p.days} روز — {_fmt_amt(p.amount, p.currency)} — {p.status}")
            text = ui.card_user_panel({
                "user_id": uid, "name": rec.display_name or (f"@{rec.username}" if rec.username else ""), "status": rec.status, "paused": rec.paused,
                "days_left": rec.days_left(), "expires_date": time.strftime("%Y-%m-%d", time.localtime(rec.expires_at)) if rec.expires_at else "؟",
                "pending_days": rec.pending_days, "connected": bool(rec.public_key_enc), "terms": bool(rec.terms_accepted_at),
                "risk": rec.risk_usdt, "cap_risk": rec.cap_risk_usdt, "collateral": rec.max_collateral_usdt, "cap_collateral": rec.cap_collateral_usdt,
                "leverage": rec.leverage, "cap_leverage": rec.cap_leverage, "max_open": rec.max_open_trades, "last_error": rec.last_error},
                _user_open_count(uid), pay_lines)
            rows = [
                [_cb_btn("📈 معاملات", f"cmd:/usertrades {uid}"), _cb_btn("📜 تاریخچه", f"cmd:/userhistory {uid}"), _cb_btn("📊 سود/زیان", f"cmd:/userpnl {uid}")],
                [_cb_btn("➕ ۷ روز", f"cmd:/extenduser {uid} 7"), _cb_btn("➕ ۳۰ روز", f"cmd:/extenduser {uid} 30"), _cb_btn("➕ ۹۰ روز", f"cmd:/extenduser {uid} 90")],
                [_cb_btn("✏️ تمدید با روز دلخواه", f"ask:extend:{uid}"), _cb_btn("🎯 تنظیم سقف‌ها", f"ask:userrisk:{uid}")],
                [_cb_btn("▶️ فعال‌سازی اشتراک" if rec.status == us.STATUS_SUSPENDED else "⏸ تعلیق اشتراک",
                         f"cmd:/resumeuser {uid}" if rec.status == us.STATUS_SUSPENDED else f"cmd:/suspenduser {uid}"),
                 _cb_btn("▶️ ورود جدید" if rec.paused else "⏸ توقف ورود جدید", f"cmd:/userresume {uid}" if rec.paused else f"cmd:/userforcepause {uid}")],
                [_cb_btn("🤖 خروج خودکار: خاموش" if rec.auto_exit else "🤖 خروج خودکار: روشن", f"cmd:/userautoexit {uid} " + ("off" if rec.auto_exit else "on")),
                 _cb_btn("✉️ ارسال پیام", f"ask:msguser:{uid}")],
                [_cb_btn("❌ بستن همه‌ی معاملات", f"cmd:/usercloseall {uid}"), _cb_btn("🔌 قطع کلید", f"cmd:/userdisconnect {uid}")],
                [_cb_btn("✅ رفع مسدودی" if rec.status == us.STATUS_BLOCKED else "⛔ مسدود", f"cmd:/unblock {uid}" if rec.status == us.STATUS_BLOCKED else f"cmd:/block {uid}"),
                 _cb_btn("🗑 حذف کامل", f"cmd:/removeuser {uid}")],
                [_cb_btn("👥 کاربران", "cmd:/listusers"), _cb_btn("📋 منو", "menu")],
            ]
            return text, rows
        if cmd == "/extenduser" and len(parts) > 2:
            try:
                days = float(parts[2])
                if days <= 0:
                    raise ValueError
            except ValueError:
                return "❌ تعداد روز نامعتبر است.", [[_cb_btn("📋 منو", "menu")]]
            if not _vault.get(parts[1]):
                return f"❌ کاربر {parts[1]} پیدا نشد.", [[_cb_btn("👥 کاربران", "cmd:/listusers")]]
            rec = _vault.extend(parts[1], days)
            left = f"{rec.days_left():.1f} روز باقی مانده" if rec.public_key_enc else f"مجموع {rec.pending_days:.0f} روز در انتظار اتصال"
            notify_admin(f"🎁 اشتراک شما {days:.0f} روز تمدید شد ({left}).", chat_id=rec.notify_chat_id or rec.user_id,
                         buttons=[[{"text": "📋 منوی من", "callback_data": "cmd:/menu"}]])
            return f"✅ اشتراک {rec.user_id} به‌اندازه‌ی {days:.0f} روز تمدید شد؛ {left}.", [[_cb_btn("👤 پنل کاربر", f"cmd:/user {rec.user_id}"), _cb_btn("📋 منو", "menu")]]
        if cmd == "/suspenduser" and len(parts) > 1:
            if not _vault.get(parts[1]):
                return f"❌ کاربر {parts[1]} پیدا نشد.", [[_cb_btn("👥 کاربران", "cmd:/listusers")]]
            _vault.suspend(parts[1])
            return (f"⏸ کاربر {parts[1]} معلق شد؛ معامله‌ی جدیدی برایش باز نمی‌شود (معاملات باز فعلی‌اش دست‌نخورده می‌مانند)."), \
                   [[_cb_btn("👤 پنل کاربر", f"cmd:/user {parts[1]}")]]
        if cmd == "/resumeuser" and len(parts) > 1:
            if not _vault.get(parts[1]):
                return f"❌ کاربر {parts[1]} پیدا نشد.", [[_cb_btn("👥 کاربران", "cmd:/listusers")]]
            try:
                _vault.resume(parts[1])
                return f"▶️ کاربر {parts[1]} دوباره فعال شد.", [[_cb_btn("👤 پنل کاربر", f"cmd:/user {parts[1]}")]]
            except ValueError as e:
                return f"❌ {e}", [[_cb_btn("👤 پنل کاربر", f"cmd:/user {parts[1]}")]]
        if cmd == "/removeuser" and len(parts) > 1:
            if not _vault.get(parts[1]):
                return f"❌ کاربر {parts[1]} پیدا نشد.", [[_cb_btn("👥 کاربران", "cmd:/listusers")]]
            n_open = _user_open_count(parts[1])
            if n_open:
                return (f"❌ کاربر {parts[1]} هنوز {n_open} معامله‌ی باز دارد؛ با حذف کلیدش محافظت آن‌ها قطع می‌شود.\nاول ببندیدشان."), \
                       [[_cb_btn("❌ بستن همه‌ی معاملات او", f"cmd:/usercloseall {parts[1]}")], [_cb_btn("👤 پنل کاربر", f"cmd:/user {parts[1]}")]]
            if not (parts[-1] in ("yes", "تایید") and len(parts) > 2):
                return ui.msg_confirm("حذف کامل کاربر", f"کاربر {parts[1]} و کلید ذخیره‌شده‌اش برای همیشه پاک می‌شود (غیرقابل بازگشت)."), \
                       [[_cb_btn("🗑 بله، حذف کن", f"cmd:/removeuser {parts[1]} yes")], [_cb_btn("↩️ لغو", f"cmd:/user {parts[1]}")]]
            _vault.remove(parts[1])
            _user_clients.pop(str(parts[1]), None)
            _user_states.pop(str(parts[1]), None)
            return f"🗑 کاربر {parts[1]} و کلیدهایش کامل حذف شد.", [[_cb_btn("👥 کاربران", "cmd:/listusers"), _cb_btn("📋 منو", "menu")]]
        if cmd == "/setuserrisk" and len(parts) >= 6:
            if not _vault.get(parts[1]):
                return f"❌ کاربر {parts[1]} پیدا نشد.", [[_cb_btn("👥 کاربران", "cmd:/listusers")]]
            try:
                rec = _vault.set_risk(parts[1], risk_usdt=parts[2], max_collateral_usdt=parts[3],
                                      leverage=parts[4], max_open_trades=int(float(parts[5])))
            except (ValueError, InvalidOperation):
                return "❌ همه‌ی مقادیر باید عدد باشند.", [[_cb_btn("👤 پنل کاربر", f"cmd:/user {parts[1]}")]]
            return (f"✅ سقف‌های {rec.user_id}: ریسک {rec.risk_usdt} | سقف وثیقه {rec.max_collateral_usdt} | "
                    f"اهرم {rec.leverage}x | حداکثر معاملات {rec.max_open_trades}"), [[_cb_btn("👤 پنل کاربر", f"cmd:/user {rec.user_id}")]]

        # ---------------- pricing & payment info (admin-only) ----------------
        if cmd == "/setpricetoman" and len(parts) > 2:
            try:
                days = int(parts[1]); toman = int(float(parts[2].replace(",", "")))
            except ValueError:
                return "❌ روز و مبلغ باید عدد باشند.", [[_cb_btn("💰 تعرفه‌ها", "cmd:/listprices")]]
            _pricing.set_price(days, toman=toman)
            return f"✅ قیمت {days} روز: {toman:,} تومان ثبت شد.", [[_cb_btn("💰 تعرفه‌ها", "cmd:/listprices")]]
        if cmd == "/setpriceusdt" and len(parts) > 2:
            try:
                days = int(parts[1]); usdt_v = float(parts[2])
            except ValueError:
                return "❌ روز و مبلغ باید عدد باشند.", [[_cb_btn("💰 تعرفه‌ها", "cmd:/listprices")]]
            _pricing.set_price(days, usdt=usdt_v)
            return f"✅ قیمت {days} روز: {usdt_v:g} USDT ثبت شد.", [[_cb_btn("💰 تعرفه‌ها", "cmd:/listprices")]]
        if cmd == "/removeprice" and len(parts) > 1:
            try:
                days = int(parts[1])
            except ValueError:
                return "❌ روز باید عدد باشد.", [[_cb_btn("💰 تعرفه‌ها", "cmd:/listprices")]]
            _pricing.remove_price(days)
            return f"✅ تعرفه‌ی {days} روز حذف شد.", [[_cb_btn("💰 تعرفه‌ها", "cmd:/listprices")]]
        if cmd == "/listprices":
            tiers = _pricing.all()
            lines = ["💰 تعرفه‌ها و اطلاعات دریافت پول", ui.DIVIDER]
            rows = []
            if not tiers:
                lines.append("هنوز تعرفه‌ای ثبت نشده. با دکمه‌های پایین اضافه کنید.")
            for days, p in tiers.items():
                tx = []
                if p.get("toman") is not None:
                    tx.append(f"{int(p['toman']):,} تومان")
                if p.get("usdt") is not None:
                    tx.append(f"{p['usdt']:g} USDT")
                lines.append(f"📅 {days} روز: " + (" | ".join(tx) if tx else "—"))
                rows.append([_cb_btn(f"🗑 حذف تعرفه‌ی {days} روز", f"cmd:/removeprice {days}")])
            lines.append("")
            lines.append(f"💳 کارت: {(_payment_info.card_number + ' — ' + _payment_info.card_holder) if _payment_info.is_card_set() else 'ثبت نشده ❌'}")
            lines.append(f"💵 USDT ({_payment_info.usdt_network}): {_payment_info.usdt_address if _payment_info.is_usdt_set() else 'ثبت نشده ❌'}")
            rows += [[_cb_btn("➕ تعرفه‌ی تومان", "ask:pricetoman"), _cb_btn("➕ تعرفه‌ی USDT", "ask:priceusdt")],
                     [_cb_btn("💳 تنظیم کارت", "ask:setcard"), _cb_btn("💵 تنظیم آدرس USDT", "ask:setusdt")],
                     [_cb_btn("👥 کاربران", "cmd:/listusers"), _cb_btn("📋 منو", "menu")]]
            return "\n".join(lines), rows
        if cmd == "/fwdsignal" and len(parts) > 1:
            # relayed by the bridge when the admin forwards/pastes a signal: /fwdsignal b64:<utf8 text, base64>
            import base64 as _b64
            try:
                raw_text = _b64.b64decode(parts[1].split("b64:", 1)[-1], validate=True).decode("utf-8")
            except Exception:
                return "❌ متن سیگنال خوانده نشد؛ دوباره فوروارد کنید.", [[_btn_menu()]]
            fs, why = fwd.parse_forwarded_signal(raw_text)
            if fs is None:
                return (f"❌ این پیام به‌عنوان سیگنال قابل اجرا شناخته نشد:\n{why}\n\n"
                        "فرمت لازم (هر مورد در یک خط): نماد، ورود (entry)، حد ضرر (stop loss) و حداقل یک تارگت (target 1 ...)؛ "
                        "همراه با عدد. هیچ سفارشی ثبت نشد."), [[_btn_menu()]]
            return open_forwarded_signal(nx, state, fs, gh, f"FWD-{command_id_for_signal()}")
        if cmd == "/cancelentry" and len(parts) > 1:
            key_c = parts[1].upper()
            if key_c in state.get("fwd_retry", {}):
                state["fwd_retry"].pop(key_c, None)
                save_state(state)
                return f"✅ سیگنال نگه‌داشته‌شده‌ی {key_c} (در انتظار اتصال) لغو شد.", [[_btn_positions(), _btn_menu()]]
            rec_c = state.get("pending_entries", {}).get(key_c)
            if not rec_c:
                return "ℹ️ سفارش ورودی با این نام در انتظار نیست (شاید قبلاً پر یا لغو شده).", [[_btn_positions(), _btn_menu()]]
            if _cancel_entry_and_settle(nx, state, key_c, rec_c, "به درخواست شما"):
                return f"✅ سفارش ورود {key_c} لغو شد.", [[_btn_positions(), _btn_menu()]]
            return f"⏳ لغو {key_c} هنوز تأیید نشد؛ ربات تا تأیید نوبیتکس تکرار می‌کند. چند ثانیه بعد «معاملات باز» را ببینید.", [[_fwd_btn_cancel(key_c)]]
        if cmd == "/pendingentries":
            pe = state.get("pending_entries", {})
            fr = state.get("fwd_retry", {})
            if not pe and not fr:
                return "📭 سفارش ورود در انتظاری ندارید.", [[_btn_positions(), _btn_menu()]]
            lines = ["⏳ سفارش‌های ورود در انتظار", ui.DIVIDER]
            rows = []
            for k_, r_ in fr.items():
                lines.append(f"• {k_} — 🌐 منتظر برقراری اتصال به نوبیتکس (تلاش {r_.get('tries', 0)})")
                rows.append([_fwd_btn_cancel(k_)])
            for k_, r_ in pe.items():
                left_h = max(0.0, (float(r_.get("expires_at", 0)) - time.time()) / 3600)
                lines.append(f"• {k_} — {'Long' if r_['side'] == 'LONG' else 'Short'} @ {num_s(D(r_['entry']))} | SL {num_s(D(r_['stop']))} | {left_h:.1f} ساعت مانده")
                rows.append([_fwd_btn_cancel(k_)])
            rows.append([_btn_positions(), _btn_menu()])
            return "\n".join(lines), rows
        if cmd == "/newdiscount" and len(parts) > 2:
            # /newdiscount CODE PERCENT [plans: all | 30,90] [max_uses 0=unlimited] [expire_days 0=never]
            try:
                pct = float(parts[2].replace("%", "").replace("٪", ""))
                plans_arg = parts[3].lower() if len(parts) > 3 else "all"
                plans = [] if plans_arg in ("all", "0", "همه") else [int(x) for x in plans_arg.replace("،", ",").split(",") if x.strip()]
                max_uses = int(float(parts[4])) if len(parts) > 4 else 0
                exp_days = float(parts[5]) if len(parts) > 5 else 0.0
                dc = _discounts.create(parts[1], pct, plans, max_uses, exp_days)
            except ValueError as e:
                return f"❌ {e}", [[_cb_btn("🎟 کدهای تخفیف", "cmd:/discounts")]]
            unknown = [d for d in dc.plans if _pricing.price_for(d) is None]
            warn = (f"\n⚠️ برای این مدت‌ها هنوز تعرفه ثبت نشده: {', '.join(str(d) for d in unknown)}") if unknown else ""
            return f"✅ کد تخفیف ساخته شد:\n{ui.discount_line(dc, 0)}{warn}", [[_cb_btn("🎟 کدهای تخفیف", "cmd:/discounts")]]
        if cmd == "/deldiscount" and len(parts) > 1:
            ok = _discounts.delete(parts[1])
            return (f"🗑 کد {us.normalize_code(parts[1])} حذف شد." if ok else "ℹ️ چنین کدی پیدا نشد."), [[_cb_btn("🎟 کدهای تخفیف", "cmd:/discounts")]]
        if cmd in ("/discountoff", "/discounton") and len(parts) > 1:
            try:
                dc = _discounts.set_active(parts[1], cmd == "/discounton")
            except KeyError:
                return "ℹ️ چنین کدی پیدا نشد.", [[_cb_btn("🎟 کدهای تخفیف", "cmd:/discounts")]]
            return f"{'✅ فعال' if dc.active else '⏸ غیرفعال'} شد: {dc.code}", [[_cb_btn("🎟 کدهای تخفیف", "cmd:/discounts")]]
        if cmd == "/discounts":
            codes = _discounts.all()
            lines = ["🎟 کدهای تخفیف", ui.DIVIDER]
            rows = []
            if not codes:
                lines.append("هنوز کدی نساخته‌اید.")
            for dc in codes:
                lines.append(ui.discount_line(dc, _ledger.uses_of_code(dc.code)))
                rows.append([_cb_btn(f"⏸ غیرفعال {dc.code}" if dc.active else f"▶️ فعال {dc.code}",
                                     f"cmd:/discountoff {dc.code}" if dc.active else f"cmd:/discounton {dc.code}"),
                             _cb_btn(f"🗑 حذف {dc.code}", f"cmd:/deldiscount {dc.code}")])
            rows += [[_cb_btn("➕ ساخت کد تخفیف", "ask:newdiscount")], [_cb_btn("👥 کاربران", "cmd:/listusers"), _cb_btn("📋 منو", "menu")]]
            return "\n".join(lines), rows
        if cmd == "/setcard" and len(parts) > 2:
            _payment_info.set_card(parts[1], " ".join(parts[2:]))
            return f"✅ اطلاعات کارت ثبت شد: {parts[1]} به‌نام {' '.join(parts[2:])}", [[_cb_btn("💰 تعرفه‌ها", "cmd:/listprices")]]
        if cmd == "/setusdt" and len(parts) > 1:
            network = parts[2] if len(parts) > 2 else "TRC20"
            _payment_info.set_usdt(parts[1], network)
            return f"✅ آدرس کیف‌پول USDT ثبت شد ({network}): {parts[1]}", [[_cb_btn("💰 تعرفه‌ها", "cmd:/listprices")]]

        # ---------------- payment review (admin-only) ----------------
        if cmd == "/pendingpayments":
            pend = [p for p in _ledger.pending() if p.note]
            if not pend:
                return "💳 پرداختی در انتظار تأیید نیست.", [[_cb_btn("👥 کاربران", "cmd:/listusers"), _cb_btn("📋 منو", "menu")]]
            lines = [f"💳 {len(pend)} پرداخت در انتظار تأیید", ui.DIVIDER]
            rows = []
            for p in pend:
                d = time.strftime("%Y-%m-%d %H:%M", time.localtime(p.requested_at))
                amt = f"{int(p.amount):,} تومان" if p.currency == "toman" else f"{p.amount:g} USDT"
                lines.append(f"{p.payment_id} — کاربر {p.user_id} — {p.days} روز — {amt} — رسید: {p.note or '—'} — [{d}]")
                rows.append([_cb_btn(f"✅ تأیید {p.payment_id}", f"cmd:/confirmpayment {p.payment_id}"),
                             _cb_btn(f"❌ رد {p.payment_id}", f"cmd:/rejectpayment {p.payment_id}")])
            rows.append([_cb_btn("📋 منو", "menu")])
            return "\n".join(lines), rows
        if cmd == "/confirmpayment" and len(parts) > 1:
            try:
                p = _ledger.confirm(parts[1])
            except (KeyError, ValueError) as e:
                return f"❌ {e}", [[_cb_btn("💳 در انتظار", "cmd:/pendingpayments")]]
            rec = _vault.get(p.user_id)
            if not rec:
                return f"⚠️ پرداخت {p.payment_id} تأیید شد ولی کاربر {p.user_id} دیگر در سیستم نیست.", [[_cb_btn("📋 منو", "menu")]]
            was_connected = bool(rec.public_key_enc)
            rec = _vault.extend(p.user_id, p.days)
            if not was_connected:
                user_msg = (f"✅ پرداخت شما ({p.payment_id}) تأیید شد!\n{p.days} روز به اشتراک شما اضافه شد "
                            f"(مجموع {rec.pending_days:.0f} روز؛ از لحظه‌ی اتصال حساب شروع می‌شود).\nحالا مرحله‌ی بعد: اتصال امن حساب نوبیتکس 👇")
                rows = [[{"text": "🔑 شروع اتصال حساب", "callback_data": "cmd:/connectguide"}]]
            else:
                user_msg = f"✅ پرداخت شما ({p.payment_id}) تأیید شد؛ اشتراک {p.days} روز تمدید شد ({rec.days_left():.1f} روز باقی مانده)."
                rows = [[{"text": "📋 منوی من", "callback_data": "cmd:/menu"}]]
            notify_admin(user_msg, chat_id=rec.notify_chat_id or p.user_id, buttons=rows)
            return f"✅ پرداخت {p.payment_id} تأیید شد و به کاربر {p.user_id} اطلاع داده شد.", [[_cb_btn("👤 پنل کاربر", f"cmd:/user {p.user_id}"), _cb_btn("💳 در انتظار", "cmd:/pendingpayments")]]
        if cmd == "/rejectpayment" and len(parts) > 1:
            reason = " ".join(parts[2:])
            try:
                p = _ledger.reject(parts[1], reason=reason)
            except (KeyError, ValueError) as e:
                return f"❌ {e}", [[_cb_btn("💳 در انتظار", "cmd:/pendingpayments")]]
            rec = _vault.get(p.user_id)
            notify_admin(f"❌ پرداخت شما ({p.payment_id}) تأیید نشد." + (f"\nدلیل: {reason}" if reason else "") +
                         "\nاگر فکر می‌کنید اشتباهی رخ داده، با پشتیبانی در تماس باشید یا دوباره پرداخت کنید.",
                         chat_id=(rec.notify_chat_id if rec else None) or p.user_id,
                         buttons=[[{"text": "🆘 پشتیبانی", "callback_data": "ask:support"}, {"text": "💳 تعرفه‌ها", "callback_data": "cmd:/plans"}]])
            return f"❌ پرداخت {p.payment_id} رد شد و به کاربر اطلاع داده شد.", [[_cb_btn("👤 پنل کاربر", f"cmd:/user {p.user_id}"), _cb_btn("💳 در انتظار", "cmd:/pendingpayments")]]

        if cmd == "/autoexit":
            if len(parts) < 2 or parts[1].lower() not in ("on", "off"):
                on = _auto_exit_enabled()
                return (f"🤖 اجرای خودکار رویدادهای کانال (بریک‌ایون، SL بعد از تارگت، حد ضرر نهایی، بسته‌شدن Runner، بستن اجباری): "
                        f"{'روشن ✅' if on else 'خاموش ⏸'}\nخاموش = پیش از هر بستن از شما تأیید می‌گیرد."), \
                       [[_cb_btn("⏸ خاموش کن" if on else "▶️ روشن کن", "cmd:/autoexit " + ("off" if on else "on"))], [_cb_btn("📋 منو", "menu")]]
            c = load_control(gh); c["auto_exit"] = parts[1].lower() == "on"; save_control(gh, c)
            _admin_auto_exit_cache.update(ts=time.time(), value=c["auto_exit"])
            return ("🤖 اجرای خودکار رویدادهای کانال " + ("روشن شد ✅" if c["auto_exit"] else "خاموش شد ⏸ (پیش از هر بستن تأیید می‌گیرم)")), [[_cb_btn("📊 وضعیت", "cmd:/status"), _cb_btn("📋 منو", "menu")]]
        if cmd == "/userautoexit" and len(parts) > 2 and parts[2].lower() in ("on", "off"):
            if not _vault.get(parts[1]):
                return f"❌ کاربر {parts[1]} پیدا نشد.", [[_cb_btn("👥 کاربران", "cmd:/listusers")]]
            _vault.set_auto_exit(parts[1], parts[2].lower() == "on")
            return f"🤖 خروج خودکار برای {parts[1]}: {parts[2].lower()}", [[_cb_btn("👤 پنل کاربر", f"cmd:/user {parts[1]}")]]

        # ---- admin looking at / acting on a subscriber's account ----
        if cmd in ("/usertrades", "/userhistory", "/userpnl", "/userclose", "/usercloseall", "/userliqsafe",
                   "/userliqadd", "/usercolall", "/userbalance", "/userconfirmclose", "/userdismissclose") and len(parts) > 1:
            rec = _vault.get(parts[1])
            if not rec or not rec.public_key_enc:
                return f"❌ کاربر {parts[1]} پیدا نشد یا حسابی متصل ندارد.", [[_cb_btn("👥 کاربران", "cmd:/listusers")]]
            inner = {"/usertrades": "/positions", "/userhistory": "/history", "/userpnl": "/pnl", "/userclose": "/close",
                     "/usercloseall": "/closeall", "/userliqsafe": "/liqsafe", "/userliqadd": "/liqadd",
                     "/usercolall": "/colall", "/userbalance": "/balance",
                     "/userconfirmclose": "/confirmclose", "/userdismissclose": "/dismissclose"}[cmd]
            arg_str = " ".join(parts[2:])
            if cmd in ("/userclose", "/userliqsafe", "/userliqadd", "/userconfirmclose", "/userdismissclose"):
                arg_str = arg_str.upper() if len(parts) == 3 else (parts[2].upper() + " " + " ".join(parts[3:]))
            full = (inner + " " + arg_str).strip()
            if cmd in ("/usertrades", "/userhistory", "/userpnl", "/userbalance"):
                return _run_as_user_silent(rec, full)          # read-only: buttons stay admin-side
            # state-changing: run as the subscriber (their chat also gets the closing/adjust reports),
            # then re-point the reply's buttons at the admin commands.
            return _remap_for_admin(_run_as_user(rec, gh, full), parts[1])
        if cmd == "/userdisconnect" and len(parts) > 1:
            rec = _vault.get(parts[1])
            if not rec:
                return f"❌ کاربر {parts[1]} پیدا نشد.", [[_cb_btn("👥 کاربران", "cmd:/listusers")]]
            if _user_open_count(parts[1]):
                return "❌ معامله‌ی باز دارد؛ اول همه را ببندید.", [[_cb_btn("❌ بستن همه‌ی معاملات او", f"cmd:/usercloseall {parts[1]}")]]
            _vault.disconnect(parts[1]); _user_clients.pop(str(parts[1]), None); _user_states.pop(str(parts[1]), None)
            notify_admin("🔌 اتصال حساب شما توسط ادمین قطع شد.", chat_id=parts[1], kb="remove")
            return f"🔌 کلید کاربر {parts[1]} حذف شد (روزهای باقیمانده حفظ شد).", [[_cb_btn("👤 پنل کاربر", f"cmd:/user {parts[1]}")]]
        if cmd == "/userforcepause" and len(parts) > 1:
            if not _vault.get(parts[1]):
                return f"❌ کاربر {parts[1]} پیدا نشد.", [[_cb_btn("👥 کاربران", "cmd:/listusers")]]
            _vault.set_paused(parts[1], True)
            return f"⏸ ورود جدید کاربر {parts[1]} متوقف شد.", [[_cb_btn("▶️ ازسرگیری", f"cmd:/userresume {parts[1]}"), _cb_btn("👤 پنل", f"cmd:/user {parts[1]}")]]
        if cmd == "/userresume" and len(parts) > 1:
            if not _vault.get(parts[1]):
                return f"❌ کاربر {parts[1]} پیدا نشد.", [[_cb_btn("👥 کاربران", "cmd:/listusers")]]
            _vault.set_paused(parts[1], False)
            return f"▶️ ورود جدید کاربر {parts[1]} ازسرگرفته شد.", [[_cb_btn("👤 پنل کاربر", f"cmd:/user {parts[1]}")]]
        if cmd == "/msguser" and len(parts) > 2:
            if not _vault.get(parts[1]):
                return f"❌ کاربر {parts[1]} پیدا نشد.", [[_cb_btn("👥 کاربران", "cmd:/listusers")]]
            notify_admin("📢 پیام ادمین:\n" + " ".join(parts[2:]), chat_id=parts[1],
                         buttons=[[{"text": "✍️ پاسخ", "callback_data": "ask:support"}, {"text": "📋 منوی من", "callback_data": "cmd:/menu"}]])
            return f"✅ پیام برای {parts[1]} ارسال شد.", [[_cb_btn("👤 پنل کاربر", f"cmd:/user {parts[1]}"), _cb_btn("📋 منو", "menu")]]
        if cmd == "/broadcast" and len(parts) > 1:
            targets = [r for r in _vault.all() if r.status != us.STATUS_BLOCKED]
            for r in targets:
                notify_admin("📢 پیام ادمین:\n" + " ".join(parts[1:]), chat_id=r.notify_chat_id or r.user_id,
                             buttons=[[{"text": "📋 منوی من", "callback_data": "cmd:/menu"}]])
            return f"✅ پیام برای {len(targets)} کاربر ارسال شد.", [[_cb_btn("📋 منو", "menu")]]
        return "❓ دستور ناشناخته. /menu را بزنید."
    except Exception as e:
        log.exception("bot command failed")
        return f"🚨 اجرای دستور ناموفق بود: {type(e).__name__}: {e}"

def main() -> int:
    global _gh_outbox, _vault, _pricing, _ledger, _payment_info, _gh_main, _discounts
    acquire_single_instance_lock()
    os.makedirs(USER_STATES_DIR, exist_ok=True)

    _gh_outbox = GithubClient(GITHUB_REPO, GITHUB_PAT, GITHUB_BRANCH)
    _vault = us.UserVault()
    _pricing = us.PricingConfig()
    _discounts = us.DiscountStore()
    _ledger = us.PaymentLedger()
    _payment_info = us.PaymentInfo()
    if _vault.all() and not os.environ.get("USER_VAULT_MASTER_KEY"):
        notify_admin("🚨 USER_VAULT_MASTER_KEY در .env تنظیم نیست، ولی در vault کاربر ثبت‌شده وجود دارد؛ "
                     "تا این کلید برنگردد هیچ‌کدام از حساب‌های کاربران کار نمی‌کند (معاملات باز آن‌ها فقط با سفارش‌های "
                     "ثبت‌شده‌ی خود نوبیتکس محافظت می‌شوند).")
    gh = GithubClient(GITHUB_REPO, GITHUB_PAT, GITHUB_BRANCH)
    _gh_main = gh
    gh.validate_repository()
    nx = NobitexClient(NobitexConfig(
        public_key=NOBITEX_PUBLIC_KEY,
        private_key_b64=NOBITEX_PRIVATE_KEY,
        base_url=NOBITEX_BASE_URL,
        public_base_url=NOBITEX_PUBLIC_BASE_URL,
    ))
    state = load_state()
    global _nx_for_history
    _nx_for_history = nx

    # Telegram is deliberately NOT contacted from Windows.  The GitHub Actions
    # bridge is the only Telegram-facing component; it relays channel posts and
    # admin commands through signals.jsonl / commands.jsonl and relays outbox.jsonl
    # back to Telegram. This is important when Telegram is filtered on the Windows host.

    if "testnet" in NOBITEX_BASE_URL.lower():
        notify_admin("🚨 NOBITEX_BASE_URL هنوز TESTNET است؛ اجرای واقعی متوقف شد.")
        raise RuntimeError("Refusing to run on testnet configuration for this production executor")

    _startup_control = load_control(gh)
    notify_admin(
        f"🟢 Executor started (build {EXECUTOR_BUILD})\nNobitex={NOBITEX_BASE_URL}\n"
        f"Risk cap=${_startup_control.get('risk_usdt', DEFAULT_RISK_USDT)}\n"
        f"Leverage preference={_startup_control.get('leverage', DEFAULT_LEVERAGE)}x (sent as-is to Nobitex)\n"
        f"GitHub={GITHUB_REPO}"
    )
    global _last_heartbeat_write
    _last_heartbeat_write = 0.0
    write_heartbeat(state)
    try:
        reconcile_on_startup(nx, state)
        _rec, _rec_btn = recover_orphan_positions(nx, gh, state, only_known=True)
        if _rec:
            notify_admin(_rec, buttons=_rec_btn)
    except Exception:
        log.exception("startup reconciliation failed; continuing to the main loop, which will keep retrying")
        notify_admin("⚠️ همگام‌سازی هنگام شروع به کار شکست خورد؛ Executor به کار خود در حلقه‌ی اصلی ادامه می‌دهد و دوباره تلاش می‌کند.")

    consecutive_errors = 0
    alerted_outage = False
    while True:
        try:
            write_heartbeat(state)
            flush_pending_outbox()
            enforce_stop_guard(nx, state)
            resolve_needs_flatten(nx, state)
            poll_commands(nx, gh, state)
            poll_once(nx, gh, state)
            enforce_stop_guard(nx, state)
            poll_commands(nx, gh, state)
            resolve_pending_entries(nx, state)
            resolve_fwd_retries(nx, state, gh)
            resolve_pending_protection(nx, state)
            poll_commands(nx, gh, state)
            resolve_needs_protection(nx, state)
            enforce_stop_guard(nx, state)
            poll_commands(nx, gh, state)
            sync_trades_with_exchange(nx, state)
            poll_commands(nx, gh, state)
            auto_recover_orphans(nx, gh, state)
            poll_commands(nx, gh, state)
            resolve_pending_close_confirm(state, nx)
            poll_commands(nx, gh, state)
            update_runner_trailing(nx, state)
            poll_commands(nx, gh, state)
            # Subscribers: expiry/renewal notices, then one isolated pass of the
            # same trading engine per subscriber. Each is wrapped so one bad
            # account can never stall the admin's own account or the others.
            manage_subscriptions()
            send_daily_reports()
            run_all_user_cycles(gh, between=lambda: _poll_commands_throttled(nx, gh, state))
            poll_commands(nx, gh, state)
            write_heartbeat(state)
            if alerted_outage:
                alerted_outage = False
                notify_admin(f"✅ اتصال برگشت؛ Executor بعد از {consecutive_errors} خطای اتصال دوباره عادی کار می‌کند.")
            consecutive_errors = 0
        except Exception as e:
            consecutive_errors += 1
            if _is_transient_exception(e):
                # A network hiccup (GitHub / Nobitex timeout, DNS, rate limit) is not a program error: log it
                # quietly, back off a little, and alert ONCE if it lasts (and once more when it ends).
                log.warning("main loop: temporary network problem #%s: %s", consecutive_errors, e)
                if consecutive_errors == 8 or (consecutive_errors > 8 and consecutive_errors % 60 == 0):
                    alerted_outage = True
                    notify_admin(f"🌐 اتصال شبکه/GitHub/نوبیتکس حدود {consecutive_errors} بار پشت‌سرهم قطع یا کند بود: {str(e)[:200]}\n"
                                 f"معاملات با سفارش‌های روی نوبیتکس محافظت‌شده می‌مانند؛ Executor خودش دوباره تلاش می‌کند.")
                time.sleep(POLL_INTERVAL_SECONDS + min(20, consecutive_errors * 2))
                continue
            log.exception("main loop error #%s", consecutive_errors)
            if consecutive_errors in (1, 5) or consecutive_errors % 20 == 0:
                notify_admin(f"🚨 Main loop error #{consecutive_errors}: {e}")
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    sys.exit(main())
