# -*- coding: utf-8 -*-
"""Telegram <-> GitHub relay for the TIC2NBTX admin/control plane."""
from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from pathlib import Path

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("telegram-github-bridge")

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHANNEL_ID = str(os.environ["TELEGRAM_CHANNEL_ID"])
ADMIN_CHAT_ID = str(os.environ["TELEGRAM_ADMIN_CHAT_ID"])
API = f"https://api.telegram.org/bot{TOKEN}"

SIGNALS = Path("signals.jsonl")
COMMANDS = Path("commands.jsonl")
OUTBOX = Path("outbox.jsonl")
BRIDGE_STATE = Path("bridge_state.json")
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
LONG_POLL_SECONDS = int(os.environ.get("TELEGRAM_LONG_POLL_SECONDS", "5"))

# Push/pull cadence inside the run loop. Previously both only happened once,
# at the very end of the whole ~275s run - a command received right after the
# loop started could sit unpushed (and therefore invisible to the Windows
# executor) for most of that window, and any outbox message the executor
# pushed to GitHub mid-run was only picked up by the *next* bridge.py run's
# fresh checkout. That combination is what made admin commands take minutes
# to answer. Pushing/pulling every few seconds instead keeps both directions
# close to real time without hammering the GitHub API.
PUSH_MIN_INTERVAL_SECONDS = float(os.environ.get("PUSH_MIN_INTERVAL_SECONDS", "3"))
PULL_MIN_INTERVAL_SECONDS = float(os.environ.get("PULL_MIN_INTERVAL_SECONDS", "5"))

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

