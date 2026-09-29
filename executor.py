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
from decimal import Decimal, ROUND_DOWN, ROUND_UP, InvalidOperation
from pathlib import Path
from typing import Any, Dict, Optional

from github_client import GithubClient
import user_store as us
import ui_text as ui
from nobitex_client import NobitexClient, NobitexConfig, NobitexAPIError, is_transient_error
from signal_parser import parse_message, ParsedSignal, ParsedEvent
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
EXECUTOR_BUILD = "2026-09-28-r30"

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
_admin_auto_exit_cache = {"ts": 0.0, "value": True}
_vault: Optional["us.UserVault"] = None
_pricing: Optional["us.PricingConfig"] = None
_ledger: Optional["us.PaymentLedger"] = None
_payment_info: Optional["us.PaymentInfo"] = None
_gh_main: Optional[GithubClient] = None
# Non-admin (multi-tenant) callers may only ever reach this fixed whitelist -
# every other command string, whatever it is, is refused before any admin
# logic runs. This check does not trust bridge.py's own filtering (defense
# in depth): even if bridge.py ever had a routing bug, a tagged non-admin
# command still cannot fall through to /close, /adduser, /removeuser, etc.
SELF_SERVICE_COMMANDS = {"/connect_enc", "/mysubscription", "/mystatus", "/subscribe", "/paid", "/start", "/help", "/accept", "/prices",
                         "/mytrades", "/myhistory", "/mypnl", "/myclose", "/mycloseall", "/mypause", "/myresume", "/myrisk", "/disconnect", "/myautoexit"}
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



def _enqueue_outbox(payload: Dict[str, Any]) -> None:
    line = json.dumps(payload, ensure_ascii=False)
    if _gh_outbox is None:
        _queue_outbox_locally(line)
        return
    try:
        _gh_outbox.append_line("outbox.jsonl", line)
    except Exception as e:
        log.error("نوشتن outbox ناموفق بود؛ پیام محلی صف شد تا بعداً دوباره تلاش شود: %s", e)
        _queue_outbox_locally(line)


def notify_admin(message: str, *, key: Optional[str] = None, buttons: Optional[list] = None,
                  chat_id: Optional[str] = None) -> None:
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
        if _active_user_chat_id and not chat_id and buttons:
            rows = buttons if isinstance(buttons[0], list) else [buttons]
            kept = [[b for b in row if str(b.get("callback_data", "")).startswith("cmd:/my")] for row in rows]
            kept = [row for row in kept if row]
            buttons = kept or None
    if buttons:
        payload["buttons"] = buttons
    _enqueue_outbox(payload)
    if _active_user_chat_id and not chat_id and message.lstrip().startswith(("🚨", "❌")):
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
            _gh_outbox.append_line("outbox.jsonl", line)
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
    """Realized P&L from Nobitex itself (never from strategy prices).
    1) the closed position's own PNL figure; 2) otherwise the real fills of
    every order this trade placed (matched amount x average fill price vs the
    real entry). Returns (Decimal|None, source). Best effort: any API problem
    just returns (None, '') and the caller falls back to a labeled estimate."""
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
    try:
        side = trade.get("side")
        entry = D(trade.get("entry_actual", "0"))
        if entry <= 0:
            return None, ""
        ids = []
        for t in (trade.get("targets") or {}).values():
            ids += [t.get("tp_order_id"), t.get("sl_order_id")]
        ids.append((trade.get("runner") or {}).get("order_id"))
        ids += list(trade.get("close_order_ids") or [])
        total = Decimal("0")
        any_fill = False
        for oid in dict.fromkeys(i for i in ids if i):
            try:
                o = nx.get_order_status(int(oid)).get("order") or {}
            except Exception:
                continue
            matched = D(o.get("matchedAmount", "0") or "0")
            if matched <= 0:
                continue
            px = D(o.get("averagePrice") or o.get("price") or "0")
            if px <= 0:
                continue
            total += ((px - entry) if side == "LONG" else (entry - px)) * matched
            any_fill = True
        if any_fill:
            return total, "fills"
    except Exception:
        pass
    return None, ""


