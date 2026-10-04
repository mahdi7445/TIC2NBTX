# -*- coding: utf-8 -*-
"""Telegram <-> GitHub relay for the TIC2NBTX admin/control plane."""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import subprocess
import time
from pathlib import Path

import requests
from cryptography.fernet import Fernet

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("telegram-github-bridge")

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHANNEL_ID = str(os.environ["TELEGRAM_CHANNEL_ID"])
ADMIN_CHAT_ID = str(os.environ["TELEGRAM_ADMIN_CHAT_ID"])
# Same master key user_store.py uses on the Windows executor. Must be set as
# a GitHub Actions secret for this workflow (never committed) - it is what
# lets a subscriber's raw API key/secret be encrypted here, BEFORE the
# /connect command is ever written to commands.jsonl, so the plaintext key
# never touches this public repo even for one commit. If this secret is
# missing, /connect is refused outright rather than ever relaying plaintext.
USER_VAULT_MASTER_KEY = os.environ.get("USER_VAULT_MASTER_KEY", "")
# The only commands a non-admin (multi-tenant subscriber) chat may ever
# trigger. Everything else from a non-admin chat_id is silently ignored,
# exactly like today's behavior for any stranger. This mirrors (but does not
# replace - see caller_chat_id in executor.py) the SELF_SERVICE_COMMANDS
# allow-list enforced independently on the executor side.
SELF_SERVICE_COMMANDS = {
    "/start", "/help", "/menu", "/terms", "/accept", "/decline", "/plans", "/prices", "/subscribe", "/cancelpay", "/paid",
    "/usediscount", "/cleardiscount",
    "/connect_enc", "/connectguide", "/support", "/mysubscription", "/mystatus",
    "/mytrades", "/myhistory", "/mypnl", "/mybalance", "/myclose", "/mycloseall", "/mypause", "/myresume",
    "/mysettings", "/myvalue", "/myset", "/myrisk", "/reqcap", "/myautoexit", "/mydailyreport",
    "/myliqsafe", "/myliqadd", "/mycolall", "/disconnect", "/myconfirmclose", "/mydismissclose",
}
API = f"https://api.telegram.org/bot{TOKEN}"

SIGNALS = Path("signals.jsonl")
COMMANDS = Path("commands.jsonl")
OUTBOX = Path("outbox.jsonl")
BRIDGE_STATE = Path("bridge_state.json")
HEARTBEAT_FILE = Path("heartbeat.json")
# How stale the Windows executor's heartbeat can be before bridge.py assumes
# it's down and alerts directly (well above HEARTBEAT_INTERVAL_SECONDS=60 in
# executor.py, so normal write jitter/relay delay never false-alarms).
HEARTBEAT_STALE_SECONDS = int(os.environ.get("HEARTBEAT_STALE_SECONDS", "360"))
MAX_LINES = int(os.environ.get("MAX_RELAY_LINES", "5000"))
RUNTIME_SECONDS = int(os.environ.get("BRIDGE_RUNTIME_SECONDS", "275"))
# Telegram's getUpdates call blocks for up to this many seconds waiting for a
# new update before returning empty - so it, not the sleep() at the bottom of
# the loop, is what actually sets the loop's cadence. The previous 25s value
# meant push/pull only really happened once every ~25s while idle, which
# quietly capped outbox-relay latency at ~25s no matter how tight the push/
# pull intervals below were set. 5s keeps genuine long-polling (still much
# cheaper than a plain poll loop) while letting push/pull run close to the
# cadence they're actually configured for.
LONG_POLL_SECONDS = int(os.environ.get("TELEGRAM_LONG_POLL_SECONDS", "3"))

# Push/pull cadence inside the run loop. Previously both only happened once,
# at the very end of the whole ~275s run - a command received right after the
# loop started could sit unpushed (and therefore invisible to the Windows
# executor) for most of that window, and any outbox message the executor
# pushed to GitHub mid-run was only picked up by the *next* bridge.py run's
# fresh checkout. That combination is what made admin commands take minutes
# to answer. Pushing/pulling every few seconds instead keeps both directions
# close to real time without hammering the GitHub API.
PUSH_MIN_INTERVAL_SECONDS = float(os.environ.get("PUSH_MIN_INTERVAL_SECONDS", "0.7"))
PULL_MIN_INTERVAL_SECONDS = float(os.environ.get("PULL_MIN_INTERVAL_SECONDS", "2"))

# Self-dispatch: queue the next run of this same workflow immediately instead
# of relying solely on GitHub's "*/5 * * * *" cron, which this repository's
# own Actions history shows firing anywhere from ~5 minutes to several hours
# apart under load - not the steady 5-minute cadence the architecture assumes.
# The cron trigger in bridge.yml is left in place as an external safety net in
# case this chain is ever broken (e.g. the token is briefly invalid).
GH_DISPATCH_TOKEN = os.environ.get("GH_DISPATCH_TOKEN", "")
GITHUB_REPOSITORY = os.environ.get("GITHUB_REPOSITORY", "")
GITHUB_REF_NAME = os.environ.get("GITHUB_REF_NAME", "main")
BRIDGE_WORKFLOW_FILE = os.environ.get("BRIDGE_WORKFLOW_FILE", "bridge.yml")

# Slash commands are only a convenience now - everything is reachable with buttons.
USER_BOT_COMMANDS = [
    {"command": "start", "description": "شروع / بازگشت به منوی من"},
    {"command": "menu", "description": "منوی من"},
    {"command": "help", "description": "راهنما"},
]
BOT_COMMANDS = [
    {"command": "start", "description": "منوی مدیریت"},
    {"command": "menu", "description": "منوی مدیریت"},
]


def load_state() -> dict:
    if not BRIDGE_STATE.exists():
        return {"telegram_update_offset": 0, "outbox_lines_sent": 0}
    try:
        return json.loads(BRIDGE_STATE.read_text(encoding="utf-8"))
    except Exception:
        return {"telegram_update_offset": 0, "outbox_lines_sent": 0}


def save_state(state: dict) -> None:
    tmp = BRIDGE_STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(BRIDGE_STATE)


def append_lines(path: Path, new_lines: list[str]) -> None:
    if not new_lines:
        return
    old = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    combined = (old + [x.rstrip("\n") for x in new_lines])[-MAX_LINES:]
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("\n".join(combined) + "\n", encoding="utf-8")
    tmp.replace(path)


def ensure_runtime_files() -> None:
    for path in (SIGNALS, COMMANDS, OUTBOX):
        if not path.exists():
            path.write_text("", encoding="utf-8")


def telegram_call(method: str, payload: dict | None = None) -> dict:
    r = requests.post(API + "/" + method, json=payload or {}, timeout=20)
    r.raise_for_status()
    body = r.json()
    if not body.get("ok"):
        raise RuntimeError(f"Telegram API error: {body}")
    return body


def send_message(text: str, inline_keyboard=None, reply_keyboard=None, reply_to_message_id=None, chat_id=None,
                 remove_keyboard: bool = False, silent: bool = False):
    payload = {"chat_id": chat_id or ADMIN_CHAT_ID, "text": str(text)[:4000], "disable_web_page_preview": True}
    if silent:
        payload["disable_notification"] = True
    if inline_keyboard is not None:
        payload["reply_markup"] = {"inline_keyboard": inline_keyboard}
    elif reply_keyboard is not None:
        payload["reply_markup"] = {"keyboard": reply_keyboard, "resize_keyboard": True, "is_persistent": True}
    elif remove_keyboard:
        payload["reply_markup"] = {"remove_keyboard": True}
    if reply_to_message_id:
        payload["reply_to_message_id"] = reply_to_message_id
        # Never let a missing/too-old root message silently swallow this one -
        # Telegram just sends it as a normal message instead of erroring out.
        payload["allow_sending_without_reply"] = True
    body = telegram_call("sendMessage", payload)
    try:
        return body.get("result", {}).get("message_id")
    except Exception:
        return None