BOT_COMMANDS = [
    {"command": "start", "description": "باز کردن منوی اصلی"},
    {"command": "menu", "description": "منوی کامل مدیریت"},
    {"command": "status", "description": "وضعیت سیستم"},
    {"command": "balance", "description": "موجودی Nobitex"},
    {"command": "positions", "description": "معاملات باز"},
    {"command": "config", "description": "تنظیمات ربات"},
    {"command": "resume", "description": "فعال‌سازی ورود معاملات"},
    {"command": "pause", "description": "توقف ورود معاملات"},
    {"command": "test_api", "description": "تست اتصال Nobitex"},
    {"command": "reconcile", "description": "همگام‌سازی معاملات"},
    {"command": "protection", "description": "بررسی محافظت معاملات"},
    {"command": "logs", "description": "آخرین لاگ‌های Executor"},
    {"command": "risk", "description": "تنظیم سقف ریسک"},
    {"command": "collateral", "description": "تنظیم مارجین"},
    {"command": "maxtrades", "description": "حداکثر معاملات همزمان"},
    {"command": "closeall", "description": "بستن همه معاملات"},
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


def send_message(text: str, inline_keyboard=None, reply_keyboard=None) -> None:
    payload = {"chat_id": ADMIN_CHAT_ID, "text": str(text)[:4000], "disable_web_page_preview": True}
    if inline_keyboard is not None:
        payload["reply_markup"] = {"inline_keyboard": inline_keyboard}
    elif reply_keyboard is not None:
        payload["reply_markup"] = {"keyboard": reply_keyboard, "resize_keyboard": True, "is_persistent": True}
    telegram_call("sendMessage", payload)


def answer_callback(callback_id: str) -> None:
    try:
        telegram_call("answerCallbackQuery", {"callback_query_id": callback_id})
    except Exception:
        pass


def setup_bot_commands() -> None:
    try:
        telegram_call("setMyCommands", {"commands": BOT_COMMANDS})
    except Exception:
        log.exception("Could not configure Telegram commands")


def reply_keyboard():
    return [
        ["📊 وضعیت سیستم", "💰 موجودی", "📈 معاملات باز"],
        ["▶️ فعال‌سازی ورود", "⏸ توقف ورود"],
        ["📌 ریسک", "💵 مارجین", "🔢 سقف معاملات"],
        ["🧪 تست API", "🔄 همگام‌سازی", "🛡️ بررسی محافظت"],
        ["🧾 آخرین لاگ‌ها", "❌ بستن یک معامله", "🚨 بستن همه معاملات"],
        ["📋 منوی کامل"],
    ]


def inline_main_menu():
    return [
        [{"text": "📊 وضعیت سیستم", "callback_data": "cmd:/status"}, {"text": "💰 موجودی", "callback_data": "cmd:/balance"}],
        [{"text": "📈 معاملات باز", "callback_data": "cmd:/positions"}, {"text": "❌ بستن یک معامله", "callback_data": "close_one_help"}],
        [{"text": "▶️ فعال‌سازی ورود", "callback_data": "cmd:/resume"}, {"text": "⏸ توقف ورود", "callback_data": "cmd:/pause"}],
        [{"text": "🧪 تست API", "callback_data": "cmd:/test_api"}, {"text": "🔄 همگام‌سازی", "callback_data": "cmd:/reconcile"}],
        [{"text": "🛡️ بررسی محافظت", "callback_data": "cmd:/protection"}, {"text": "🧾 آخرین لاگ‌ها", "callback_data": "cmd:/logs"}],
        [{"text": "📌 ریسک", "callback_data": "risk_menu"}, {"text": "💵 مارجین", "callback_data": "collateral_menu"}],
        [{"text": "🔢 سقف معاملات", "callback_data": "maxtrades_menu"}],
        [{"text": "🚨 بستن همه معاملات", "callback_data": "closeall_confirm"}],
    ]


def append_command(update_id: int, command: str) -> None:
    append_lines(COMMANDS, [json.dumps({
        "command_id": f"tg-{update_id}",
        "update_id": update_id,
        "ts": time.time(),
        "command": command,
    }, ensure_ascii=False)])


def send_menu() -> None:
    send_message(
        "🤖 پنل مدیریت TRADE IS COOL — Nobitex Auto Executor\n\n"
        "همه‌ی کنترل‌های Executor و Nobitex از همین منو در دسترس است؛ هر دکمه بلافاصله "
        "فرمان را برای Windows در GitHub ثبت می‌کند.",
        inline_keyboard=inline_main_menu(),
        reply_keyboard=reply_keyboard(),
    )


def handle_message(update: dict) -> bool:
    msg = update.get("message") or {}
    chat = msg.get("chat", {})
    if str(chat.get("id")) != ADMIN_CHAT_ID:
        return False
    text = (msg.get("text") or "").strip()

    if text in {"/start", "/menu", "menu", "/help", "help", "📋 منوی کامل"}:
        send_menu()
        return True

    keyboard_commands = {
        "📊 وضعیت سیستم": "/status", "💰 موجودی": "/balance",
        "📈 معاملات باز": "/positions",
        "▶️ فعال‌سازی ورود": "/resume", "⏸ توقف ورود": "/pause",
        "🧪 تست API": "/test_api", "🔄 همگام‌سازی": "/reconcile",
        "🛡️ بررسی محافظت": "/protection", "🧾 آخرین لاگ‌ها": "/logs",
    }
    if text in keyboard_commands:
        command = keyboard_commands[text]
        append_command(int(update["update_id"]), command)
        send_message(f"📤 فرمان {command} برای Executor ویندوز در GitHub صف شد.")
        return True

    if text == "❌ بستن یک معامله":
        append_command(int(update["update_id"]), "/positions")
        send_message(
            "📈 لیست معاملات باز به‌زودی در همین چت می‌رسد.\n\n"
            "برای بستن یک معامله مشخص، کلید آن (مثلاً BTC_5M) را از پاسخ بالا بردارید و "
            "به‌صورت زیر بفرستید:\n/close BTC_5M"
        )
        return True

    if text == "📌 ریسک":
        send_message("🎯 سقف ریسک هر معامله", inline_keyboard=[
            [{"text": "0.25 USDT", "callback_data": "cmd:/risk 0.25"}, {"text": "0.50 USDT", "callback_data": "cmd:/risk 0.50"}],
            [{"text": "1.00 USDT", "callback_data": "cmd:/risk 1"}, {"text": "↩️ منو", "callback_data": "menu"}],
        ])
        return True

    if text == "💵 مارجین":
        send_message("💵 سقف وجه تضمین هر معامله", inline_keyboard=[
            [{"text": "1.00", "callback_data": "cmd:/collateral 1"}, {"text": "1.25", "callback_data": "cmd:/collateral 1.25"}],
            [{"text": "1.50", "callback_data": "cmd:/collateral 1.5"}, {"text": "2.00", "callback_data": "cmd:/collateral 2"}],
            [{"text": "5.00", "callback_data": "cmd:/collateral 5"}, {"text": "↩️ منو", "callback_data": "menu"}],
        ])
        return True

    if text == "🔢 سقف معاملات":
        send_message("🔢 حداکثر تعداد پوزیشن همزمان", inline_keyboard=[
            [{"text": "5", "callback_data": "cmd:/maxtrades 5"}, {"text": "10", "callback_data": "cmd:/maxtrades 10"}],
            [{"text": "15", "callback_data": "cmd:/maxtrades 15"}, {"text": "20", "callback_data": "cmd:/maxtrades 20"}],
            [{"text": "↩️ منو", "callback_data": "menu"}],
        ])
        return True

    if text == "🚨 بستن همه معاملات":
        send_message("⚠️ تأیید نهایی\n\nآیا واقعاً می‌خواهید تمام پوزیشن‌های باز با Market بسته شوند؟", inline_keyboard=[
            [{"text": "❌ بله، همه را ببند", "callback_data": "cmd:/closeall"}],
            [{"text": "↩️ لغو", "callback_data": "menu"}],
        ])
        return True

    if text.startswith("/"):
        append_command(int(update["update_id"]), text)
        send_message(f"📤 فرمان {text} دریافت شد و برای Executor ویندوز در GitHub صف شد.")
        return True

    send_menu()
    return True


def handle_callback(update: dict) -> bool:
    q = update.get("callback_query") or {}
    msg = q.get("message") or {}
    if str(msg.get("chat", {}).get("id")) != ADMIN_CHAT_ID:
        return False
    answer_callback(str(q.get("id", "")))
    data = str(q.get("data", ""))
    uid = int(update["update_id"])

    if data == "menu":
        send_menu()
        return True

    if data == "close_one_help":
        append_command(uid, "/positions")
        send_message(
            "📈 لیست معاملات باز به‌زودی در همین چت می‌رسد.\n\n"
            "برای بستن یک معامله مشخص، کلید آن (مثلاً BTC_5M) را از پاسخ بالا بردارید و "
            "به‌صورت زیر بفرستید:\n/close BTC_5M"
        )
        return True

    if data == "risk_menu":
        send_message("🎯 سقف ریسک هر معامله", inline_keyboard=[
            [{"text": "0.25 USDT", "callback_data": "cmd:/risk 0.25"}, {"text": "0.50 USDT", "callback_data": "cmd:/risk 0.50"}],
            [{"text": "1.00 USDT", "callback_data": "cmd:/risk 1"}, {"text": "↩️ منو", "callback_data": "menu"}],
        ])
        return True

    if data == "collateral_menu":
        send_message("💵 سقف وجه تضمین هر معامله", inline_keyboard=[
            [{"text": "1.00", "callback_data": "cmd:/collateral 1"}, {"text": "1.25", "callback_data": "cmd:/collateral 1.25"}],
            [{"text": "1.50", "callback_data": "cmd:/collateral 1.5"}, {"text": "2.00", "callback_data": "cmd:/collateral 2"}],
            [{"text": "5.00", "callback_data": "cmd:/collateral 5"}, {"text": "↩️ منو", "callback_data": "menu"}],
        ])
        return True

    if data == "maxtrades_menu":
        send_message("🔢 حداکثر تعداد پوزیشن همزمان", inline_keyboard=[
            [{"text": "5", "callback_data": "cmd:/maxtrades 5"}, {"text": "10", "callback_data": "cmd:/maxtrades 10"}],
            [{"text": "15", "callback_data": "cmd:/maxtrades 15"}, {"text": "20", "callback_data": "cmd:/maxtrades 20"}],
            [{"text": "↩️ منو", "callback_data": "menu"}],
        ])
        return True

    if data == "closeall_confirm":
        send_message("⚠️ تأیید نهایی\n\nآیا واقعاً می‌خواهید تمام پوزیشن‌های باز با Market بسته شوند؟", inline_keyboard=[
            [{"text": "❌ بله، همه را ببند", "callback_data": "cmd:/closeall"}],
            [{"text": "↩️ لغو", "callback_data": "menu"}],
        ])
        return True

    if data.startswith("cmd:"):
        command = data[4:].strip()
        append_command(uid, command)
        send_message(f"📤 فرمان {command} برای Executor ویندوز در GitHub صف شد.")
        return True

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


def relay_outbox(state: dict) -> bool:
    if not OUTBOX.exists():
        return False
    lines = OUTBOX.read_text(encoding="utf-8").splitlines()
    sent = int(state.get("outbox_lines_sent", 0))
    if sent > len(lines):
        sent = 0
    changed = False
    for idx in range(sent, len(lines)):
        line = lines[idx].strip()
        if not line:
            state["outbox_lines_sent"] = idx + 1
            changed = True
            continue
        try:
            text = str(json.loads(line).get("text", line))
        except Exception:
            text = line
        try:
            send_message(text)
        except Exception as e:
            log.error("outbox delivery failed at line %s: %s", idx, e)
            break
        state["outbox_lines_sent"] = idx + 1
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
    ensure_runtime_files()
    self_dispatch_next_run()  # queue our successor first, before anything else can fail
    setup_bot_commands()
    state = load_state()
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

        try:
            changed_since_push |= poll_telegram(state)
        except Exception:
            log.exception("Telegram polling failed")
        try:
            changed_since_push |= relay_outbox(state)
        except Exception:
            log.exception("outbox relay failed")
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