def _record_trade_history(state: Dict[str, Any], key: str, trade: dict, reason: str, note: str = "",
                          exit_price: Optional[Decimal] = None) -> None:
    """Append a closed trade to the persistent history log (state["trade_history"]).
    Pure record-keeping for the /history and /pnl commands - never reads back
    into, or affects, any trading decision, so it cannot introduce a behavior
    change.

    Also computes an estimated realized USDT P&L: for each hit target, the
    known target price against entry; for whatever portion wasn't closed via
    a named target, the given exit_price (or the trade's current stop_price
    as a fallback) against entry. This is explicitly an estimate - Nobitex
    doesn't expose a simple realized-PNL-per-position figure we could read
    back, and real fills can differ slightly from these reference prices
    (slippage) - so it's always labeled as such wherever it's shown.
    """
    targets = trade.get("targets", {}) or {}
    hit_list = [f"T{n}" for n in range(1, 5) if (targets.get(str(n)) or {}).get("hit")]
    side = trade.get("side")

    realized_usdt = None
    try:
        entry_p = D(trade.get("entry_actual", "0"))
        initial_amount = D(trade.get("initial_amount", "0"))
        if entry_p > 0 and initial_amount > 0:
            realized_usdt = Decimal("0")
            accounted_pct = Decimal("0")
            for n in range(1, 5):
                t = targets.get(str(n)) or {}
                if t.get("hit"):
                    pct = D(t.get("pct", "0"))
                    tp = D(t.get("tp_price", "0"))
                    diff = (tp - entry_p) if side == "LONG" else (entry_p - tp)
                    realized_usdt += diff * (initial_amount * pct)
                    accounted_pct += pct
            remaining_pct = max(Decimal("0"), Decimal("1") - accounted_pct)
            if remaining_pct > 0:
                ref = exit_price if exit_price is not None else D(trade.get("stop_price", trade.get("original_stop", "0")))
                if ref and ref > 0:
                    diff = (ref - entry_p) if side == "LONG" else (entry_p - ref)
                    realized_usdt += diff * (initial_amount * remaining_pct)
    except (InvalidOperation, TypeError, ZeroDivisionError):
        realized_usdt = None

    r_multiple = None
    if realized_usdt is not None:
        try:
            risk_cap_usdt = D(trade.get("risk_usdt", "0"))
            if risk_cap_usdt > 0:
                r_multiple = realized_usdt / risk_cap_usdt
        except (InvalidOperation, TypeError):
            pass

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
        "position_id": trade.get("position_id"),
        "opened_at": trade.get("opened_at"),
        "closed_at": time.time(),
        "reason": reason,
        "note": note,
        "realized_usdt_estimate": str(realized_usdt) if realized_usdt is not None else None,
        "r_multiple_estimate": str(r_multiple) if r_multiple is not None else None,
        "signal_id": trade.get("signal_id"),
        "exit_price": str(exit_price) if exit_price is not None else None,
    }
    exch, exch_src = _exchange_realized_pnl(trade)
    if exch is not None:
        entry["realized_usdt"] = str(exch)
        entry["pnl_source"] = exch_src
        try:
            risk_cap_usdt = D(trade.get("risk_usdt", "0"))
            if risk_cap_usdt > 0:
                entry["r_multiple"] = str(exch / risk_cap_usdt)
        except (InvalidOperation, TypeError):
            pass
    else:
        entry["pnl_source"] = "estimate"
    hist = state.setdefault("trade_history", [])
    hist.append(entry)
    state["trade_history"] = hist[-300:]


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
            if str(p.get("side", "")).lower() != wanted_side:
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
    targets = trade.get("targets") or {}
    top = 0
    for n in range(1, 5):
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
    for n in range(1, 5):
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
        trade["closed_pct"] = str(sum((D(x.get("pct", "0")) for x in targets.values() if x.get("hit")), Decimal("0")))
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

    expected = 4
    targets = trade.get("targets", {})
    if len(targets) != expected:
        raise RuntimeError(f"Expected {expected} targets, got {len(targets)}")

    for n in range(1, 5):
        t = targets.get(str(n)) or {}
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
            state.get("needs_protection", {}).pop(key, None)
            _record_trade_history(state, key, trade, "closed_externally", "confirmed closed by Nobitex during protection repair")
            state.get("open_trades", {}).pop(key, None)
            save_state(state)
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

    hit_pct = sum((D(t["pct"]) for t in trade.get("targets", {}).values() if t.get("hit")), Decimal("0"))
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
        if t.get("hit"):
            continue
        pct = D(t["pct"]) / remaining_original
        amount = (liability * pct).quantize(Decimal("0.0000000001"), rounding=ROUND_DOWN)
        try:
            created, sp = _with_stop(lambda sp: _create_target_oco(nx, trade, int(n), amount, D(t["tp_price"]), sp))
        except NobitexAPIError as e:
            if not _is_price_condition(e) or amount <= 0:
                raise
            # TP price already passed (or otherwise invalid): protect this slice with a stop only.
            r, sp = _with_stop(lambda sp: _create_runner_stop(nx, trade, amount, sp))
            created = {"target": int(n), "amount": str(amount), "tp_price": t["tp_price"], "stop_price": str(sp),
                       "tp_order_id": None, "sl_order_id": r.get("order_id"), "status": "open", "tp_missing": True}
            notes.append(f"⚠️ T{n}: قیمت تارگت ({t['tp_price']}) دیگر برای سفارش حدی معتبر نبود؛ این بخش فقط با حد ضرر محافظت شد (بدون TP).")
        created["pct"] = t["pct"]
        created["hit"] = False
        trade["targets"][n] = created
        if used_stop is None:
            used_stop = sp
            candidates = [sp]
        trade["stop_price"] = str(used_stop)
        _save()

    runner_amount = (liability * (WRUNNER / remaining_original)).quantize(Decimal("0.0000000001"), rounding=ROUND_DOWN)
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
    for n, pct in {1: W1, 2: W2, 3: W3, 4: W4}.items():
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
    pcts = {1: W1, 2: W2, 3: W3, 4: W4}
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
        hint = ""
        hint_buttons = None
        if "smallorder" in str(protection_error).lower():
            hint = (
                "\nℹ️ علت رایج این خطا: کوچک‌ترین تارگت (۱۰٪ حجم) از حداقل سایز سفارش نوبیتکس "
                "برای این بازار کمتر بوده. برای جلوگیری از تکرار، ریسک/مارجین یا اهرم را با "
                "/risk، /collateral یا /leverage افزایش دهید."
            )
            hint_buttons = [
                {"text": "📌 ریسک", "callback_data": "risk_menu"},
                {"text": "💵 مارجین", "callback_data": "collateral_menu"},
                {"text": "📈 اهرم", "callback_data": "leverage_menu"},
            ]
        _flag_protection_issue(nx, state, key, trade, str(protection_error), extra_hint=hint, extra_buttons=hint_buttons)
        raise SignalFinalized(str(protection_error)) from protection_error

    lev_note = "" if leverage == leverage_requested else (
        f" (⚠️ درخواست‌شده {leverage_requested}x بود؛ نوبیتکس {leverage}x اعمال کرد و محاسبات با همان انجام شد)")
    text = ui.card_opened(
        key, side, actual_entry, sig.stop, leverage, lev_note, trade["targets"],
        trade["runner"].get("stop_price"), effective_risk, live_liability, src.upper(),
        trade.get("signal_id"), position["id"], _stop_vs_liquidation_note(trade, position))
    if _active_user_chat_id:
        btns = [[{"text": f"❌ بستن {key}", "callback_data": f"cmd:/myclose {key}"},
                 {"text": "📊 معاملات من", "callback_data": "cmd:/mytrades"}]]
    else:
        btns = [[{"text": "❌ بستن این معامله", "callback_data": f"cmd:/close {key}"},
                 {"text": "🛡️ بررسی محافظت", "callback_data": "cmd:/protection"}],
                [{"text": "📋 منو", "callback_data": "menu"}]]
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
        notify_admin(f"⛔ {key}: سقف معاملات همزمان پر است ({control.get('max_open_trades')}).", key=key); return
    src, dst = symbol_to_currencies(sig.symbol)
    side = sig.side.upper()
    if dst != "usdt":
        notify_admin(f"⚠️ {key}: فقط بازارهای USDT برای این Executor فعال هستند؛ {src}/{dst} رد شد.", key=key)
        return

    try:
        if not nx.is_symbol_available(src, dst, "buy" if side == "LONG" else "sell"):
            notify_admin(f"❌ {key}: بازار تعهدی/جهت موردنظر در نوبیتکس فعال نیست.", key=key)
            return

        leverage, _leverage_pref, leverage_market_max = resolve_leverage(nx, src, dst, control)

        risk_cap=D(control.get("risk_usdt", DEFAULT_RISK_USDT)); collateral_cap=D(control.get("max_collateral_usdt", DEFAULT_MAX_COLLATERAL_USDT))
        margin_balance=nx.get_margin_usdt_balance()
        free_slots=max(1, int(control.get("max_open_trades", DEFAULT_MAX_OPEN_TRADES))-len(state.get("open_trades", {})))
        per_trade_budget=min(collateral_cap, margin_balance / Decimal(free_slots))
        if per_trade_budget <= 0:
            notify_admin(f"❌ {key}: موجودی آزاد Margin USDT کافی نیست. Available={margin_balance} USDT", key=key); return
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
                detailed=not _active_user_chat_id)
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
            notify_admin(f"❌ اجرای سیگنال {key} شکست خورد: {e}", key=key)
            _last_transient_notice[throttle_key] = now_t
        if transient:
            raise  # retried next cycle; an already-opened position is adopted, never duplicated
        # Definitive rejection: never retry this same message every 2 seconds forever.
        raise SignalFinalized(str(e)) from e


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
    notify_admin(
        ui.card_target_hit(key, trade.get("side"), target_num, t["pct"], t["tp_price"],
                           _stop_vs_liquidation_note(trade, _position_status_safe(nx, trade))),
        key=key,
    )


def handle_event(nx: NobitexClient, state: Dict[str, Any], ev: ParsedEvent) -> None:
    key = trade_key(ev.symbol, ev.timeframe)
    trade = state["open_trades"].get(key)

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
                trade["closed_pct"] = str(W1 + W2 + W3 + W4)
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
        implied_hits = ["1", "2"] if ev.kind == "sl_after_t2" else ["1", "2", "4"]
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
            [{"text": "✅ بله، طبق کانال ببند", "callback_data": f"cmd:/confirmclose {key}"}],
            [{"text": "❌ نه، نگه دار", "callback_data": f"cmd:/dismissclose {key}"}],
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
                    [{"text": "✅ بله، طبق کانال ببند", "callback_data": f"cmd:/confirmclose {key}"}],
                    [{"text": "❌ نه، نگه دار", "callback_data": f"cmd:/dismissclose {key}"}],
                    [{"text": "📋 منو", "callback_data": "menu"}],
                ],
            )
            rec["alerts_sent"] = sent + 1
    state["pending_close_confirm"] = pending
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
                     buttons=[[{"text": "✅ بله، ببند", "callback_data": f"cmd:/confirmclose {key}"}]])
        state.setdefault("pending_close_confirm", {})[key] = {"kind": reason, "price": str(price), "requested_at": time.time(), "alerts_sent": 0}
        save_state(state)
        return
    if status is None or str(status.get("status", "")).lower() in TERMINAL_POSITION_STATUSES:
        _record_trade_history(state, key, trade, reason, "confirmed closed by Nobitex before this event was processed")
        state["open_trades"].pop(key, None)
        save_state(state)
        _notify_closed(state, key, reason)
        return
    if not _pos_open(status):
        notify_admin(f"⚠️ {key}: وضعیت پوزیشن نامشخص است ({status.get('status')!r}); چیزی بسته/لغو نشد.", key=key)
        return
    for t in trade.get("targets", {}).values():
        _cancel_oco(nx, t)
    _cancel_order_quiet(nx, trade.get("runner", {}).get("order_id"), "runner-price-close")

    liability = D(status.get("liability", "0"))
    if liability <= 0:
        _record_trade_history(state, key, trade, reason, "liability already zero")
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
        _record_trade_history(state, key, trade, reason, f"closed at strategy price {price}", exit_price=price)
        state["open_trades"].pop(key, None)
        save_state(state)
        _notify_closed(state, key, reason)
    except NobitexAPIError as e:
        notify_admin(f"🚨 {key}: {reason} close failed: {e.code} - {e.message}", key=key)
        # Orders were cancelled above - make sure the position does not stay naked.
        _flag_protection_issue(nx, state, key, trade, f"{reason} close failed after orders were cancelled: {e}", quiet=True)


def _notify_closed(state: Dict[str, Any], key: str, reason: str, note: str = "") -> None:
    """One consistent 'trade closed' card built from the history entry that
    _record_trade_history just appended (result, R, exit, targets, reason)."""
    hist = state.get("trade_history") or []
    h = hist[-1] if hist and hist[-1].get("key") == key else None
    if not h:
        notify_admin(f"🏁 {key}: معامله بسته شد — {ui.REASON_FA.get(reason, reason)}", key=key)
        return
    usdt = h.get("realized_usdt") if h.get("realized_usdt") is not None else h.get("realized_usdt_estimate")
    r = h.get("r_multiple") if h.get("r_multiple") is not None else h.get("r_multiple_estimate")
    hit = set(h.get("targets_hit") or [])
    tline = "  ".join(f"T{n} {'✅' if f'T{n}' in hit else '⏳'}" for n in range(1, 5))
    notify_admin(ui.card_closed(key, h.get("side"), h.get("reason") or reason, h.get("entry_actual"), h.get("exit_price"),
                                usdt, r, h.get("realized_usdt") is not None, tline, h.get("signal_id"),
                                h.get("opened_at"), h.get("closed_at"), note), key=key)