def delete_message(chat_id: str, message_id) -> None:
    """Best-effort: remove a subscriber's message that contained their raw API key/secret from
    their own chat right after it was encrypted. If Telegram refuses, nothing else is affected
    (the key was already encrypted and never logged)."""
    try:
        telegram_call("deleteMessage", {"chat_id": chat_id, "message_id": message_id})
    except Exception:
        log.warning("could not delete a sensitive message (chat %s)", chat_id)


def copy_message(to_chat_id: str, from_chat_id: str, message_id) -> None:
    try:
        telegram_call("copyMessage", {"chat_id": to_chat_id, "from_chat_id": from_chat_id, "message_id": message_id})
    except Exception:
        log.warning("could not copy message %s to admin", message_id)


def answer_callback(callback_id: str, text: str = "") -> None:
    try:
        payload = {"callback_query_id": callback_id}
        if text:
            payload["text"] = text[:190]
        telegram_call("answerCallbackQuery", payload)
    except Exception:
        pass


def setup_bot_commands() -> None:
    try:
        telegram_call("setMyCommands", {"commands": USER_BOT_COMMANDS})
        telegram_call("setMyCommands", {"commands": BOT_COMMANDS,
                                        "scope": {"type": "chat", "chat_id": int(ADMIN_CHAT_ID)}})
    except Exception:
        log.exception("Could not configure Telegram commands")


# --------------------------------------------------------------------------- keyboards
def user_reply_keyboard():
    return [["📋 منوی من", "📈 معاملات باز"], ["📜 تاریخچه", "📊 سود و زیان"], ["⚙️ تنظیمات", "🆘 پشتیبانی"]]


USER_KB_TEXT = {"📋 منوی من": "/menu", "📈 معاملات باز": "/mytrades", "📜 تاریخچه": "/myhistory",
                "📊 سود و زیان": "/mypnl", "⚙️ تنظیمات": "/mysettings"}


def admin_reply_keyboard():
    return [["📋 منو", "📈 معاملات باز", "📊 وضعیت سیستم"], ["📜 تاریخچه", "📊 سود و زیان", "👥 کاربران"]]


ADMIN_KB_TEXT = {"📈 معاملات باز": "/positions", "📊 وضعیت سیستم": "/status", "📜 تاریخچه": "/history",
                 "📊 سود و زیان": "/pnl", "👥 کاربران": "/listusers"}


def B(text: str, data: str) -> dict:
    return {"text": text, "callback_data": data}


def inline_main_menu():
    return [
        [B("📊 وضعیت سیستم", "cmd:/status"), B("💰 موجودی", "cmd:/balance")],
        [B("📈 معاملات باز", "cmd:/positions"), B("📜 تاریخچه", "cmd:/history"), B("📊 سود و زیان", "cmd:/pnl")],
        [B("▶️ فعال‌سازی ورود", "cmd:/resume"), B("⏸ توقف ورود", "cmd:/pause")],
        [B("🎯 ریسک", "risk_menu"), B("💵 سقف وثیقه", "collateral_menu")],
        [B("🔢 حداکثر معاملات", "maxtrades_menu"), B("📈 اهرم", "leverage_menu")],
        [B("💵 مبلغ تعهد همه‌ی معاملات", "cmd:/colall"), B("❌ بستن همه‌ی معاملات", "cmd:/closeall")],
        [B("🛡 بررسی محافظت", "cmd:/protection"), B("🔄 همگام‌سازی", "cmd:/reconcile"), B("♻️ بازیابی", "cmd:/recover")],
        [B("🤖 خروج خودکار", "cmd:/autoexit"), B("🧪 تست API", "cmd:/test_api"), B("🧾 لاگ‌ها", "cmd:/logs")],
        [B("👥 کاربران و اشتراک", "users_menu"), B("💳 پرداخت‌های در انتظار", "cmd:/pendingpayments")],
    ]


ADMIN_MENU_TEXT = ("🤖 پنل مدیریت TRADE IS COOL\n━━━━━━━━━━━━━━\n"
                   "همه‌ی کارها با دکمه‌هاست؛ هر جا عدد لازم باشد، ربات خودش می‌پرسد.\n"
                   "زیر هر پیام ربات هم دکمه‌های همان بخش هست.")


def send_menu(first: bool = False) -> None:
    send_message(ADMIN_MENU_TEXT, inline_keyboard=inline_main_menu())


ADMIN_PRESETS = {
    "risk_menu": ("🎯 ریسک هر معامله (USDT)", "/risk", ["0.25", "0.5", "1", "2", "5", "10"], "arisk"),
    "collateral_menu": ("💵 سقف وثیقه‌ی هر معامله (USDT)", "/collateral", ["1", "1.25", "1.5", "2", "5", "10", "25"], "acol"),
    "maxtrades_menu": ("🔢 حداکثر معاملات همزمان", "/maxtrades", ["3", "5", "10", "15", "20"], "amax"),
    "leverage_menu": ("📈 اهرم", "/leverage", ["3", "5", "10", "15", "20"], "alev"),
}


def preset_menu(key: str):
    title, cmd, vals, ask = ADMIN_PRESETS[key]
    btns = [B(v + ("x" if cmd == "/leverage" else ""), f"cmd:{cmd} {v}") for v in vals]
    rows = [btns[i:i + 3] for i in range(0, len(btns), 3)]
    rows.append([B("✏️ عدد دلخواه", f"ask:{ask}")])
    rows.append([B("📋 منو", "menu")])
    return f"{title}\n━━━━━━━━━━━━━━\nیکی را بزنید یا عدد دلخواه را وارد کنید:", rows


def users_menu():
    return ("👥 مدیریت کاربران و اشتراک\n━━━━━━━━━━━━━━\nهمه‌چیز از همین‌جا با دکمه انجام می‌شود.",
            [[B("👥 لیست کاربران", "cmd:/listusers"), B("➕ افزودن کاربر", "ask:adduser")],
             [B("💳 پرداخت‌های در انتظار", "cmd:/pendingpayments"), B("💰 تعرفه‌ها و کارت", "cmd:/listprices")],
             [B("🎟 کدهای تخفیف", "cmd:/discounts"), B("📢 پیام همگانی", "ask:broadcast")],
             [B("📋 منو", "menu")]])


# --------------------------------------------------------------------------- command relay
# --------------------------------------------------------------------------- "working on it" indicator
# Telegram's own button toast ("sent...") disappears after a few seconds, long before a slow answer
# arrives, so people think the bot is dead. While a request is waiting for its answer we therefore keep
# TWO signals alive in that chat: the "typing..." status under the bot's name (re-sent every ~4 s, because
# Telegram clears it after 5 s) and one small "working on it" message that is deleted the moment the
# answer is delivered (relay_outbox) - or turned into a short note if the answer takes too long.
PENDING_TTL_SECONDS = int(os.environ.get("PENDING_TTL_SECONDS", "90"))
PENDING_TEXT = "⏳ در حال انجام… لطفاً چند لحظه صبر کنید"
PENDING_LATE_TEXT = "⌛ پاسخ دیرتر از معمول است. اگر چیزی نیامد، یک بار دیگر دکمه را بزنید."
_typing_ts: dict = {}          # chat_id -> last time we sent the typing status (not persisted: avoids git churn)


def send_chat_action(chat_id: str) -> None:
    try:
        telegram_call("sendChatAction", {"chat_id": chat_id, "action": "typing"})
    except Exception:
        pass


def mark_pending(chat_id: str) -> None:
    """A command was just relayed for this chat: show that work is in progress until the reply arrives."""
    st = STATE_REF.get("state")
    if st is None:
        return
    pend = st.setdefault("pending", {})
    now = time.time()
    chat_id = str(chat_id)
    rec = pend.get(chat_id)
    if rec and now - float(rec.get("ts", 0)) < PENDING_TTL_SECONDS:
        rec["ts"] = now                    # a second tap while waiting: keep the one existing indicator
    else:
        mid = None
        try:
            mid = send_message(PENDING_TEXT, chat_id=chat_id, silent=True)
        except Exception:
            log.warning("could not send the working-on-it message to chat %s", chat_id)
        pend[chat_id] = {"ts": now, "mid": mid}
    send_chat_action(chat_id)
    _typing_ts[chat_id] = now


def clear_pending(chat_id) -> None:
    """The answer for this chat was delivered: remove the indicator."""
    st = STATE_REF.get("state")
    if st is None:
        return
    rec = (st.get("pending") or {}).pop(str(chat_id), None)
    _typing_ts.pop(str(chat_id), None)
    if rec and rec.get("mid"):
        delete_message(str(chat_id), rec["mid"])


def tick_pending(state: dict) -> bool:
    """Called every loop: keep 'typing...' alive and expire indicators whose answer never came."""
    pend = state.get("pending") or {}
    if not pend:
        return False
    now = time.time()
    changed = False
    for chat_id, rec in list(pend.items()):
        if now - float(rec.get("ts", 0)) > PENDING_TTL_SECONDS:
            pend.pop(chat_id, None)
            _typing_ts.pop(chat_id, None)
            changed = True
            if rec.get("mid"):
                try:
                    telegram_call("editMessageText", {"chat_id": chat_id, "message_id": rec["mid"], "text": PENDING_LATE_TEXT})
                except Exception:
                    delete_message(chat_id, rec["mid"])
            continue
        if now - _typing_ts.get(chat_id, 0) >= 4.0:
            send_chat_action(chat_id)
            _typing_ts[chat_id] = now
    return changed


# Heuristic only (the executor re-validates everything with its own strict parser, fwd_signal.py): is this admin
# message probably a trading signal - a stop-loss word plus an entry/target word plus a number?
_SIG_STOP = re.compile(r"stop\s*[-_]?\s*loss|\bstoploss\b|\bstop\b|\bsl\b|حد\s*ضرر|حدضرر|استاپ", re.IGNORECASE)
_SIG_ENTRY_TP = re.compile(r"\bentry\b|\benter\b|\btarget|\btp\s*\d?|\btp\b|\bt\s*\d\b|take\s*[-_]?\s*profit|\blimit\b|\bmarket\b|\bcmp\b|ورود|تارگت|هدف|حد\s*سود|بازار|لیمیت", re.IGNORECASE)


def looks_like_signal(text: str) -> bool:
    t = (text or "").translate(_FA_DIGITS)
    return bool(re.search(r"\d", t)) and bool(_SIG_STOP.search(t)) and bool(_SIG_ENTRY_TP.search(t))


# Why some buttons seemed to act twice: the answer takes a few seconds, so people tap again, and every tap used
# to become its own command. Two protections: (1) the exact same command from the same chat within a few seconds
# is dropped (a deliberate repeat after the answer arrived still works), (2) the same Telegram update is never
# turned into two commands even if a run is restarted.
DEBOUNCE_SECONDS = float(os.environ.get("COMMAND_DEBOUNCE_SECONDS", "4"))
_recent_cmds: dict = {}


def is_duplicate_tap(chat_id, command: str) -> bool:
    now = time.time()
    for k in [k for k, ts in _recent_cmds.items() if now - ts > 60]:
        _recent_cmds.pop(k, None)
    k = (str(chat_id), " ".join(str(command).split()))
    last = _recent_cmds.get(k)
    _recent_cmds[k] = now
    return last is not None and now - last < DEBOUNCE_SECONDS


def _command_already_queued(command_id: str) -> bool:
    try:
        if not COMMANDS.exists():
            return False
        needle = f'"command_id": "{command_id}"'
        with COMMANDS.open("r", encoding="utf-8") as f:
            return any(needle in line for line in f)
    except Exception:
        return False


def append_command(update_id: int, command: str, chat_id: str | None = None, meta: dict | None = None,
                   suffix: str = "") -> bool:
    """Queue one command for the executor. Returns False when it was dropped as a duplicate."""
    if is_duplicate_tap(chat_id if chat_id is not None else ADMIN_CHAT_ID, command):
        log.info("dropped duplicate tap: %s", command[:60])
        return False
    if _command_already_queued(f"tg-{update_id}{suffix}"):
        log.info("dropped already-queued update %s", update_id)
        return False
    payload = {
        "command_id": f"tg-{update_id}{suffix}",
        "update_id": update_id,
        "ts": time.time(),
        "command": command,
    }
    if chat_id is not None:
        # Presence of this field is what executor.py treats as "non-admin caller" (see caller_chat_id
        # there) - never set it for an admin command, and never omit it for a self-service one.
        payload["chat_id"] = str(chat_id)
        if meta:
            payload["meta"] = meta
    append_lines(COMMANDS, [json.dumps(payload, ensure_ascii=False)])
    return True


def encrypt_for_vault(plaintext: str) -> str:
    if not USER_VAULT_MASTER_KEY:
        raise RuntimeError("USER_VAULT_MASTER_KEY تنظیم نشده")
    return Fernet(USER_VAULT_MASTER_KEY.encode()).encrypt(plaintext.strip().encode()).decode()


_user_hits: dict = {}
USER_RATE_WINDOW_SECONDS = 600
USER_RATE_MAX = 40


def user_rate_limited(chat_id: str) -> bool:
    """Any stranger can message a public bot. Every accepted message becomes a line in a public repo
    and triggers a reply, so one chat is limited to USER_RATE_MAX messages per window; the rest are
    silently dropped. The admin chat is never limited."""
    now = time.time()
    hits = [t for t in _user_hits.get(chat_id, []) if now - t < USER_RATE_WINDOW_SECONDS]
    if len(hits) >= USER_RATE_MAX:
        _user_hits[chat_id] = hits
        return True
    hits.append(now)
    _user_hits[chat_id] = hits
    return False


# --------------------------------------------------------------------------- wizard (button-driven prompts)
WIZARD_TTL_SECONDS = 20 * 60
_FA_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩٫٬،", "01234567890123456789.,,")
_wizard: dict = {}          # chat_id -> {"prompt": str, "arg": str, "step": int, "data": {...}, "ts": float}


def _norm_number(text: str):
    t = text.strip().translate(_FA_DIGITS).replace(",", "").replace(" ", "")
    if not t or len(t) > 20:
        return None
    try:
        v = float(t)
    except ValueError:
        return None
    if v != v or v in (float("inf"), float("-inf")):
        return None
    return t


def _q(text: str) -> str:
    return text