def _close_remaining_market(nx: NobitexClient, state: Dict[str, Any], key: str, reason: str) -> None:
    trade = state["open_trades"].get(key)
    if not trade:
        return

    try:
        status = _position_status(nx, trade)
    except TransientAPIError as e:
        notify_admin(f"⏳ {key}: بستن ({reason}) انجام نشد چون نوبیتکس موقتاً پاسخ/اجازه نداد ({e}). "
                     f"هیچ سفارشی لغو نشد و معامله دست‌نخورده است؛ چند لحظه بعد دوباره امتحان کنید.", key=key)
        return
    if status is None or str(status.get("status", "")).lower() in TERMINAL_POSITION_STATUSES:
        _record_trade_history(state, key, trade, reason, "confirmed closed by Nobitex before this command/event was processed")
        state["open_trades"].pop(key, None)
        save_state(state)
        _notify_closed(state, key, reason)
        return
    if not _pos_open(status):
        notify_admin(f"⚠️ {key}: وضعیت پوزیشن نامشخص است ({status.get('status')!r}); چیزی بسته/لغو نشد.", key=key)
        return

    for t in trade.get("targets", {}).values():
        _cancel_oco(nx, t)
    _cancel_order_quiet(nx, trade.get("runner", {}).get("order_id"), "runner-final")

    liability = D(status.get("liability", "0"))
    if liability > 0:
        try:
            resp = nx.close_position_market(int(trade["position_id"]), amount=fmt_amount(liability))
            _oid = order_id_from(resp) if isinstance(resp, dict) else None
            if _oid:
                trade.setdefault("close_order_ids", []).append(_oid)
        except NobitexAPIError as e:
            notify_admin(f"🚨 {key}: final market close failed ({reason}): {e.code} - {e.message}", key=key)
            # Orders were cancelled above - restore protection instead of leaving the position naked.
            _flag_protection_issue(nx, state, key, trade, f"final close failed after orders were cancelled: {e}", quiet=True)
            return

    exit_ref = None
    try:
        src_c, dst_c = symbol_to_currencies(trade["symbol"])
        exit_ref = D(nx.get_last_trade_price(f"{src_c.upper()}{dst_c.upper()}"))
    except Exception:
        pass  # fall back to the trade's stop_price inside _record_trade_history
    _record_trade_history(state, key, trade, reason, "closed via market order", exit_price=exit_ref)
    state["open_trades"].pop(key, None)
    save_state(state)
    _notify_closed(state, key, reason)


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
                _record_trade_history(state, key, trade, "closed_externally", "confirmed closed by Nobitex during runner trailing update")
                state["open_trades"].pop(key, None)
                continue
            if not _pos_open(status):
                continue
            liability = D(status.get("liability", "0"))
            if liability <= 0:
                _record_trade_history(state, key, trade, "closed_externally", "liability zero during runner trailing update")
                state["open_trades"].pop(key, None)
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
            _record_trade_history(state, key, trade, "closed_externally", "not active on Nobitex at startup reconciliation")
            state["open_trades"].pop(key, None)
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
                 if now - float(t.get("last_sync", 0)) >= SYNC_INTERVAL_SECONDS)
    for _, key in due[:max_per_call]:
        trade = state["open_trades"].get(key)
        if not trade:
            continue
        trade["last_sync"] = now
        try:
            status = _position_status(nx, trade)
            if status is None or str(status.get("status", "")).lower() in TERMINAL_POSITION_STATUSES:
                _record_trade_history(state, key, trade, "closed_on_exchange", "Nobitex reports the position closed (TP/SL/liquidation on the exchange)")
                state["open_trades"].pop(key, None)
                state.get("needs_protection", {}).pop(key, None)
                state.get("pending_close_confirm", {}).pop(key, None)
                save_state(state)
                _notify_closed(state, key, "closed_on_exchange")
                continue
            if not _pos_open(status) or D(status.get("liability", "0")) <= 0:
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
    for n in range(1, 5):
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
            hits_txt = ", ".join(f"T{n}" for n in range(1, 5) if trade["targets"][str(n)]["hit"]) or "هیچ‌کدام"
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
        try:
            result = handle_bot_command(command, state, nx, gh, caller_chat_id=caller_chat_id)
            buttons = None
            cmd_name = command.strip().split()[0].lower() if command.strip() else ""
            if caller_chat_id is not None:
                # Self-service reply goes to the subscriber's own chat, never the admin chat.
                if isinstance(result, tuple):
                    notify_admin(result[0], chat_id=caller_chat_id, buttons=result[1])
                else:
                    notify_admin(result, chat_id=caller_chat_id)
                processed.add(command_id)
                changed = True
                continue
            if isinstance(result, tuple):
                result, buttons = result
            if buttons is None and cmd_name == "/positions" and state.get("open_trades"):
                # One tap per trade to close exactly that one - no need to
                # remember/type the SYMBOL_TIMEFRAME key.
                buttons = [[{"text": f"❌ بستن {k}", "callback_data": f"cmd:/close {k}"}]
                           for k in sorted(state["open_trades"].keys())]
                buttons.append([{"text": "📋 منو", "callback_data": "menu"}])
            notify_admin(f"🛠 فرمان {command_id} اجرا شد\n{result}", buttons=buttons)
            processed.add(command_id)
        except Exception as e:
            if caller_chat_id is not None:
                notify_admin("❌ خطای داخلی؛ لطفاً دوباره امتحان کنید.", chat_id=caller_chat_id)
                log.exception("self-service command failed for chat %s: %s", caller_chat_id, command)
            else:
                notify_admin(f"🚨 اجرای فرمان {command_id} ناموفق بود: {type(e).__name__}: {e}")
            # Mark it processed after reporting, so a permanently bad command
            # cannot loop forever.
            processed.add(command_id)
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
    global _active_user_chat_id, _active_state_path, _nx_for_history, _active_user_auto_exit
    prev = (_active_user_chat_id, _active_state_path, _nx_for_history, _active_user_auto_exit)
    nx_user = _get_user_client(rec)
    st = _get_user_state(rec, gh)
    try:
        _active_user_chat_id = str(rec.notify_chat_id or rec.user_id)
        _active_state_path = user_state_path(rec.user_id)
        _nx_for_history = nx_user
        _active_user_auto_exit = bool(getattr(rec, "auto_exit", True))
        yield nx_user, st
    finally:
        _active_user_chat_id, _active_state_path, _nx_for_history, _active_user_auto_exit = prev


def user_control(rec) -> dict:
    """Per-subscriber replacement for control.json. New entries are allowed
    only while the subscription is valid and they have not paused."""
    return {"enabled": bool(rec.is_active() and not rec.paused),
            "risk_usdt": rec.risk_usdt, "max_collateral_usdt": rec.max_collateral_usdt,
            "max_open_trades": int(rec.max_open_trades), "leverage": rec.leverage}


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
                             "معامله‌ی جدید باز کند یا محافظت را به‌روز کند.\nکلید جدید بسازید و /connect بزنید.",
                             chat_id=_active_user_chat_id or uid)
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
            poll_once(nx_u, gh, st, control_override=user_control(rec))
            resolve_pending_protection(nx_u, st)
            resolve_needs_protection(nx_u, st)
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


def run_all_user_cycles(gh: GithubClient) -> None:
    gh = _SignalsSnapshot(gh)
    for rec in list(_vault.all()):
        try:
            run_user_cycle(gh, rec)
        except Exception:
            log.exception("run_user_cycle wrapper failed for %s", getattr(rec, "user_id", "?"))


def manage_subscriptions() -> None:
    """Called every loop tick: flips expired subscriptions (once), and warns
    each user once when 3 days or less remain."""
    for rec in _vault.sweep_expired():
        chat = rec.notify_chat_id or rec.user_id
        open_n = _user_open_count(rec.user_id)
        notify_admin("⏳ اشتراک شما به پایان رسید؛ ورود معاملات جدید متوقف شد."
                     + (f"\nمعاملات بازِ فعلی‌تان ({open_n}) همچنان تا بسته شدن با همان حد ضرر/تارگت/رانر مدیریت می‌شوند."
                        if open_n else "")
                     + "\nبرای تمدید: /prices و /subscribe", chat_id=chat)
        notify_admin(f"⏳ اشتراک کاربر {rec.user_id}" + (f" ({rec.display_name})" if rec.display_name else "")
                     + f" منقضی شد ({open_n} معامله‌ی باز).")
    for rec in _vault.due_soon(3 * 86400):
        chat = rec.notify_chat_id or rec.user_id
        notify_admin(f"🔔 فقط {rec.days_left():.1f} روز از اشتراک شما مانده. برای تمدید: /prices و /subscribe", chat_id=chat)
        _vault.mark_notice_sent(rec.user_id)