# Each prompt: list of steps; a step = (question, kind). kinds: num | int | text | secret | days
# `build(is_admin, arg, answers)` -> command string (or None when handled specially).
PROMPTS = {
    # ---- subscriber ----
    "apikey": {"admin": False, "steps": [
        ("🔑 مرحله ۱ از ۲ — کلید عمومی (API Key)\n\nکلید عمومی را از نوبیتکس کپی کنید و همین‌جا بفرستید.\n"
         "🔒 پیام شما بلافاصله رمزنگاری و از چت پاک می‌شود.", "secret"),
        ("✅ کلید عمومی دریافت شد.\n\n🔑 مرحله ۲ از ۲ — کلید خصوصی (Secret Key)\n\nحالا کلید خصوصی را بفرستید.\n"
         "🔒 این پیام هم بلافاصله رمزنگاری و پاک می‌شود.", "secret")]},
    "receipt": {"admin": False, "steps": [
        ("🧾 کد رهگیری (یا هش تراکنش) پرداخت را بفرستید.\nاگر رسید را به‌صورت عکس دارید، همان عکس را هم می‌توانید بفرستید.", "text")]},
    "support": {"admin": False, "steps": [("🆘 پیام‌تان را برای پشتیبانی بنویسید:", "text")]},
    "discount": {"admin": False, "steps": [("🎟 کد تخفیف را بفرستید (مثلاً EID20):", "text")]},
    "setrisk": {"admin": False, "steps": [("🎯 ریسک هر معامله چند USDT باشد؟ (فقط عدد، مثلاً 1)", "num")]},
    "setcol": {"admin": False, "steps": [("💵 سقف وثیقه‌ی هر معامله چند USDT باشد؟ (فقط عدد)", "num")]},
    "setlev": {"admin": False, "steps": [("📈 اهرم چند برابر باشد؟ (فقط عدد، مثلاً 5)", "num")]},
    "setmax": {"admin": False, "steps": [("🔢 حداکثر چند معامله‌ی همزمان؟ (عدد صحیح)", "int")]},
    "setdaily": {"admin": False, "steps": [("🛑 سقف ضرر روزانه چند USDT؟ (0 = خاموش)", "num")]},
    "reqcap": {"admin": False, "steps": [("📨 مقدار موردنظرتان را بفرستید (فقط عدد). درخواست برای ادمین ارسال می‌شود:", "num")]},
    # ---- shared (subscriber or admin) ----
    "colallpct": {"both": True, "steps": [("✏️ وثیقه‌ی همه‌ی معاملات چند درصد بیشتر شود؟ (عدد بین 1 تا 100)", "num")]},
    "colallto": {"both": True, "steps": [("🎯 وثیقه‌ی هر معامله برابر چند USDT شود؟ (فقط عدد؛ فقط افزایش انجام می‌شود)", "num")]},
    # ---- admin ----
    "adduser": {"admin": True, "steps": [("➕ شناسه‌ی عددی تلگرام کاربر را بفرستید:", "int"),
                                         ("📅 چند روز اشتراک هدیه بدهم؟ (فقط عدد)", "num")]},
    "extend": {"admin": True, "steps": [("📅 چند روز تمدید شود؟ (فقط عدد)", "num")]},
    "userrisk": {"admin": True, "steps": [("🎯 سقف ریسک هر معامله (USDT):", "num"), ("💵 سقف وثیقه‌ی هر معامله (USDT):", "num"),
                                          ("📈 سقف اهرم:", "num"), ("🔢 سقف معاملات همزمان:", "int")]},
    "msguser": {"admin": True, "steps": [("✉️ متن پیام برای این کاربر:", "text")]},
    "reply": {"admin": True, "steps": [("✍️ متن پاسخ به کاربر:", "text")]},
    "broadcast": {"admin": True, "steps": [("📢 متن پیام همگانی (برای همه‌ی کاربران غیرمسدود):", "text")]},
    "rejectreason": {"admin": True, "steps": [("✍️ دلیل رد پرداخت را بنویسید (به کاربر گفته می‌شود):", "text")]},
    "newdiscount": {"admin": True, "steps": [
        ("🎟 نام کد تخفیف را بفرستید (فقط حروف انگلیسی/عدد، مثلاً EID20):", "text"),
        ("٪ چند درصد تخفیف؟ (عدد بین 1 تا 99)", "num"),
        ("📅 روی کدام اشتراک‌ها اعمال شود؟\nمدت‌ها را با کاما بفرستید، مثلاً 30,90 — یا بنویسید all برای همه:", "text"),
        ("🔢 حداکثر چند نفر بتوانند استفاده کنند؟ (0 = نامحدود)", "int"),
        ("⏳ چند روز اعتبار داشته باشد؟ (0 = بدون انقضا)", "num")]},
    "pricetoman": {"admin": True, "steps": [("📅 مدت (روز):", "int"), ("💳 قیمت به تومان (فقط عدد):", "num")]},
    "priceusdt": {"admin": True, "steps": [("📅 مدت (روز):", "int"), ("💵 قیمت به USDT (فقط عدد):", "num")]},
    "setcard": {"admin": True, "steps": [("💳 شماره‌ی کارت را بفرستید:", "text"), ("👤 نام صاحب کارت:", "text")]},
    "setusdt": {"admin": True, "steps": [("💵 آدرس کیف پول USDT:", "text"), ("🌐 نام شبکه (مثلاً TRC20):", "text")]},
    "arisk": {"admin": True, "steps": [("🎯 ریسک هر معامله (USDT)؟", "num")]},
    "acol": {"admin": True, "steps": [("💵 سقف وثیقه‌ی هر معامله (USDT)؟", "num")]},
    "amax": {"admin": True, "steps": [("🔢 حداکثر معاملات همزمان؟", "int")]},
    "alev": {"admin": True, "steps": [("📈 اهرم؟", "num")]},
    "ucolallpct": {"admin": True, "steps": [("✏️ وثیقه‌ی همه‌ی معاملات این کاربر چند درصد بیشتر شود؟ (1 تا 100)", "num")]},
    "ucolallto": {"admin": True, "steps": [("🎯 وثیقه‌ی هر معامله برابر چند USDT شود؟", "num")]},
}
_USER_FIELD_OF_ASK = {"setrisk": "risk", "setcol": "col", "setlev": "lev", "setmax": "max", "setdaily": "daily"}


def _build_command(name: str, arg: str, answers: list, is_admin: bool):
    a = answers
    if name == "receipt":
        return f"/paid {arg} {a[0]}"
    if name == "support":
        return f"/support {a[0]}"
    if name in _USER_FIELD_OF_ASK:
        return f"/myset {_USER_FIELD_OF_ASK[name]} {a[0]}"
    if name == "reqcap":
        return f"/reqcap {arg} {a[0]}"
    if name in ("colallpct", "colallto"):
        mode = "pct" if name == "colallpct" else "to"
        return f"/colall {mode} {a[0]}" if is_admin else f"/mycolall {mode} {a[0]}"
    if name == "adduser":
        return f"/adduser {int(float(a[0]))} {a[1]}"
    if name == "extend":
        return f"/extenduser {arg} {a[0]}"
    if name == "userrisk":
        return f"/setuserrisk {arg} {a[0]} {a[1]} {a[2]} {int(float(a[3]))}"
    if name in ("msguser", "reply"):
        return f"/msguser {arg} {a[0]}"
    if name == "discount":
        code = "".join(str(a[0]).split())
        return f"/usediscount {code}" if code else None
    if name == "newdiscount":
        code = "".join(str(a[0]).split())
        plans = "".join(str(a[2]).split()) or "all"
        if not code or not all(ch.isdigit() or ch == "," for ch in plans.replace("all", "").replace("همه", "")):
            return None
        return f"/newdiscount {code} {a[1]} {plans} {int(float(a[3]))} {a[4]}"
    if name == "broadcast":
        return f"/broadcast {a[0]}"
    if name == "rejectreason":
        return f"/rejectpayment {arg} {a[0]}"
    if name == "pricetoman":
        return f"/setpricetoman {int(float(a[0]))} {a[1]}"
    if name == "priceusdt":
        return f"/setpriceusdt {int(float(a[0]))} {a[1]}"
    if name == "setcard":
        return f"/setcard {a[0].replace(' ', '')} {a[1]}"
    if name == "setusdt":
        return f"/setusdt {a[0].replace(' ', '')} {a[1].replace(' ', '') or 'TRC20'}"
    if name == "arisk":
        return f"/risk {a[0]}"
    if name == "acol":
        return f"/collateral {a[0]}"
    if name == "amax":
        return f"/maxtrades {int(float(a[0]))}"
    if name == "alev":
        return f"/leverage {a[0]}"
    if name in ("ucolallpct", "ucolallto"):
        return f"/usercolall {arg} {'pct' if name == 'ucolallpct' else 'to'} {a[0]}"
    return None


def _cancel_row(is_admin: bool):
    return [[B("❌ انصراف", "wiz:cancel")]]


def _ask_question(chat_id: str, name: str, arg: str, step: int, prefix: str = "") -> None:
    q, _kind = PROMPTS[name]["steps"][step]
    extra = ""
    if name == "receipt":
        extra = f"\n\n(شناسه‌ی پرداخت: {arg})"
    send_message(prefix + q + extra, inline_keyboard=_cancel_row(chat_id == ADMIN_CHAT_ID), chat_id=chat_id)


def start_wizard(chat_id: str, data: str, is_admin: bool) -> bool:
    """`data` looks like 'ask:<prompt>[:arg]'. Returns True if a prompt was started."""
    bits = data.split(":", 2)
    name = bits[1] if len(bits) > 1 else ""
    arg = bits[2] if len(bits) > 2 else ""
    spec = PROMPTS.get(name)
    if not spec:
        return False
    if is_admin and spec.get("admin") is False:
        pass                                   # the admin may also use user prompts harmlessly
    if not is_admin and spec.get("admin") is True:
        return False                           # a subscriber can never start an admin prompt
    _wizard[chat_id] = {"prompt": name, "arg": arg, "step": 0, "answers": [], "ts": time.time()}
    _ask_question(chat_id, name, arg, 0)
    return True


def _persist_wizard(state: dict) -> None:
    now = time.time()
    keep = {k: v for k, v in _wizard.items() if now - v.get("ts", 0) < WIZARD_TTL_SECONDS}
    _wizard.clear()
    _wizard.update(keep)
    state["wizard"] = keep


def _load_wizard(state: dict) -> None:
    _wizard.clear()
    now = time.time()
    for k, v in (state.get("wizard") or {}).items():
        if isinstance(v, dict) and now - float(v.get("ts", 0)) < WIZARD_TTL_SECONDS:
            _wizard[str(k)] = v


def handle_wizard_input(update: dict, chat_id: str, text: str, is_admin: bool, meta: dict) -> bool:
    """Feed a free-text answer into the active prompt. Returns True if consumed."""
    w = _wizard.get(chat_id)
    if not w:
        return False
    if time.time() - float(w.get("ts", 0)) > WIZARD_TTL_SECONDS:
        _wizard.pop(chat_id, None)
        return False
    name, arg, step = w["prompt"], w["arg"], int(w["step"])
    spec = PROMPTS.get(name)
    if not spec:
        _wizard.pop(chat_id, None)
        return False
    _q_text, kind = spec["steps"][step]
    msg = update.get("message") or {}
    mid = msg.get("message_id")
    uid = int(update["update_id"])

    # a receipt may be a photo
    if name == "receipt" and (msg.get("photo") or msg.get("document")) and not is_admin:
        copy_message(ADMIN_CHAT_ID, chat_id, mid)
        text = (msg.get("caption") or "").strip() or "📷 تصویر رسید (در چت ربات برای ادمین فرستاده شد)"

    if kind == "secret":
        # Sensitive: remove it from the chat right away, whatever happens next.
        if mid:
            delete_message(chat_id, mid)
        parts = text.split()
        if name == "apikey" and step == 0 and len(parts) == 2 and all(16 <= len(x) <= 300 for x in parts):
            # both keys in one message: accept and finish
            try:
                enc_pub, enc_priv = encrypt_for_vault(parts[0]), encrypt_for_vault(parts[1])
            except RuntimeError:
                _wizard.pop(chat_id, None)
                log.error("USER_VAULT_MASTER_KEY missing - refusing to relay a key at all (never relay plaintext)")
                send_message("⚠️ سرویس اتصال کلید موقتاً در دسترس نیست؛ کمی بعد دوباره امتحان کنید یا به پشتیبانی پیام بدهید.",
                             chat_id=chat_id, inline_keyboard=[[B("🆘 پشتیبانی", "ask:support")]])
                return True
            append_command(uid, f"/connect_enc {enc_pub} {enc_priv}", chat_id=chat_id, meta=meta)
            _wizard.pop(chat_id, None)
            send_message("🔐 کلیدها رمزنگاری و برای بررسی ارسال شد (پیام حاوی کلید از چت پاک شد). چند لحظه صبر کنید...", chat_id=chat_id)
            return True
        if len(parts) != 1 or not (16 <= len(parts[0]) <= 300):
            send_message("⚠️ به نظر کلید کامل نیست. فقط همان یک رشته‌ی کلید را (بدون فاصله و متن اضافه) کپی و ارسال کنید.",
                         inline_keyboard=_cancel_row(False), chat_id=chat_id)
            return True
        try:
            enc = encrypt_for_vault(parts[0])
        except RuntimeError:
            _wizard.pop(chat_id, None)
            log.error("USER_VAULT_MASTER_KEY missing - refusing to relay a key at all (never relay plaintext)")
            send_message("⚠️ سرویس اتصال کلید موقتاً در دسترس نیست؛ کمی بعد دوباره امتحان کنید یا به پشتیبانی پیام بدهید.",
                         chat_id=chat_id, inline_keyboard=[[B("🆘 پشتیبانی", "ask:support")]])
            return True
        w["answers"].append(enc)
        answer = enc
    elif kind in ("num", "int"):
        n = _norm_number(text)
        if n is None or (kind == "int" and float(n) != int(float(n))):
            send_message("⚠️ لطفاً فقط یک عدد بفرستید" + (" (عدد صحیح)" if kind == "int" else "") + "؛ مثلاً 5",
                         inline_keyboard=_cancel_row(is_admin), chat_id=chat_id)
            return True
        answer = n
        w["answers"].append(answer)
    else:
        t = text.strip()
        if not t:
            send_message("⚠️ متن خالی است؛ دوباره بفرستید.", inline_keyboard=_cancel_row(is_admin), chat_id=chat_id)
            return True
        answer = t[:1000]
        w["answers"].append(answer)

    w["ts"] = time.time()
    if step + 1 < len(spec["steps"]):
        w["step"] = step + 1
        _ask_question(chat_id, name, arg, step + 1)
        return True

    # last step: build the command
    answers = list(w["answers"])
    _wizard.pop(chat_id, None)
    if name == "apikey":
        append_command(uid, f"/connect_enc {answers[0]} {answers[1]}", chat_id=chat_id, meta=meta)
        send_message("🔐 هر دو کلید رمزنگاری و برای بررسی ارسال شد (پیام‌های حاوی کلید از چت پاک شد؛ اگر پاک نشده، خودتان حذف کنید).\n"
                     "چند لحظه صبر کنید تا اتصال آزمایش شود...", chat_id=chat_id)
        return True
    try:
        command = _build_command(name, arg, answers, is_admin)
    except (ValueError, IndexError):
        command = None
    if not command:
        send_message("⚠️ ورودی معتبر نبود؛ دوباره از منو شروع کنید.", chat_id=chat_id,
                     inline_keyboard=[[B("📋 منو", "menu" if is_admin else "cmd:/menu")]])
        return True
    queued = append_command(uid, command) if is_admin else append_command(uid, command, chat_id=chat_id, meta=meta)
    if queued:
        mark_pending(chat_id)
    return True


def _meta_of(frm: dict) -> dict:
    return {"first_name": str((frm or {}).get("first_name") or "")[:60], "username": str((frm or {}).get("username") or "")[:40]}