PREREQUISITES_TEXT = (
    "📋 شرایط و پیش‌نیازهای استفاده (لطفاً با دقت بخوانید):\n\n"
    "۱) حساب نوبیتکس شما باید احراز هویت‌شده و معاملات تعهدی (Margin) برایتان فعال باشد.\n"
    "۲) باید در کیف پول تعهدی (Margin) نوبیتکس به اندازه‌ی کافی USDT داشته باشید؛ هر معامله بخشی از آن را "
    "به‌عنوان وثیقه می‌گیرد (سقف وثیقه و ریسک شما در /mysubscription دیده می‌شود). اگر موجودی کم باشد، سیگنال اجرا نمی‌شود.\n"
    "۳) کلید API را فقط با دسترسی Read و Trade بسازید و دسترسی Withdraw (برداشت) را خاموش بگذارید؛ "
    "کلید دارای دسترسی برداشت به‌طور خودکار رد می‌شود. اگر برای کلید محدودیت IP گذاشته‌اید، آدرس IP سرور ربات "
    "(از ادمین بپرسید) باید مجاز باشد وگرنه اتصال برقرار نمی‌شود.\n"
    "۴) ربات فقط بر اساس سیگنال‌های کانال معامله باز می‌کند و مدیریت می‌کند (حد ضرر، ۴ تارگت، رانر). سود تضمین‌شده نیست؛ "
    "ضرر و لیکویید شدن با اهرم ممکن است و مسئولیت آن با خود شماست.\n"
    "۵) سفارش‌ها و حد ضرر معاملاتی را که ربات باز کرده است دستی تغییر ندهید؛ مدیریت خودکار به هم می‌ریزد. "
    "اگر می‌خواهید ببندید از /mytrades و /myclose استفاده کنید.\n"
    "۶) با پایان اشتراک فقط ورود معاملات جدید متوقف می‌شود؛ معاملات بازِ فعلی تا بسته شدن همچنان مدیریت می‌شوند.\n"
    "۷) کارمزدها و هزینه‌های نوبیتکس بر عهده‌ی شماست. پرداخت اشتراک به‌صورت دستی و با تأیید ادمین ثبت می‌شود.\n"
    "۸) کلید شما رمزنگاری می‌شود و پیام حاوی آن از چت حذف می‌شود؛ ادمین هیچ‌وقت کلید خام شما را نمی‌بیند.\n\n"
    "اگر همه‌ی موارد را قبول دارید بفرستید: /accept"
)

# What a subscriber may map onto the (already tested) admin handlers, run
# against THEIR OWN account. Fixed table: nothing else can ever be forwarded.
_USER_CMD_MAP = {"/mytrades": "/positions", "/myhistory": "/history", "/mypnl": "/pnl"}


def _run_as_user(rec, gh: GithubClient, command: str) -> str:
    with _user_context(rec, gh) as (nx_u, st):
        return handle_bot_command(command, st, nx_u, gh)


def _run_as_user_silent(rec, command: str) -> str:
    """Admin oversight read of a subscriber's account: uses their client and
    state, but any message generated along the way must not be copied to the
    subscriber's chat, so the ambient chat routing is switched off (the
    returned text goes to the admin as the command's normal reply)."""
    global _active_user_chat_id
    with _user_context(rec, _gh_main) as (nx_u, st):
        saved, _active_user_chat_id = _active_user_chat_id, None
        try:
            return handle_bot_command(command, st, nx_u, _gh_main)
        finally:
            _active_user_chat_id = saved


def _trade_close_buttons(uid: str) -> Optional[list]:
    st = _user_states.get(str(uid)) or (load_state(user_state_path(uid)) if os.path.exists(user_state_path(uid)) else {})
    keys = sorted((st.get("open_trades") or {}).keys())
    if not keys:
        return None
    return [[{"text": f"❌ بستن {k}", "callback_data": f"cmd:/myclose {k}"}] for k in keys]