# --------------------------------------------------------------------------- messages
def handle_self_service_message(update: dict, chat_id: str, text: str) -> bool:
    """Non-admin chat. Only SELF_SERVICE_COMMANDS (and wizard answers) ever reach commands.jsonl,
    always tagged with chat_id so executor.py knows this is not the admin (it enforces the same
    allow-list independently). Everything else is treated as a request for the menu."""
    msg = update.get("message") or {}
    if (msg.get("chat") or {}).get("type") != "private":
        return True                               # the bot only talks in private chats
    uid = int(update["update_id"])
    meta = _meta_of(msg.get("from") or {})
    parts = text.split()
    cmd = parts[0].lower().split("@")[0] if parts else ""
    if user_rate_limited(chat_id):
        return True

    # Leaving any prompt: a command or a reply-keyboard button always wins over a pending prompt.
    is_menu_text = text in USER_KB_TEXT or text == "🆘 پشتیبانی"
    if cmd.startswith("/") or is_menu_text:
        _wizard.pop(chat_id, None)
    elif chat_id in _wizard:
        return handle_wizard_input(update, chat_id, text, False, meta)
    elif msg.get("photo") or msg.get("document"):
        send_message("📎 فایل دریافت نشد. برای ارسال رسید پرداخت، از منو «پرداخت کردم — ارسال رسید» را بزنید.",
                     chat_id=chat_id, inline_keyboard=[[B("📋 منوی من", "cmd:/menu")]])
        return True

    if text == "🆘 پشتیبانی":
        start_wizard(chat_id, "ask:support", False)
        return True
    if text in USER_KB_TEXT:
        if append_command(uid, USER_KB_TEXT[text], chat_id=chat_id, meta=meta):
            mark_pending(chat_id)
        return True

    if cmd == "/connect" and len(parts) == 3:               # old typed style still works
        try:
            enc_pub = encrypt_for_vault(parts[1])
            enc_priv = encrypt_for_vault(parts[2])
        except RuntimeError:
            log.error("USER_VAULT_MASTER_KEY missing - refusing to relay a /connect at all (never relay plaintext)")
            send_message("⚠️ سرویس اتصال کلید موقتاً در دسترس نیست؛ بعداً دوباره امتحان کنید.", chat_id=chat_id)
            return True
        append_command(uid, f"/connect_enc {enc_pub} {enc_priv}", chat_id=chat_id, meta=meta)
        mid = msg.get("message_id")
        if mid:
            delete_message(chat_id, mid)
        send_message("🔐 کلید شما رمزنگاری و برای بررسی ارسال شد (پیام حاوی کلید پاک شد). چند لحظه صبر کنید...", chat_id=chat_id)
        return True

    if cmd in SELF_SERVICE_COMMANDS and cmd != "/connect_enc":
        if append_command(uid, text if cmd == parts[0].lower() else " ".join([cmd] + parts[1:]), chat_id=chat_id, meta=meta):
            mark_pending(chat_id)
        return True

    # anything else typed: show the right screen for where they are in the flow
    if append_command(uid, "/menu", chat_id=chat_id, meta=meta):
        mark_pending(chat_id)
    return True


def handle_message(update: dict) -> bool:
    msg = update.get("message") or {}
    chat = msg.get("chat", {})
    chat_id = str(chat.get("id"))
    text = (msg.get("text") or msg.get("caption") or "").strip()
    if chat_id != ADMIN_CHAT_ID:
        return handle_self_service_message(update, chat_id, text)

    uid = int(update["update_id"])
    cmd = text.split()[0].lower().split("@")[0] if text.split() else ""
    _is_fwd = any(k in msg for k in ("forward_origin", "forward_from_chat", "forward_from", "forward_date", "forward_sender_name"))
    if cmd.startswith("/") or text in ADMIN_KB_TEXT or text in ("📋 منو", "منو", "menu"):
        _wizard.pop(chat_id, None)
    elif chat_id in _wizard and (_is_fwd or looks_like_signal(text)) and "\n" in text:
        _wizard.pop(chat_id, None)      # a forwarded/pasted signal must never be swallowed as the answer of a half-finished prompt
    elif chat_id in _wizard:
        return handle_wizard_input(update, chat_id, text, True, {})

    if cmd in ("/start", "/menu", "/help") or text in ("menu", "منو", "📋 منو", "help"):
        send_menu()
        st_ref = STATE_REF.get("state")
        if st_ref is not None and not st_ref.get("admin_kb_sent"):
            send_message("👇 دکمه‌های سریع پایین صفحه فعال شد.", reply_keyboard=admin_reply_keyboard())
            st_ref["admin_kb_sent"] = True
        return True

    if text in ADMIN_KB_TEXT:
        if append_command(uid, ADMIN_KB_TEXT[text]):
            mark_pending(chat_id)
        return True

    if text.startswith("/"):
        if append_command(uid, text if cmd == text.split()[0] else " ".join([cmd] + text.split()[1:])):
            mark_pending(chat_id)
        return True

    # A forwarded (or pasted) trading signal: hand the raw text to the executor, which parses it strictly and
    # opens it with the bot's own settings. base64 keeps the multi-line text intact on a one-line command.
    msg_obj = update.get("message") or {}
    is_forward = any(k in msg_obj for k in ("forward_origin", "forward_from_chat", "forward_from", "forward_date", "forward_sender_name"))
    has_digits = bool(re.search(r"\d", text.translate(_FA_DIGITS)))
    # A forwarded message with numbers always goes to the executor: if it is not a usable signal the admin is told
    # exactly why (instead of silently getting the menu).
    if looks_like_signal(text) or (is_forward and has_digits and not text.startswith("/")):
        payload = base64.b64encode(text[:3500].encode("utf-8")).decode("ascii")
        if append_command(uid, f"/fwdsignal b64:{payload}"):
            mark_pending(chat_id)
        return True

    send_menu()
    return True


STATE_REF: dict = {}       # points at the bridge state dict for the admin-keyboard flag


# --------------------------------------------------------------------------- callbacks
def handle_self_service_callback(update: dict, q: dict, chat_id: str) -> bool:
    """Button taps from a subscriber's own chat. Only 'cmd:/<self-service command>' and
    'ask:<subscriber prompt>' do anything; /connect_enc is never accepted from a button (it needs the
    encryption path in the wizard). Anything else is ignored, exactly like a stranger's message."""
    msg = q.get("message") or {}
    if (msg.get("chat") or {}).get("type") != "private":
        answer_callback(str(q.get("id", "")))
        return True
    data = str(q.get("data", ""))
    meta = _meta_of(q.get("from") or {})
    if user_rate_limited(chat_id):
        answer_callback(str(q.get("id", "")), "کمی آهسته‌تر 🙏")
        return True
    if data == "wiz:cancel":
        _wizard.pop(chat_id, None)
        answer_callback(str(q.get("id", "")), "لغو شد")
        append_command(int(update["update_id"]), "/menu", chat_id=chat_id, meta=meta)
        return True
    if data.startswith("ask:"):
        answer_callback(str(q.get("id", "")))
        start_wizard(chat_id, data, False)
        return True
    if data.startswith("cmd:"):
        command = data[len("cmd:"):].strip()
        cmd = command.split()[0].lower() if command else ""
        if cmd in SELF_SERVICE_COMMANDS and cmd != "/connect_enc":
            _wizard.pop(chat_id, None)
            answer_callback(str(q.get("id", "")), "⏳ در حال انجام…")
            if append_command(int(update["update_id"]), command, chat_id=chat_id, meta=meta):
                mark_pending(chat_id)
            return True
    answer_callback(str(q.get("id", "")))
    return True


def handle_callback(update: dict) -> bool:
    q = update.get("callback_query") or {}
    msg = q.get("message") or {}
    cb_chat_id = str(msg.get("chat", {}).get("id"))
    if cb_chat_id != ADMIN_CHAT_ID:
        return handle_self_service_callback(update, q, cb_chat_id)
    data = str(q.get("data", ""))
    uid = int(update["update_id"])
    qid = str(q.get("id", ""))

    if data == "menu":
        answer_callback(qid)
        _wizard.pop(cb_chat_id, None)
        send_menu()
        return True
    if data == "wiz:cancel":
        answer_callback(qid, "لغو شد")
        _wizard.pop(cb_chat_id, None)
        send_menu()
        return True
    if data in ADMIN_PRESETS:
        answer_callback(qid)
        text, rows = preset_menu(data)
        send_message(text, inline_keyboard=rows)
        return True
    if data == "users_menu":
        answer_callback(qid)
        text, rows = users_menu()
        send_message(text, inline_keyboard=rows)
        return True
    if data == "close_one_help":
        answer_callback(qid, "⏳ در حال انجام…")
        if append_command(uid, "/positions"):
            mark_pending(cb_chat_id)
        return True
    if data.startswith("ask:"):
        answer_callback(qid)
        if not start_wizard(cb_chat_id, data, True):
            send_message("⚠️ این گزینه شناخته نشد.", inline_keyboard=[[B("📋 منو", "menu")]])
        return True
    if data.startswith("ack:"):
        answer_callback(qid, "⏳ در حال انجام…")
        if append_command(uid, f"/ack {data[len('ack:'):]}"):
            mark_pending(cb_chat_id)
        return True
    if data.startswith("cmd:"):
        answer_callback(qid, "⏳ در حال انجام…")
        _wizard.pop(cb_chat_id, None)
        if append_command(uid, data[4:].strip()):
            mark_pending(cb_chat_id)
        return True
    answer_callback(qid)
    return False


def poll_telegram(state: dict) -> bool:
    offset = int(state.get("telegram_update_offset", 0))
    r = requests.get(API + "/getUpdates", params={
        "offset": offset,
        "timeout": LONG_POLL_SECONDS,
        "allowed_updates": json.dumps(["channel_post", "message", "callback_query"]),
    }, timeout=LONG_POLL_SECONDS + 10)
    r.raise_for_status()
    body = r.json()
    if not body.get("ok"):
        raise RuntimeError(str(body))

    changed = False
    signal_lines: list[str] = []
    for update in body.get("result", []):
        uid = int(update.get("update_id", 0))
        state["telegram_update_offset"] = max(int(state.get("telegram_update_offset", 0)), uid + 1)
        post = update.get("channel_post") or {}
        if post and str(post.get("chat", {}).get("id")) == CHANNEL_ID:
            text = post.get("text") or post.get("caption")
            if text:
                message_id = post.get("message_id")
                signal_lines.append(json.dumps({
                    "update_id": uid,
                    "message_id": message_id,
                    "signal_id": f"{CHANNEL_ID}:{message_id}" if message_id else str(uid),
                    "ts": time.time(),
                    # Telegram's own message timestamp, NOT when the relay got
                    # around to forwarding it. If this bridge (or the Windows
                    # executor) was offline and catches up on a backlog of
                    # channel posts in one go, "ts" above would show the
                    # catch-up moment for all of them - useless for staleness
                    # checks. "posted_at" is what the executor actually
                    # compares its age against.
                    "posted_at": post.get("date"),
                    "text": text,
                }, ensure_ascii=False))
                changed = True
        if update.get("message"):
            changed |= handle_message(update)
        if update.get("callback_query"):
            changed |= handle_callback(update)

    if signal_lines:
        append_lines(SIGNALS, signal_lines)
        log.info("Relayed %d Telegram channel posts to signals.jsonl", len(signal_lines))
    return changed or bool(body.get("result"))


NOBITEX_SIGNUP_URL = "https://nobitex.ir/signup/?refcode=221200"


def _sanitize_user_buttons(rows: list) -> list:
    """Defense in depth: a button in a subscriber's chat may only be 'cmd:<self-service command>',
    'ask:<prompt>', or the wizard cancel. Anything else is dropped before Telegram ever sees it."""
    out = []
    for row in rows:
        kept = []
        for b in row:
            d = str(b.get("callback_data", ""))
            if b.get("url") == NOBITEX_SIGNUP_URL and not d:
                kept.append({"text": str(b.get("text", "🆕 ثبت‌نام در نوبیتکس"))[:60], "url": NOBITEX_SIGNUP_URL})   # the ONLY link a user button may carry
            elif d == "wiz:cancel":
                kept.append(b)
            elif d.startswith("ask:") and d.split(":")[1] in PROMPTS and PROMPTS[d.split(":")[1]].get("admin") is not True:
                kept.append(b)
            elif d.startswith("cmd:") and d[4:].split()[:1] and d[4:].split()[0].lower() in SELF_SERVICE_COMMANDS \
                    and d[4:].split()[0].lower() != "/connect_enc":
                kept.append(b)
        if kept:
            out.append(kept)
    return out or [[B("📋 منوی من", "cmd:/menu")]]


def relay_outbox(state: dict) -> bool:
    if not OUTBOX.exists():
        return False
    lines = OUTBOX.read_text(encoding="utf-8").splitlines()
    sent = int(state.get("outbox_lines_sent", 0))
    if sent > len(lines):
        sent = 0
    changed = False
    thread_roots = state.setdefault("thread_roots", {})
    for idx in range(sent, len(lines)):
        line = lines[idx].strip()
        if not line:
            state["outbox_lines_sent"] = idx + 1
            changed = True
            continue
        key = None
        buttons = None
        reply_chat_id = None
        kb = None
        mid = None
        try:
            obj = json.loads(line)
            mid = str(obj.get("mid") or "") or None
            text = str(obj.get("text", line))
            key = obj.get("key") or None
            reply_chat_id = obj.get("chat_id") or None
            kb = obj.get("kb") or None
            raw_buttons = obj.get("buttons")
            if raw_buttons:
                # Accept either a single row ([{...}, {...}]) or multiple
                # rows ([[...], [...]]) from executor.py.
                buttons = raw_buttons if isinstance(raw_buttons[0], list) else [raw_buttons]
        except Exception:
            text = line
        # The same outbox message can appear twice (a GitHub write that timed out but was stored, then retried).
        # Each message has a unique id; one already delivered is skipped, never sent again.
        relayed = state.setdefault("relayed_mids", [])
        if mid and mid in relayed:
            log.info("skipping duplicate outbox message %s", mid[:8])
            state["outbox_lines_sent"] = idx + 1
            changed = True
            continue
        # Every message carries a way back to the menu, so nobody is ever left without a button.
        if not buttons:
            buttons = [[B("📋 منو", "menu")]] if not reply_chat_id or str(reply_chat_id) == ADMIN_CHAT_ID \
                else [[B("📋 منوی من", "cmd:/menu")]]
        if reply_chat_id and str(reply_chat_id) != ADMIN_CHAT_ID:
            buttons = _sanitize_user_buttons(buttons)
        try:
            msg_id = send_message(
                text, inline_keyboard=buttons, chat_id=reply_chat_id,
                # A reply-threaded root message only makes sense within the
                # admin chat's own history - never reuse an admin thread_root
                # for a subscriber's separate chat.
                reply_to_message_id=(thread_roots.get(key) if (key and not reply_chat_id) else None),
            )
            # Thread roots are message ids inside the ADMIN chat only; a
            # subscriber's message id means nothing there and would break
            # later admin replies-to for the same trade key.
            if key and msg_id and key not in thread_roots and not reply_chat_id:
                thread_roots[key] = msg_id
            # The message IS delivered at this point: record it before anything else can fail, so a failure in
            # the optional follow-ups below can never make the same message be sent a second time.
            state["outbox_lines_sent"] = idx + 1
            if mid:
                relayed.append(mid)
                del relayed[:-2000]
            changed = True
            try:
                clear_pending(reply_chat_id or ADMIN_CHAT_ID)      # the answer is here: remove "working on it"
                if kb == "user" and reply_chat_id:
                    send_message("👇 دکمه‌های سریع پایین صفحه فعال شد.", reply_keyboard=user_reply_keyboard(), chat_id=reply_chat_id)
                elif kb == "remove" and reply_chat_id:
                    send_message("ℹ️ منوی سریع پایین صفحه برداشته شد.", remove_keyboard=True, chat_id=reply_chat_id)
            except Exception:
                log.warning("optional follow-up after outbox message %s failed (message itself was delivered)", idx, exc_info=True)
        except Exception as e:
            status = getattr(getattr(e, "response", None), "status_code", None)
            if reply_chat_id and status in (400, 403):
                # Permanent failure for one subscriber's chat (bot blocked,
                # chat deleted, user never started the bot). Retrying would
                # block EVERY message queued behind it - including the admin's
                # - forever, so log it, skip just this message and move on.
                log.error("dropping undeliverable message to user chat %s (HTTP %s): %s", reply_chat_id, status, e)
                if mid:
                    relayed.append(mid)
                    del relayed[:-2000]
            else:
                log.error("outbox delivery failed at line %s: %s", idx, e)
                break
        state["outbox_lines_sent"] = idx + 1
        state["thread_roots"] = thread_roots
        changed = True
    return changed