def handle_self_service_command(cmd: str, parts: list, state: Dict[str, Any], nx: NobitexClient,
                                 caller_chat_id: str):
    """The fixed command surface a subscriber's own chat can reach (see
    SELF_SERVICE_COMMANDS / the gate in handle_bot_command). `nx`/`state` are
    the ADMIN's and are never used here: every action touching a subscriber's
    Nobitex account goes through _user_context() with THEIR OWN client and
    state. May return a str, or (str, buttons)."""
    if _vault is None:
        return "⏳ سرویس هنوز آماده نیست؛ کمی بعد دوباره امتحان کنید."
    gh = _gh_main
    uid = caller_chat_id
    rec = _vault.get(uid)

    if cmd in ("/start", "/help"):
        if not rec:
            return ("👋 سلام! برای استفاده از معامله‌گر خودکار، اول باید ادمین شما را ثبت کند. "
                    "با ادمین در تماس باشید و شناسه‌ی عددی تلگرام خود را به او بدهید: " + str(uid))
        if not rec.terms_accepted_at:
            return "👋 خوش آمدید!\n\n" + PREREQUISITES_TEXT
        if not rec.public_key_enc:
            return ("✅ شرایط را پذیرفته‌اید. حالا کلید API نوبیتکس‌تان را بفرستید "
                    "(قبل از ارسال به سرور رمزنگاری می‌شود و پیام شما از چت پاک می‌شود):\n"
                    "/connect <API_KEY> <API_SECRET>\n\n"
                    "کلید باید فقط Read و Trade داشته باشد و Withdraw خاموش باشد.")
        return ("📋 دستورهای شما:\n"
                "/mysubscription — وضعیت و جزئیات اشتراک\n/prices — تعرفه‌ها و روش پرداخت\n"
                "/subscribe <روز> <toman|usdt> — خرید/تمدید\n/paid <شناسه> <کد رهگیری> — ثبت رسید\n"
                "/mytrades — معاملات باز (با دکمه‌ی بستن)\n/myhistory [تعداد] — تاریخچه\n/mypnl — سود و زیان\n"
                "/myclose <کلید> — بستن یک معامله\n/mycloseall تایید — بستن همه\n"
                "/mypause — توقف ورود جدید | /myresume — ازسرگیری\n"
                "/myrisk <ریسک> <سقف وثیقه> <اهرم> — تنظیم (حداکثر تا سقف ادمین)\n"
                "/disconnect تایید — حذف کلید API (فقط بدون معامله‌ی باز)")

    if cmd == "/prices":
        tiers = _pricing.all() if _pricing else {}
        if not tiers:
            return "💳 هنوز تعرفه‌ای ثبت نشده؛ با ادمین هماهنگ کنید."
        lines = ["💳 تعرفه‌ها:"]
        for days, p in tiers.items():
            opts = []
            if p.get("toman") is not None:
                opts.append(f"{int(p['toman']):,} تومان")
            if p.get("usdt") is not None:
                opts.append(f"{p['usdt']:g} USDT")
            lines.append(f"  {days} روز: " + (" یا ".join(opts) if opts else "—"))
        methods = []
        if _payment_info and _payment_info.is_card_set():
            methods.append("کارت به کارت (تومان)")
        if _payment_info and _payment_info.is_usdt_set():
            methods.append(f"USDT ({_payment_info.usdt_network})")
        lines.append("روش‌های پرداخت: " + ("، ".join(methods) if methods else "هنوز ثبت نشده"))
        lines.append("برای خرید: /subscribe <روز> <toman یا usdt>   مثال: /subscribe 30 usdt")
        return "\n".join(lines)

    if not rec:
        return "❌ شما در سیستم ثبت نشده‌اید؛ با ادمین تماس بگیرید."

    if cmd == "/accept":
        _vault.accept_terms(uid)
        return ("✅ شرایط ثبت شد.\n"
                + ("کلید API شما قبلاً متصل است." if rec.public_key_enc else
                   "حالا کلید را بفرستید:\n/connect <API_KEY> <API_SECRET>"))

    if cmd == "/connect_enc" and len(parts) > 2:
        if not rec.terms_accepted_at:
            return "❌ اول باید شرایط را بخوانید و /accept بزنید؛ سپس کلید را دوباره بفرستید.\n\n" + PREREQUISITES_TEXT
        try:
            pub = us.decrypt_field(parts[1])
            priv = us.decrypt_field(parts[2])
        except RuntimeError as e:
            return f"❌ {e}"
        candidate = NobitexClient(NobitexConfig(public_key=pub, private_key_b64=priv,
                                                 base_url=NOBITEX_BASE_URL, public_base_url=NOBITEX_PUBLIC_BASE_URL))
        ok, detail = us.validate_key_permissions(candidate)
        if not ok:
            return f"❌ کلید پذیرفته نشد: {detail}"
        try:
            candidate.get_margin_usdt_balance()
        except Exception as e:
            return f"❌ مجوز کلید درست بود ولی اتصال آزمایشی به حساب تعهدی شکست خورد (حساب تعهدی فعال است؟): {e}"
        first_time = rec.status == us.STATUS_PENDING
        _vault.connect_keys(uid, pub, priv)
        rec = _vault.get(uid)
        rec.notify_chat_id = uid
        _vault._save()
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
                     f" کلید خودش را متصل کرد ({detail}).")
        return (f"✅ کلید متصل شد. {detail}\n"
                + (f"اشتراک شما فعال است — {rec.days_left():.1f} روز باقی مانده. از این لحظه سیگنال‌های "
                   f"جدید کانال روی حساب شما اجرا می‌شود."
                   if rec.status == us.STATUS_ACTIVE else "⚠️ اشتراک شما فعال نیست؛ با /prices و /subscribe تمدید کنید."))

    if cmd in ("/mysubscription", "/mystatus"):
        lines = ["👤 اشتراک شما", f"وضعیت: {rec.status}" + (" (ورود جدید متوقف شده توسط خودتان)" if rec.paused else "")]
        if rec.status == us.STATUS_ACTIVE:
            lines.append(f"روزهای باقیمانده: {rec.days_left():.1f}")
            lines.append(f"تاریخ پایان: {time.strftime('%Y-%m-%d', time.localtime(rec.expires_at))}")
        elif rec.status == us.STATUS_PENDING:
            lines.append(f"روزهای در انتظار (پس از اتصال کلید شروع می‌شود): {rec.pending_days:.0f}")
        elif rec.status == us.STATUS_EXPIRED:
            lines.append("اشتراک شما منقضی شده؛ ورود جدید متوقف است. تمدید: /prices و /subscribe")
        lines.append(f"کلید API: {'متصل ✅' if rec.public_key_enc else 'متصل نشده ❌'}")
        lines.append(f"ریسک هر معامله: {rec.risk_usdt} (سقف ادمین {rec.cap_risk_usdt}) USDT | سقف وثیقه: "
                     f"{rec.max_collateral_usdt} (سقف {rec.cap_collateral_usdt}) | اهرم: {rec.leverage}x (سقف {rec.cap_leverage}x) | "
                     f"حداکثر معاملات همزمان: {rec.max_open_trades}")
        lines.append(f"معاملات باز: {_user_open_count(uid)}")
        hist = _ledger.for_user(uid) if _ledger else []
        if hist:
            lines.append("💳 پرداخت‌های اخیر:")
            for p in hist[-5:]:
                d = time.strftime("%Y-%m-%d", time.localtime(p.requested_at))
                lines.append(f"  {p.payment_id} [{d}] {p.days} روز — {p.amount:g} {p.currency} — {p.status}")
        return "\n".join(lines)

    if cmd == "/subscribe" and len(parts) > 2:
        try:
            days = int(parts[1])
        except ValueError:
            return "❌ فرمت: /subscribe <روز> <toman یا usdt>"
        currency = parts[2].lower()
        if currency not in ("toman", "usdt"):
            return "❌ ارز باید toman یا usdt باشد. مثال: /subscribe 30 usdt"
        tier = _pricing.price_for(days) if _pricing else None
        amount = tier.get(currency) if tier else None
        if amount is None:
            return f"❌ برای {days} روز قیمتی به {currency} ثبت نشده؛ /prices را ببینید."
        if currency == "toman" and not (_payment_info and _payment_info.is_card_set()):
            return "❌ اطلاعات کارت هنوز توسط ادمین ثبت نشده؛ با ادمین تماس بگیرید."
        if currency == "usdt" and not (_payment_info and _payment_info.is_usdt_set()):
            return "❌ آدرس USDT هنوز توسط ادمین ثبت نشده؛ با ادمین تماس بگیرید."
        already = [p for p in _ledger.for_user(uid) if p.status == us.STATUS_PENDING_PAY and not p.note]
        if len(already) >= 3:
            return ("❌ چند درخواست پرداخت بدون کد رهگیری دارید؛ اول با /paid برایشان کد رهگیری بفرستید: "
                    + ", ".join(p.payment_id for p in already))
        pay = _ledger.request(uid, days, currency, amount)
        if currency == "toman":
            instr = (f"💳 مبلغ {int(amount):,} تومان را به کارت زیر واریز کنید:\n{_payment_info.card_number}\n"
                     f"به‌نام {_payment_info.card_holder}")
        else:
            instr = (f"💵 مبلغ دقیقاً {amount:g} USDT را فقط روی شبکه‌ی {_payment_info.usdt_network} به آدرس زیر واریز کنید "
                     f"(ارسال روی شبکه‌ی دیگر ممکن است قابل بازگشت نباشد):\n{_payment_info.usdt_address}")
        notify_admin(f"💳 درخواست پرداخت جدید {pay.payment_id} از کاربر {uid}: {days} روز — {amount:g} {currency}")
        return (f"{instr}\n\nبعد از واریز، کد رهگیری/هش تراکنش را بفرستید:\n/paid {pay.payment_id} <کد رهگیری>\n\n"
                f"شناسه‌ی درخواست شما: {pay.payment_id}")

    if cmd == "/paid" and len(parts) > 2:
        pay = _ledger.get(parts[1]) if _ledger else None
        if not pay or pay.user_id != str(uid):
            return "❌ چنین درخواست پرداختی برای شما پیدا نشد."
        try:
            _ledger.set_note(parts[1], " ".join(parts[2:])[:300])
        except ValueError as e:
            return f"❌ {e}"
        notify_admin(f"📩 کاربر {uid} برای پرداخت {pay.payment_id} ({pay.days} روز — {pay.amount:g} {pay.currency}) "
                     f"کد رهگیری فرستاد: {' '.join(parts[2:])[:300]}",
                     buttons=[[{"text": "✅ تأیید", "callback_data": f"cmd:/confirmpayment {pay.payment_id}"},
                               {"text": "❌ رد", "callback_data": f"cmd:/rejectpayment {pay.payment_id} رسید نامعتبر"}]])
        return "✅ کد رهگیری ثبت شد؛ بعد از بررسی ادمین، اشتراک شما اعمال می‌شود و همین‌جا خبر می‌دهم."

    # ---- everything below needs a connected key ----
    if cmd in ("/mytrades", "/myhistory", "/mypnl", "/myclose", "/mycloseall", "/myrisk", "/mypause", "/myresume", "/disconnect", "/myautoexit"):
        if cmd == "/myautoexit":
            if len(parts) < 2 or parts[1].lower() not in ("on", "off"):
                return (f"🤖 خروج خودکار طبق کانال: {'روشن ✅' if rec.auto_exit else 'خاموش ⏸'}\n"
                        f"روشن = وقتی کانال بریک‌ایون، حد ضرر یا بستن معامله را اعلام کند، ربات خودش بی‌واسطه می‌بندد.\n"
                        f"خاموش = فقط خبر می‌دهد و بستن با خودتان است (/myclose).\n"
                        f"تغییر: /myautoexit on  یا  /myautoexit off")
            _vault.set_auto_exit(uid, parts[1].lower() == "on")
            return "🤖 خروج خودکار طبق کانال " + ("روشن شد ✅" if parts[1].lower() == "on" else "خاموش شد ⏸ (بستن با خودتان است)")
        if cmd == "/mypause":
            _vault.set_paused(uid, True)
            return "⏸ ورود معاملات جدید متوقف شد. معاملات بازِ فعلی‌تان همچنان مدیریت می‌شوند. ازسرگیری: /myresume"
        if cmd == "/myresume":
            _vault.set_paused(uid, False)
            return ("▶️ ورود معاملات جدید دوباره فعال شد." if rec.is_active() else
                    "▶️ ثبت شد، ولی اشتراک شما فعال نیست؛ برای ورود جدید باید تمدید کنید (/prices).")
        if cmd == "/myrisk":
            if len(parts) == 1:
                return (f"🎯 ریسک: {rec.risk_usdt} USDT (سقف {rec.cap_risk_usdt}) | وثیقه: {rec.max_collateral_usdt} "
                        f"(سقف {rec.cap_collateral_usdt}) | اهرم: {rec.leverage}x (سقف {rec.cap_leverage}x)\n"
                        f"تغییر: /myrisk <ریسک> <سقف وثیقه> <اهرم> — هر مقدار تا سقف ادمین؛ برای بالاتر با ادمین هماهنگ کنید.")
            if len(parts) != 4:
                return "❌ فرمت: /myrisk <ریسک USDT> <سقف وثیقه USDT> <اهرم>"
            try:
                r = _vault.user_set_risk(uid, parts[1], parts[2], parts[3])
            except ValueError as e:
                return f"❌ {e}"
            return f"✅ ثبت شد: ریسک {r.risk_usdt} | وثیقه {r.max_collateral_usdt} | اهرم {r.leverage}x (برای معاملات جدید اعمال می‌شود)."
        if cmd == "/disconnect":
            if _user_open_count(uid) > 0:
                return "❌ معامله‌ی باز دارید؛ اگر کلید را حذف کنم محافظت آن‌ها قطع می‌شود. اول ببندیدشان (/mytrades) یا صبر کنید."
            if parts[-1] != "تایید":
                return "⚠️ کلید API شما از سیستم پاک می‌شود و ورود جدید متوقف می‌شود (روزهای باقیمانده حفظ می‌شود). تأیید: /disconnect تایید"
            _vault.disconnect(uid)
            _user_clients.pop(uid, None)
            _user_states.pop(uid, None)
            notify_admin(f"🔌 کاربر {uid} کلیدش را قطع کرد.")
            return "✅ کلید حذف شد. هر زمان خواستید دوباره /connect بزنید؛ روزهای باقیمانده حفظ شده است."
        if not rec.public_key_enc:
            return "❌ هنوز کلیدی متصل نکرده‌اید. /start"
        if cmd == "/mytrades":
            text = _run_as_user(rec, gh, "/positions")
            return (text, _trade_close_buttons(uid))
        if cmd in ("/myhistory", "/mypnl"):
            return _run_as_user(rec, gh, _USER_CMD_MAP[cmd] + (" " + parts[1] if cmd == "/myhistory" and len(parts) > 1 else ""))
        if cmd == "/myclose":
            if len(parts) < 2:
                return "❌ فرمت: /myclose <کلید معامله> (از /mytrades)"
            return _run_as_user(rec, gh, f"/close {parts[1].upper()}")
        if cmd == "/mycloseall":
            if parts[-1] != "تایید":
                return "⚠️ همه‌ی معاملات بازتان با قیمت بازار بسته می‌شود. تأیید: /mycloseall تایید"
            return _run_as_user(rec, gh, "/closeall")

    return "❓ دستور نامعتبر یا ناقص. /help را بزنید."


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
        if cmd in ("/status","/config"):
            c=load_control(gh); bal=nx.get_margin_usdt_balance();
            return (f"📊 وضعیت\nExecutor: 🟢 online (build {EXECUTOR_BUILD})\nورود جدید: {'🟢 فعال' if c['enabled'] else '🔴 متوقف'}\n"
                    f"Risk cap: {c['risk_usdt']} USDT\nMax collateral/trade: {c['max_collateral_usdt']} USDT\n"
                    f"Leverage preference: {c.get('leverage', DEFAULT_LEVERAGE)}x (sent as-is to Nobitex)\n"
                    f"Max open trades: {c['max_open_trades']}\nOpen trades(state): {len(state.get('open_trades',{}))}\n"
                    f"⏳ Pending protection: {len(state.get('pending_protection',{}))}\n"
                    f"🚨 Incomplete protection (never auto-closed): {len(state.get('needs_protection',{}))}\n"
                    f"📡 Pending close confirmations: {len(state.get('pending_close_confirm',{}))}\nMargin USDT: {bal}")
        if cmd=="/balance":
            m=nx.get_margin_usdt_balance(); s=nx.get_spot_usdt_balance(); return f"💰 موجودی USDT\nMargin active: {m}\nSpot active: {s}"
        if cmd=="/positions":
            trades = state.get("open_trades", {})
            if not trades:
                try:
                    _n_live = len([p for p in nx.list_positions(status="active").get("positions", [])
                                   if str(p.get("status", "")).lower() == "open"])
                except Exception:
                    _n_live = 0
                if _n_live:
                    return (f"📈 هیچ معامله‌ای در state نیست، ولی نوبیتکس {_n_live} پوزیشن باز دارد.\n"
                            f"برای برگرداندن آن‌ها زیر نظر Executor و ثبت دوباره‌ی محافظت، /recover را بزنید.")
                return "📈 هیچ معامله‌ی بازی در state نیست."
            live_by_id = {int(p["id"]): p for p in nx.list_positions(status="active").get("positions", []) if p.get("id") is not None}
            cards = [ui.card_position(key, t, live_by_id.get(int(t.get("position_id", 0) or 0), {}),
                                      _stop_vs_liquidation_note(t, live_by_id.get(int(t.get("position_id", 0) or 0), {})))
                     for key, t in sorted(trades.items())]
            return ui.card_positions(cards)[:4000]
        if cmd=="/history":
            hist = state.get("trade_history", [])
            if not hist:
                return "📜 هنوز هیچ معامله‌ای در تاریخچه ثبت نشده."
            n = 10
            if len(parts) > 1:
                try:
                    n = max(1, min(50, int(parts[1])))
                except ValueError:
                    pass
            recent = hist[-n:][::-1]
            lines = [f"📜 تاریخچه‌ی معاملات (آخرین {len(recent)} مورد از {len(hist)})", ui.DIVIDER]
            lines.append(("\n" + ui.DIVIDER + "\n").join(ui.card_history_item(h) for h in recent))
            return "\n".join(lines)[:4000]
        if cmd=="/pnl":
            hist = state.get("trade_history", [])
            n = 100
            if len(parts) > 1:
                try:
                    n = max(1, min(300, int(parts[1])))
                except ValueError:
                    pass
            sample = hist[-n:]
            if not sample:
                return "📊 هنوز هیچ معامله‌ای در تاریخچه ثبت نشده."
            total = Decimal("0"); wins = 0; losses = 0; flat = 0; counted = 0; n_exact = 0
            for h in sample:
                raw = h.get("realized_usdt") if h.get("realized_usdt") is not None else h.get("realized_usdt_estimate")
                if raw is None:
                    continue
                if h.get("realized_usdt") is not None:
                    n_exact += 1
                try:
                    v = D(raw)
                except (InvalidOperation, TypeError):
                    continue
                total += v; counted += 1
                if v > 0: wins += 1
                elif v < 0: losses += 1
                else: flat += 1
            if counted == 0:
                return f"📊 از {len(sample)} معامله‌ی بررسی‌شده، برای هیچ‌کدام سود/زیان قابل محاسبه نبود."
            winrate = (wins / counted) * 100
            avg = total / counted
            skipped = len(sample) - counted
            lines = [
                "📊 خلاصه‌ی سود/زیان" + (" (واقعی از نوبیتکس)" if n_exact == counted else " (ترکیب واقعی + تخمینی)"),
                f"بازه: آخرین {len(sample)} معامله‌ی ثبت‌شده در تاریخچه" + (f" ({skipped} مورد بدون داده‌ی کافی برای محاسبه)" if skipped else ""),
                "",
                f"جمع کل: {'🟢 +' if total>=0 else '🔴 '}{total:.4f} USDT",
                f"میانگین هر معامله: {avg:.4f} USDT",
                f"تعداد برد: {wins}",
                f"تعداد باخت: {losses}",
                f"بدون سود/زیان: {flat}",
                f"نرخ برد: ٪{winrate:.1f}",
                "",
                f"از {counted} معامله: {n_exact} مورد با عدد واقعی نوبیتکس (PNL پوزیشن/Fillهای واقعی)، "
                f"{counted - n_exact} مورد تخمینی (معاملات قدیمی‌تر که قبل از این نسخه بسته شده‌اند).",
                "ℹ️ عدد واقعیِ مبتنی بر Fill ناخالص است (کارمزد/بهره‌ی احتمالی را شامل نمی‌شود)؛ عدد PNL خود پوزیشن اگر نوبیتکس بدهد همان مرجع است.",
            ]
            return "\n".join(lines)[:4000]
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

        # ---------------- multi-tenant user management (admin-only) ----------------
        if cmd == "/adduser" and len(parts) >= 3:
            try:
                uid = _safe_uid(parts[1])
                days = float(parts[2])
                if days <= 0:
                    raise ValueError
            except ValueError:
                return "❌ فرمت: /adduser <شناسه‌ی عددی تلگرام> <روز مثبت> [نام]"
            name = " ".join(parts[3:])
            existing = _vault.get(uid)
            if existing and existing.public_key_enc:
                return (f"⚠️ کاربر {uid} از قبل ثبت و متصل است ({existing.status}). برای افزودن زمان از "
                        f"/extenduser {uid} <روز> استفاده کنید؛ /adduser اشتراک او را ریست می‌کرد.")
            _vault.add_pending(uid, days, name)
            return (f"✅ کاربر {uid}" + (f" ({name})" if name else "") + f" اضافه شد؛ اشتراک {days:.0f} روزه در انتظار اتصال کلید.\n"
                    f"⏳ شمارش روزها از لحظه‌ی اتصال کلید شروع می‌شود، نه از الان.\n"
                    f"به او بگویید در چت خصوصی همین ربات /start را بزند.")
        if cmd == "/listusers":
            users = sorted(_vault.all(), key=lambda r: r.added_at, reverse=True)
            if not users:
                return "👥 هیچ کاربری ثبت نشده."
            icon = {us.STATUS_ACTIVE: "🟢", us.STATUS_PENDING: "🟡", us.STATUS_SUSPENDED: "🟠", us.STATUS_EXPIRED: "🔴"}
            lines = [f"👥 کاربران ({len(users)}):"]
            for r in users:
                extra = f"{r.days_left():.1f} روز مانده" if r.status == us.STATUS_ACTIVE else \
                        (f"{r.pending_days:.0f} روز در انتظار اتصال" if r.status == us.STATUS_PENDING else "")
                lines.append(f"{icon.get(r.status,'⚪')} {r.user_id}" + (f" ({r.display_name})" if r.display_name else "")
                             + f" — {r.status}" + (f" — {extra}" if extra else ""))
            return "\n".join(lines)
        if cmd == "/userstatus" and len(parts) > 1:
            rec = _vault.get(parts[1])
            if not rec:
                return f"❌ کاربر {parts[1]} پیدا نشد."
            pay_hist = _ledger.for_user(rec.user_id)
            lines = [
                f"👤 کاربر {rec.user_id}" + (f" ({rec.display_name})" if rec.display_name else ""),
                f"وضعیت: {rec.status}",
                (f"روزهای باقیمانده: {rec.days_left():.1f}" if rec.status == us.STATUS_ACTIVE else
                 (f"روزهای در انتظار اتصال: {rec.pending_days:.0f}" if rec.status == us.STATUS_PENDING else
                  f"منقضی/غیرفعال از: {time.strftime('%Y-%m-%d', time.localtime(rec.expires_at)) if rec.expires_at else '؟'}")),
                f"کلید API: {'متصل ✅' if rec.public_key_enc else 'متصل نشده ❌'} | شرایط: {'پذیرفته ✅' if rec.terms_accepted_at else 'پذیرفته نشده'}"
                + (" | ⏸ توقف ورود توسط کاربر" if rec.paused else ""),
                f"ریسک: {rec.risk_usdt}/{rec.cap_risk_usdt} USDT | وثیقه: {rec.max_collateral_usdt}/{rec.cap_collateral_usdt} | "
                f"اهرم: {rec.leverage}/{rec.cap_leverage}x | سقف معاملات همزمان: {rec.max_open_trades}",
                f"معاملات باز: {_user_open_count(rec.user_id)}",
            ]
            if rec.last_error:
                lines.append(f"⚠️ آخرین خطا: {rec.last_error}")
            if pay_hist:
                lines.append("💳 تاریخچه‌ی پرداخت:")
                for p in pay_hist[-10:]:
                    d = time.strftime("%Y-%m-%d", time.localtime(p.requested_at))
                    lines.append(f"  {p.payment_id} [{d}] {p.days} روز — {p.amount} {p.currency} — {p.status}")
            return "\n".join(lines)
        if cmd == "/extenduser" and len(parts) > 2:
            try:
                days = float(parts[2])
            except ValueError:
                return "❌ فرمت: /extenduser <user_id> <روز>"
            if not _vault.get(parts[1]):
                return f"❌ کاربر {parts[1]} پیدا نشد."
            rec = _vault.extend(parts[1], days)
            return f"✅ اشتراک {rec.user_id} به‌اندازه‌ی {days:.0f} روز تمدید شد؛ حالا {rec.days_left():.1f} روز باقی مانده."
        if cmd == "/suspenduser" and len(parts) > 1:
            if not _vault.get(parts[1]):
                return f"❌ کاربر {parts[1]} پیدا نشد."
            _vault.suspend(parts[1])
            return f"⏸ کاربر {parts[1]} معلق شد؛ دیگر معامله‌ی جدیدی برایش باز نمی‌شود (معاملات باز فعلی‌اش دست‌نخورده می‌مانند)."
        if cmd == "/resumeuser" and len(parts) > 1:
            if not _vault.get(parts[1]):
                return f"❌ کاربر {parts[1]} پیدا نشد."
            try:
                _vault.resume(parts[1])
                return f"▶️ کاربر {parts[1]} دوباره فعال شد."
            except ValueError as e:
                return f"❌ {e}"
        if cmd == "/removeuser" and len(parts) > 1:
            if parts[-1] != "تایید":
                return (f"⚠️ حذف کاربر {parts[1]} کلید متصل‌شده‌اش را هم برای همیشه پاک می‌کند (غیرقابل بازگشت).\n"
                        f"برای تأیید نهایی دقیقاً همین را بفرستید: /removeuser {parts[1]} تایید")
            if not _vault.get(parts[1]):
                return f"❌ کاربر {parts[1]} پیدا نشد."
            n_open = _user_open_count(parts[1])
            if n_open:
                return (f"❌ کاربر {parts[1]} هنوز {n_open} معامله‌ی باز دارد؛ با حذف کلیدش محافظت آن‌ها قطع می‌شود.\\n"
                        f"اول ببندیدشان: /usercloseall {parts[1]} تایید")
            _vault.remove(parts[1])
            _user_clients.pop(str(parts[1]), None)
            _user_states.pop(str(parts[1]), None)
            return f"🗑 کاربر {parts[1]} و کلیدهایش کامل حذف شد."
        if cmd == "/setuserrisk" and len(parts) >= 6:
            if not _vault.get(parts[1]):
                return f"❌ کاربر {parts[1]} پیدا نشد."
            try:
                rec = _vault.set_risk(parts[1], risk_usdt=parts[2], max_collateral_usdt=parts[3],
                                       leverage=parts[4], max_open_trades=int(parts[5]))
            except (ValueError, InvalidOperation):
                return "❌ فرمت: /setuserrisk <user_id> <risk_usdt> <max_collateral_usdt> <leverage> <max_open_trades>"
            return (f"✅ تنظیمات {rec.user_id}: ریسک {rec.risk_usdt} | سقف وثیقه {rec.max_collateral_usdt} | "
                    f"اهرم {rec.leverage}x | سقف معاملات {rec.max_open_trades}")

        # ---------------- pricing & payment info (admin-only) ----------------
        if cmd == "/setpricetoman" and len(parts) > 2:
            try:
                days = int(parts[1]); toman = int(parts[2])
            except ValueError:
                return "❌ فرمت: /setpricetoman <روز> <مبلغ تومان>"
            _pricing.set_price(days, toman=toman)
            return f"✅ قیمت {days} روز: {toman:,} تومان ثبت شد."
        if cmd == "/setpriceusdt" and len(parts) > 2:
            try:
                days = int(parts[1]); usdt = float(parts[2])
            except ValueError:
                return "❌ فرمت: /setpriceusdt <روز> <مبلغ USDT>"
            _pricing.set_price(days, usdt=usdt)
            return f"✅ قیمت {days} روز: {usdt:g} USDT ثبت شد."
        if cmd == "/removeprice" and len(parts) > 1:
            try:
                days = int(parts[1])
            except ValueError:
                return "❌ فرمت: /removeprice <روز>"
            _pricing.remove_price(days)
            return f"✅ تعرفه‌ی {days} روز حذف شد."
        if cmd == "/listprices":
            tiers = _pricing.all()
            if not tiers:
                return "💳 هنوز هیچ تعرفه‌ای ثبت نشده. با /setpricetoman یا /setpriceusdt اضافه کنید."
            lines = ["💳 تعرفه‌های فعلی:"]
            for days, p in tiers.items():
                parts_txt = []
                if p.get("toman") is not None:
                    parts_txt.append(f"{p['toman']:,} تومان")
                if p.get("usdt") is not None:
                    parts_txt.append(f"{p['usdt']:g} USDT")
                lines.append(f"  {days} روز: " + (" یا ".join(parts_txt) if parts_txt else "—"))
            return "\n".join(lines)
        if cmd == "/setcard" and len(parts) > 2:
            _payment_info.set_card(parts[1], " ".join(parts[2:]))
            return f"✅ اطلاعات کارت ثبت شد: {parts[1]} به‌نام {' '.join(parts[2:])}"
        if cmd == "/setusdt" and len(parts) > 1:
            network = parts[2] if len(parts) > 2 else "TRC20"
            _payment_info.set_usdt(parts[1], network)
            return f"✅ آدرس کیف‌پول USDT ثبت شد ({network}): {parts[1]}"

        # ---------------- payment review (admin-only) ----------------
        if cmd == "/pendingpayments":
            pend = _ledger.pending()
            if not pend:
                return "💳 پرداخت در انتظار تأیید نیست."
            lines = [f"💳 {len(pend)} پرداخت در انتظار تأیید:"]
            for p in pend:
                d = time.strftime("%Y-%m-%d %H:%M", time.localtime(p.requested_at))
                lines.append(f"  {p.payment_id} — کاربر {p.user_id} — {p.days} روز — {p.amount} {p.currency} — کد/رهگیری: {p.note or '—'} — [{d}]")
            return "\n".join(lines)
        if cmd == "/confirmpayment" and len(parts) > 1:
            try:
                p = _ledger.confirm(parts[1])
            except (KeyError, ValueError) as e:
                return f"❌ {e}"
            rec = _vault.get(p.user_id)
            if not rec:
                return f"⚠️ پرداخت {p.payment_id} تأیید شد ولی کاربر {p.user_id} دیگر در vault نیست."
            rec = _vault.extend(p.user_id, p.days)
            if rec.status == us.STATUS_PENDING:
                user_msg = (f"✅ پرداخت {p.payment_id} تأیید شد؛ {p.days} روز به اشتراک شما اضافه شد "
                            f"(مجموع {rec.pending_days:.0f} روز؛ از لحظه‌ی اتصال کلید API شروع می‌شود). ادامه: /start")
            else:
                user_msg = (f"✅ پرداخت {p.payment_id} تأیید شد؛ اشتراک شما {p.days} روز تمدید شد "
                            f"({rec.days_left():.1f} روز باقی مانده).")
            notify_admin(user_msg, chat_id=rec.notify_chat_id or p.user_id)
            return f"✅ پرداخت {p.payment_id} تأیید و اشتراک {p.user_id} به‌روزرسانی شد."
        if cmd == "/rejectpayment" and len(parts) > 1:
            reason = " ".join(parts[2:])
            try:
                p = _ledger.reject(parts[1], reason=reason)
            except (KeyError, ValueError) as e:
                return f"❌ {e}"
            rec = _vault.get(p.user_id)
            notify_admin(f"❌ پرداخت شما ({p.payment_id}) تأیید نشد." + (f" دلیل: {reason}" if reason else "") +
                         "\nاگر فکر می‌کنید اشتباهی رخ داده، با ادمین در تماس باشید.",
                         chat_id=(rec.notify_chat_id if rec else None) or p.user_id)
            return f"❌ پرداخت {p.payment_id} رد شد."

        if cmd == "/autoexit":
            if len(parts) < 2 or parts[1].lower() not in ("on", "off"):
                cur = "روشن ✅" if _auto_exit_enabled() else "خاموش ⏸"
                return (f"🤖 اجرای خودکار رویدادهای کانال (بریک‌ایون، SL بعد از تارگت، حد ضرر نهایی، بسته‌شدن Runner، بستن اجباری): {cur}\n"
                        f"تغییر: /autoexit on  یا  /autoexit off (خاموش = مثل قبل قبل از بستن از شما تأیید می‌گیرد)")
            c = load_control(gh); c["auto_exit"] = parts[1].lower() == "on"; save_control(gh, c)
            _admin_auto_exit_cache.update(ts=time.time(), value=c["auto_exit"])
            return "🤖 اجرای خودکار رویدادهای کانال " + ("روشن شد ✅" if c["auto_exit"] else "خاموش شد ⏸ (پیش از هر بستن تأیید می‌گیرم)")
        if cmd == "/userautoexit" and len(parts) > 2 and parts[2].lower() in ("on", "off"):
            if not _vault.get(parts[1]):
                return f"❌ کاربر {parts[1]} پیدا نشد."
            _vault.set_auto_exit(parts[1], parts[2].lower() == "on")
            return f"🤖 اجرای خودکار خروج طبق کانال برای {parts[1]}: {parts[2].lower()}"
        if cmd == "/usertrades" and len(parts) > 1:
            rec = _vault.get(parts[1])
            if not rec:
                return f"❌ کاربر {parts[1]} پیدا نشد."
            if not rec.public_key_enc:
                return f"ℹ️ کاربر {parts[1]} هنوز کلید متصل نکرده."
            text = _run_as_user_silent(rec, "/positions")
            btns = [[{"text": f"❌ بستن {k} ({parts[1]})", "callback_data": f"cmd:/userclose {parts[1]} {k}"}]
                    for k in sorted((_user_states.get(str(parts[1])) or {}).get("open_trades", {}).keys())]
            return (text, btns or None) if btns else text
        if cmd == "/userhistory" and len(parts) > 1:
            rec = _vault.get(parts[1])
            if not rec or not rec.public_key_enc:
                return f"❌ کاربر {parts[1]} پیدا نشد یا کلید متصل ندارد."
            return _run_as_user_silent(rec, "/history " + (parts[2] if len(parts) > 2 else "10"))
        if cmd == "/userpnl" and len(parts) > 1:
            rec = _vault.get(parts[1])
            if not rec or not rec.public_key_enc:
                return f"❌ کاربر {parts[1]} پیدا نشد یا کلید متصل ندارد."
            return _run_as_user_silent(rec, "/pnl")
        if cmd == "/userclose" and len(parts) > 2:
            rec = _vault.get(parts[1])
            if not rec or not rec.public_key_enc:
                return f"❌ کاربر {parts[1]} پیدا نشد یا کلید متصل ندارد."
            res = _run_as_user(rec, gh, f"/close {parts[2].upper()}")
            return f"[کاربر {parts[1]}] {res}"
        if cmd == "/usercloseall" and len(parts) > 1:
            rec = _vault.get(parts[1])
            if not rec or not rec.public_key_enc:
                return f"❌ کاربر {parts[1]} پیدا نشد یا کلید متصل ندارد."
            if parts[-1] != "تایید":
                return f"⚠️ همه‌ی معاملات باز کاربر {parts[1]} با قیمت بازار بسته می‌شود. تأیید: /usercloseall {parts[1]} تایید"
            res = _run_as_user(rec, gh, "/closeall")
            return f"[کاربر {parts[1]}] {res}"
        if cmd == "/userdisconnect" and len(parts) > 1:
            rec = _vault.get(parts[1])
            if not rec:
                return f"❌ کاربر {parts[1]} پیدا نشد."
            if _user_open_count(parts[1]):
                return "❌ معامله‌ی باز دارد؛ اول ببندید: /usercloseall " + parts[1] + " تایید"
            _vault.disconnect(parts[1]); _user_clients.pop(str(parts[1]), None); _user_states.pop(str(parts[1]), None)
            return f"🔌 کلید کاربر {parts[1]} حذف شد (روزهای باقیمانده حفظ شد)."
        if cmd == "/userforcepause" and len(parts) > 1:
            if not _vault.get(parts[1]):
                return f"❌ کاربر {parts[1]} پیدا نشد."
            _vault.set_paused(parts[1], True)
            return f"⏸ ورود جدید کاربر {parts[1]} متوقف شد (/userresume برای ازسرگیری)."
        if cmd == "/userresume" and len(parts) > 1:
            if not _vault.get(parts[1]):
                return f"❌ کاربر {parts[1]} پیدا نشد."
            _vault.set_paused(parts[1], False)
            return f"▶️ ورود جدید کاربر {parts[1]} ازسرگرفته شد."
        if cmd == "/msguser" and len(parts) > 2:
            if not _vault.get(parts[1]):
                return f"❌ کاربر {parts[1]} پیدا نشد."
            notify_admin("📢 پیام ادمین:\\n" + " ".join(parts[2:]), chat_id=parts[1])
            return f"✅ پیام برای {parts[1]} ارسال شد."
        if cmd == "/broadcast" and len(parts) > 1:
            targets = [r for r in _vault.all() if r.public_key_enc or r.status == us.STATUS_PENDING]
            for r in targets:
                notify_admin("📢 پیام ادمین:\\n" + " ".join(parts[1:]), chat_id=r.notify_chat_id or r.user_id)
            return f"✅ پیام برای {len(targets)} کاربر ارسال شد."
        return "❓ دستور ناشناخته. /menu را بزنید."
    except Exception as e:
        log.exception("bot command failed")
        return f"🚨 اجرای دستور ناموفق بود: {type(e).__name__}: {e}"

def main() -> int:
    global _gh_outbox, _vault, _pricing, _ledger, _payment_info, _gh_main
    acquire_single_instance_lock()
    os.makedirs(USER_STATES_DIR, exist_ok=True)

    _gh_outbox = GithubClient(GITHUB_REPO, GITHUB_PAT, GITHUB_BRANCH)
    _vault = us.UserVault()
    _pricing = us.PricingConfig()
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
    while True:
        try:
            flush_pending_outbox()
            poll_commands(nx, gh, state)
            poll_once(nx, gh, state)
            poll_commands(nx, gh, state)
            resolve_pending_protection(nx, state)
            poll_commands(nx, gh, state)
            resolve_needs_protection(nx, state)
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
            run_all_user_cycles(gh)
            poll_commands(nx, gh, state)
            write_heartbeat(state)
            consecutive_errors = 0
        except Exception as e:
            consecutive_errors += 1
            log.exception("main loop error #%s", consecutive_errors)
            if consecutive_errors in (1, 5) or consecutive_errors % 20 == 0:
                notify_admin(f"🚨 Main loop error #{consecutive_errors}: {e}")
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    sys.exit(main())