def self_dispatch_next_run() -> None:
    """Queue the next run of this workflow right now via workflow_dispatch.

    Called near the START of main() (not the end) so that even if this run
    itself crashes or is killed early, the next run has already been queued -
    the 5-minute cron stays as a fallback, but in normal operation the chain
    of self-dispatched runs is what keeps the relay effectively continuous.
    concurrency: cancel-in-progress: false in bridge.yml means a run queued
    while this one is still active simply waits and starts right after,
    instead of overlapping or being dropped.
    """
    if not GH_DISPATCH_TOKEN or not GITHUB_REPOSITORY:
        log.warning("Self-dispatch skipped: GH_DISPATCH_TOKEN or GITHUB_REPOSITORY not set "
                    "(falling back to the 5-minute cron only).")
        return
    url = f"https://api.github.com/repos/{GITHUB_REPOSITORY}/actions/workflows/{BRIDGE_WORKFLOW_FILE}/dispatches"
    try:
        r = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {GH_DISPATCH_TOKEN}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            json={"ref": GITHUB_REF_NAME},
            timeout=15,
        )
        if r.status_code in (201, 204):
            log.info("Self-dispatch: next relay run queued.")
        else:
            log.error("Self-dispatch failed (HTTP %s): %s", r.status_code, r.text[:300])
    except Exception:
        log.exception("Self-dispatch request failed; relying on the 5-minute cron for the next run")


def pull_latest() -> None:
    """Fast-forward the local checkout to match origin/main.

    Without this, outbox.jsonl entries the Windows executor pushes to GitHub
    *while this job is running* stay invisible until the next run's fresh
    checkout - i.e. admin notifications (trade opened, target hit, command
    results, errors) could sit unsent for most of a ~275s window. --ff-only
    means this is a no-op (safely skipped and retried next cycle) whenever
    there are local commits still waiting to be pushed, so it can never
    clobber anything this process itself just wrote.
    """
    try:
        subprocess.run(["git", "fetch", "origin", "main"], check=True,
                       capture_output=True, text=True, timeout=20)
        subprocess.run(["git", "merge", "--ff-only", "origin/main"], check=True,
                       capture_output=True, text=True, timeout=20)
    except Exception as e:
        log.info("pull_latest skipped/failed this cycle (will retry): %s", e)


def check_executor_heartbeat(state: dict) -> bool:
    """Alert the admin directly if the Windows executor has gone silent
    (crashed, PC powered off, lost internet, etc.). Nothing else in this
    system can detect that on its own - a dead executor can't push its own
    "I'm down" message - so this is the one place that can. Alerts once when
    it goes stale and once when it recovers, not on every check. Returns
    True when the debounce flag changed, so the caller knows to push it.
    """
    try:
        if not HEARTBEAT_FILE.exists():
            return False  # executor.py not yet updated to write one, or first run
        data = json.loads(HEARTBEAT_FILE.read_text(encoding="utf-8"))
        age = time.time() - float(data.get("ts", 0))
        was_down = bool(state.get("executor_down_alerted"))
        if age > HEARTBEAT_STALE_SECONDS:
            if not was_down:
                send_message(
                    f"🔴 هشدار: Executor ویندوز حدود {age/60:.0f} دقیقه است هیچ نشانه‌ای از فعالیت "
                    f"نداشته (احتمال خاموش‌شدن سیستم، قطع اینترنت، یا خطای برنامه). لطفاً وضعیت آن را "
                    f"بررسی کنید — تا وقتی دوباره فعال نشود، هیچ سیگنال جدیدی اجرا و هیچ معامله‌ای مدیریت نمی‌شود."
                )
                state["executor_down_alerted"] = True
                return True
        elif was_down:
            send_message("🟢 Executor ویندوز دوباره فعال شد و به‌روزرسانی می‌فرستد.")
            state["executor_down_alerted"] = False
            return True
    except Exception:
        log.exception("heartbeat check failed")
    return False


def git_sync_and_push() -> None:
    ensure_runtime_files()
    subprocess.run(["git", "config", "user.name", "telegram-github-bridge"], check=True)
    subprocess.run(["git", "config", "user.email", "actions@users.noreply.github.com"], check=True)
    subprocess.run(["git", "add", "signals.jsonl", "commands.jsonl", "outbox.jsonl", "bridge_state.json"], check=True)
    if subprocess.run(["git", "diff", "--cached", "--quiet"]).returncode == 0:
        return
    subprocess.run(["git", "commit", "-m", f"chore: telegram relay @ {int(time.time())} [skip ci]"], check=True)
    for attempt in range(5):
        r = subprocess.run(["git", "push", "origin", "HEAD:main"], capture_output=True, text=True)
        if r.returncode == 0:
            return
        if attempt == 4:
            raise RuntimeError(r.stderr[-1500:])
        subprocess.run(["git", "fetch", "origin", "main"], check=False)
        subprocess.run(["git", "rebase", "origin/main"], check=False)
        time.sleep(1 + attempt)


def main() -> None:
    self_dispatch_next_run()  # queue our successor first, before anything else can fail
    try:
        ensure_runtime_files()
        _run_relay_loop()
    except Exception:
        # The successor run was already dispatched above, so continuous
        # operation is unaffected either way - this only prevents a genuinely
        # unexpected error from showing up as a failed run in the Actions
        # history (which was otherwise possible even though every git/network
        # call inside the loop is already individually guarded below).
        log.exception("Unhandled error in relay loop; successor run already queued")


def _run_relay_loop() -> None:
    try:
        setup_bot_commands()
    except Exception:
        log.exception("setup_bot_commands failed (non-fatal, continuing)")
    state = load_state()
    STATE_REF["state"] = state
    _load_wizard(state)
    start = time.time()
    last_push = 0.0
    last_pull = time.time()  # actions/checkout already gave us a fresh copy
    changed_since_push = False
    log.info("Telegram-GitHub relay started for %.0fs", RUNTIME_SECONDS)
    while time.time() - start < RUNTIME_SECONDS:
        now = time.time()
        if now - last_pull >= PULL_MIN_INTERVAL_SECONDS:
            pull_latest()
            last_pull = time.time()
            changed_since_push |= check_executor_heartbeat(state)

        try:
            changed_since_push |= poll_telegram(state)
        except Exception:
            log.exception("Telegram polling failed")
        try:
            changed_since_push |= relay_outbox(state)
        except Exception:
            log.exception("outbox relay failed")
        try:
            changed_since_push |= tick_pending(state)
        except Exception:
            log.exception("pending indicator tick failed")
        _persist_wizard(state)
        save_state(state)

        now = time.time()
        if changed_since_push and (now - last_push) >= PUSH_MIN_INTERVAL_SECONDS:
            try:
                git_sync_and_push()
                changed_since_push = False
            except Exception:
                log.exception("git push failed; will retry next cycle")
            last_push = now
        time.sleep(0.5)

    if changed_since_push:
        try:
            git_sync_and_push()
        except Exception:
            log.exception("final git push failed")
    log.info("Telegram-GitHub relay finished")


if __name__ == "__main__":
    main()
